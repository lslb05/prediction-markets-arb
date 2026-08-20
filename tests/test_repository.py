from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from prediction_markets import repository
from prediction_markets.contracts import Market

from .conftest import unit_vector

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _market(external_id: str, title: str, **overrides: object) -> Market:
    fields: dict[str, object] = {
        "external_id": external_id,
        "title": title,
        "specific_outcome": None,
        "category": "Economics",
        "start_date": NOW,
        "end_date": NOW + timedelta(days=90),
        "status": "open",
        "execution_data": {
            "token_yes": f"{external_id}-yes",
            "token_no": f"{external_id}-no",
        },
    }
    fields.update(overrides)
    return Market(**fields)  # type: ignore[arg-type]


def _seed_pair(
    connection: psycopg.Connection, *, similarity_tilt: float
) -> tuple[int, int]:
    """One Polymarket and one Kalshi market, embedded at a chosen similarity."""
    repository.upsert_markets(
        connection, platform="polymarket", markets=[_market("poly-1", "Fed cuts")]
    )
    repository.upsert_markets(
        connection, platform="kalshi", markets=[_market("kalshi-1", "Fed cuts")]
    )
    poly_id, kalshi_id = (
        _id(connection, "polymarket", "poly-1"),
        _id(connection, "kalshi", "kalshi-1"),
    )
    repository.store_embeddings(
        connection,
        model="fake",
        vectors=[
            (poly_id, unit_vector(0)),
            (kalshi_id, unit_vector(0, similarity_tilt)),
        ],
    )
    return poly_id, kalshi_id


def _id(connection: psycopg.Connection, platform: str, external_id: str) -> int:
    row = connection.execute(
        """
        SELECT m.id FROM core.markets m
        JOIN core.platforms p ON p.id = m.platform_id
        WHERE p.slug = %s AND m.external_id = %s
        """,
        (platform, external_id),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_platforms_are_seeded_by_the_schema(connection: psycopg.Connection) -> None:
    """The original pipeline hardcoded ids 3 and 4, true only on one machine."""
    assert repository.platform_id(connection, "polymarket") > 0
    assert repository.platform_id(connection, "kalshi") > 0


def test_unknown_platform_fails_loudly(connection: psycopg.Connection) -> None:
    with pytest.raises(LookupError):
        repository.platform_id(connection, "betfair")


def test_reingestion_updates_without_duplicating(
    connection: psycopg.Connection,
) -> None:
    first = repository.upsert_markets(
        connection, platform="kalshi", markets=[_market("k-1", "Fed cuts")]
    )
    second = repository.upsert_markets(
        connection,
        platform="kalshi",
        markets=[_market("k-1", "Fed cuts", status="closed")],
    )

    assert first == second == 1
    row = connection.execute(
        "SELECT status FROM core.markets WHERE external_id = 'k-1'"
    ).fetchone()
    assert row is not None and row[0] == "closed"


def test_first_seen_survives_an_update(connection: psycopg.Connection) -> None:
    """It is the only record of when the venue listed the contract."""
    repository.upsert_markets(
        connection, platform="kalshi", markets=[_market("k-1", "Fed cuts")]
    )
    original = connection.execute(
        "SELECT first_seen_at FROM core.markets WHERE external_id = 'k-1'"
    ).fetchone()

    repository.upsert_markets(
        connection,
        platform="kalshi",
        markets=[_market("k-1", "Fed cuts", status="closed")],
    )
    after = connection.execute(
        "SELECT first_seen_at, updated_at FROM core.markets WHERE external_id = 'k-1'"
    ).fetchone()

    assert original is not None and after is not None
    assert after[0] == original[0]
    assert after[1] >= original[0]


def test_a_status_the_schema_does_not_know_is_rejected(
    connection: psycopg.Connection,
) -> None:
    """The two venues used to write 'OPEN' and 'open' into the same column."""
    with pytest.raises(psycopg.errors.CheckViolation):
        repository.upsert_markets(
            connection,
            platform="kalshi",
            markets=[_market("k-1", "Fed cuts", status="OPEN")],
        )


def test_neighbour_search_applies_the_similarity_floor(
    connection: psycopg.Connection,
) -> None:
    # cos(1.2 rad) is about 0.36, well under any usable threshold.
    _seed_pair(connection, similarity_tilt=1.2)

    assert (
        repository.nearest_neighbours(
            connection, model="fake", min_similarity=0.75, per_market=5
        )
        == []
    )


def test_neighbour_search_returns_a_close_pair(
    connection: psycopg.Connection,
) -> None:
    poly_id, kalshi_id = _seed_pair(connection, similarity_tilt=0.3)

    (neighbour,) = repository.nearest_neighbours(
        connection, model="fake", min_similarity=0.75, per_market=5
    )

    assert (neighbour.poly_id, neighbour.kalshi_id) == (poly_id, kalshi_id)
    assert neighbour.similarity == pytest.approx(0.9553, abs=1e-3)


def test_a_vector_from_another_model_is_not_compared(
    connection: psycopg.Connection,
) -> None:
    """Two checkpoints produce two coordinate systems; mixing them is meaningless."""
    _seed_pair(connection, similarity_tilt=0.3)

    assert (
        repository.nearest_neighbours(
            connection, model="another-model", min_similarity=0.75, per_market=5
        )
        == []
    )


def test_a_confirmed_market_is_not_proposed_again(
    connection: psycopg.Connection,
) -> None:
    poly_id, kalshi_id = _seed_pair(connection, similarity_tilt=0.3)
    connection.execute(
        "INSERT INTO core.matched_markets (poly_id, kalshi_id) VALUES (%s, %s)",
        (poly_id, kalshi_id),
    )

    assert (
        repository.nearest_neighbours(
            connection, model="fake", min_similarity=0.75, per_market=5
        )
        == []
    )


def test_the_trigger_denormalises_the_titles(connection: psycopg.Connection) -> None:
    """So a human reviewing the queue reads both questions without a four-way join."""
    poly_id, kalshi_id = _seed_pair(connection, similarity_tilt=0.3)
    repository.store_candidates(
        connection, candidates=[(poly_id, kalshi_id, 0.95, 0.8)]
    )

    ((_, poly_text, kalshi_text, _, _),) = repository.pending_candidates(
        connection, limit=10
    )
    assert poly_text.startswith("Fed cuts")
    assert kalshi_text.startswith("Fed cuts")


def test_confirming_drops_the_alternatives_for_both_legs(
    connection: psycopg.Connection,
) -> None:
    """A Kalshi contract cannot be equivalent to two Polymarket contracts."""
    poly_id, kalshi_id = _seed_pair(connection, similarity_tilt=0.3)
    repository.upsert_markets(
        connection, platform="polymarket", markets=[_market("poly-2", "Fed cuts soon")]
    )
    rival = _id(connection, "polymarket", "poly-2")
    repository.store_candidates(
        connection,
        candidates=[(poly_id, kalshi_id, 0.95, 0.9), (rival, kalshi_id, 0.90, 0.7)],
    )
    chosen = repository.pending_candidates(connection, limit=10)[0][0]

    repository.confirm_candidate(connection, candidate_id=chosen)

    assert repository.pending_candidates(connection, limit=10) == []
    assert len(repository.matched_pairs(connection)) == 1


def test_confirming_a_candidate_that_does_not_exist_raises(
    connection: psycopg.Connection,
) -> None:
    with pytest.raises(psycopg.errors.RaiseException):
        repository.confirm_candidate(connection, candidate_id=9999)


def test_an_expired_pair_is_not_scanned(connection: psycopg.Connection) -> None:
    repository.upsert_markets(
        connection,
        platform="polymarket",
        markets=[_market("poly-1", "Old", end_date=datetime(2020, 1, 1, tzinfo=UTC))],
    )
    repository.upsert_markets(
        connection, platform="kalshi", markets=[_market("kalshi-1", "Old")]
    )
    connection.execute(
        "INSERT INTO core.matched_markets (poly_id, kalshi_id) VALUES (%s, %s)",
        (
            _id(connection, "polymarket", "poly-1"),
            _id(connection, "kalshi", "kalshi-1"),
        ),
    )

    assert repository.matched_pairs(connection) == []


def test_rescanning_the_same_instant_does_not_duplicate(
    connection: psycopg.Connection,
) -> None:
    pair_id = _confirmed_pair(connection)
    record = repository.ScanRecord(
        matched_market_id=pair_id,
        observed_at=NOW,
        strategy="kalshi_yes",
        kalshi_bid=0.40,
        polymarket_ask=0.55,
        total_cost=0.97,
        edge_bps=300,
    )

    assert repository.record_scans(connection, records=[record]) == 1
    assert repository.record_scans(connection, records=[record]) == 0


def test_the_report_keeps_only_edges_above_the_floor(
    connection: psycopg.Connection,
) -> None:
    pair_id = _confirmed_pair(connection)
    observed = datetime.now(UTC).replace(microsecond=0)
    repository.record_scans(
        connection,
        records=[
            repository.ScanRecord(
                pair_id, observed, "kalshi_yes", 0.4, 0.55, 0.97, 300
            ),
            repository.ScanRecord(
                pair_id, observed, "kalshi_no", 0.5, 0.52, 1.04, -400
            ),
        ],
    )

    rows = repository.opportunities_since(
        connection, since=observed - timedelta(hours=1), min_edge_bps=200
    )

    assert [row[2] for row in rows] == ["kalshi_yes"]


def _confirmed_pair(connection: psycopg.Connection) -> int:
    poly_id, kalshi_id = _seed_pair(connection, similarity_tilt=0.3)
    connection.execute(
        "INSERT INTO core.matched_markets (poly_id, kalshi_id) VALUES (%s, %s)",
        (poly_id, kalshi_id),
    )
    return repository.matched_pairs(connection)[0].matched_market_id
