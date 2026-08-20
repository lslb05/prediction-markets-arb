import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
import pytest

from prediction_markets import repository
from prediction_markets.contracts import Market
from prediction_markets.ingestion.arbitrage import scan
from prediction_markets.ingestion.embeddings import embed_pending
from prediction_markets.ingestion.markets import ingest_polymarket
from prediction_markets.ingestion.matching import propose_candidates
from prediction_markets.sources.kalshi.auth import KalshiAuth

from .conftest import FakeEncoder, FakeReranker, unit_vector

MockClient = Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client]

NOW = datetime(2030, 1, 1, tzinfo=UTC)


def _market(external_id: str, title: str, outcome: str | None = None) -> Market:
    return Market(
        external_id=external_id,
        title=title,
        specific_outcome=outcome,
        category="Economics",
        start_date=NOW,
        end_date=NOW + timedelta(days=90),
        status="open",
        execution_data={
            "token_yes": f"{external_id}-yes",
            "token_no": f"{external_id}-no",
        },
    )


def _page(body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        offset = request.url.params.get("offset")
        return httpx.Response(200, content=body if offset == "0" else b"[]")

    return respond


def test_ingestion_archives_the_body_and_publishes_the_rows(
    mock_client: MockClient,
    gamma_body: bytes,
    tmp_path: Path,
    connection: psycopg.Connection,
) -> None:
    total = ingest_polymarket(
        root=tmp_path,
        client=mock_client(_page(gamma_body)),
        connection=connection,
        min_volume=100,
    )

    assert total == 2
    (partition,) = (tmp_path / "raw" / "polymarket" / "markets").glob("collected=*")
    # The archived body is the whole catalogue as of one run, page by page,
    # not the objects the adapter chose to keep.
    stored = json.loads((partition / "data.json").read_text(encoding="utf-8"))
    assert stored[0][0]["title"] == "Fed decision in March 2030"


def test_reingesting_identical_content_creates_no_new_partition(
    mock_client: MockClient,
    gamma_body: bytes,
    tmp_path: Path,
    connection: psycopg.Connection,
) -> None:
    for _ in range(2):
        ingest_polymarket(
            root=tmp_path,
            client=mock_client(_page(gamma_body)),
            connection=connection,
            min_volume=100,
        )

    partitions = list((tmp_path / "raw" / "polymarket" / "markets").glob("collected=*"))
    assert len(partitions) == 1


def test_the_manifest_records_the_contract_it_was_read_under(
    mock_client: MockClient,
    gamma_body: bytes,
    tmp_path: Path,
    connection: psycopg.Connection,
) -> None:
    ingest_polymarket(
        root=tmp_path,
        client=mock_client(_page(gamma_body)),
        connection=connection,
        min_volume=100,
    )

    (partition,) = (tmp_path / "raw" / "polymarket" / "markets").glob("collected=*")
    manifest = json.loads((partition / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["contract_version"] == "polymarket.gamma.events.v1"
    assert manifest["rows"] == 2


def test_embedding_only_touches_what_is_missing(
    connection: psycopg.Connection,
) -> None:
    repository.upsert_markets(
        connection, platform="kalshi", markets=[_market("k-1", "Fed cuts", "25 bps")]
    )
    encoder = FakeEncoder({"Fed cuts 25 bps": unit_vector(0)})

    assert embed_pending(connection=connection, encoder=encoder) == 1
    # A second pass has nothing left to do, so re-running the DAG is free.
    assert embed_pending(connection=connection, encoder=encoder) == 0


def test_a_resolved_market_is_never_embedded(connection: psycopg.Connection) -> None:
    repository.upsert_markets(
        connection, platform="kalshi", markets=[_market("k-1", "Fed cuts")]
    )
    connection.execute("UPDATE core.markets SET status = 'closed'")

    assert embed_pending(connection=connection, encoder=FakeEncoder({})) == 0


def _two_candidates(connection: psycopg.Connection) -> tuple[int, int, int]:
    """One Kalshi market and two near-identical Polymarket markets."""
    repository.upsert_markets(
        connection,
        platform="polymarket",
        markets=[
            _market("p-1", "Fed cuts in March"),
            _market("p-2", "Fed cuts by March"),
        ],
    )
    repository.upsert_markets(
        connection, platform="kalshi", markets=[_market("k-1", "Fed cuts in March")]
    )
    ids = {
        external: _id(connection, platform, external)
        for platform, external in (
            ("polymarket", "p-1"),
            ("polymarket", "p-2"),
            ("kalshi", "k-1"),
        )
    }
    repository.store_embeddings(
        connection,
        model="fake",
        vectors=[
            (ids["p-1"], unit_vector(0, 0.05)),
            (ids["p-2"], unit_vector(0, 0.20)),
            (ids["k-1"], unit_vector(0)),
        ],
    )
    return ids["p-1"], ids["p-2"], ids["k-1"]


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


def test_only_the_best_candidate_per_leg_reaches_the_queue(
    connection: psycopg.Connection,
) -> None:
    """Both Polymarket markets match the same Kalshi contract; only one can be right."""
    _two_candidates(connection)
    reranker = FakeReranker(
        {
            ("Fed cuts in March", "Fed cuts in March"): 0.95,
            ("Fed cuts by March", "Fed cuts in March"): 0.60,
        }
    )

    assert (
        propose_candidates(
            connection=connection,
            reranker=reranker,
            embedding_model="fake",
            min_similarity=0.75,
            min_rerank=0.0,
        )
        == 1
    )
    ((_, poly_text, _, _, rerank),) = repository.pending_candidates(
        connection, limit=10
    )
    assert poly_text.startswith("Fed cuts in March")
    assert rerank == pytest.approx(0.95)


def test_the_reranker_can_veto_what_the_vectors_proposed(
    connection: psycopg.Connection,
) -> None:
    """The whole reason for a second stage.

    'Fed cuts in March' and 'Fed cuts in June' are near-identical vectors and
    opposite contracts; only a model that reads both texts together can tell
    them apart.
    """
    _two_candidates(connection)
    reranker = FakeReranker({}, fallback=-2.0)

    assert (
        propose_candidates(
            connection=connection,
            reranker=reranker,
            embedding_model="fake",
            min_similarity=0.75,
            min_rerank=0.0,
        )
        == 0
    )
    assert reranker.seen, "the reranker should have been consulted"


def test_matching_is_idempotent(connection: psycopg.Connection) -> None:
    _two_candidates(connection)
    reranker = FakeReranker({}, fallback=0.9)
    arguments = {
        "connection": connection,
        "reranker": reranker,
        "embedding_model": "fake",
        "min_similarity": 0.75,
        "min_rerank": 0.0,
    }

    assert propose_candidates(**arguments) == 1  # type: ignore[arg-type]
    assert propose_candidates(**arguments) == 0  # type: ignore[arg-type]


def _scannable_pair(connection: psycopg.Connection) -> int:
    repository.upsert_markets(
        connection, platform="polymarket", markets=[_market("p-1", "Fed cuts")]
    )
    repository.upsert_markets(
        connection, platform="kalshi", markets=[_market("KXFED", "Fed cuts")]
    )
    connection.execute(
        "INSERT INTO core.matched_markets (poly_id, kalshi_id) VALUES (%s, %s)",
        (_id(connection, "polymarket", "p-1"), _id(connection, "kalshi", "KXFED")),
    )
    return repository.matched_pairs(connection)[0].matched_market_id


def _books(
    kalshi: dict[str, int], asks: dict[str, str]
) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        if "clob.polymarket.com" in str(request.url):
            token = request.url.params.get("token_id", "")
            price = asks.get(token)
            body = {"asks": [{"price": price}] if price else []}
            return httpx.Response(200, content=json.dumps(body).encode())
        return httpx.Response(200, content=json.dumps({"market": kalshi}).encode())

    return respond


def test_a_scan_records_both_directions_not_only_the_profitable_one(
    connection: psycopg.Connection, mock_client: MockClient, kalshi_auth: KalshiAuth
) -> None:
    """Without the rejected observations there is no denominator later."""
    pair_id = _scannable_pair(connection)
    client = mock_client(
        _books(
            {"yes_bid": 40, "no_bid": 50},
            {"p-1-yes": "0.52", "p-1-no": "0.55"},
        )
    )

    assert scan(connection=connection, client=client, auth=kalshi_auth) == 2

    rows = connection.execute(
        "SELECT strategy, edge_bps FROM core.arbitrage_scans "
        "WHERE matched_market_id = %s ORDER BY strategy",
        (pair_id,),
    ).fetchall()
    # kalshi_no costs 0.50 + 0.02 fee + 0.52; kalshi_yes costs 0.40 + 0.02 + 0.55.
    assert rows == [("kalshi_no", -400), ("kalshi_yes", 300)]


def test_an_empty_book_side_yields_no_row_for_that_leg(
    connection: psycopg.Connection, mock_client: MockClient, kalshi_auth: KalshiAuth
) -> None:
    _scannable_pair(connection)
    client = mock_client(
        _books({"yes_bid": 40, "no_bid": 0}, {"p-1-yes": "0.52", "p-1-no": "0.55"})
    )

    assert scan(connection=connection, client=client, auth=kalshi_auth) == 1


def test_one_unreachable_venue_does_not_abort_the_sweep(
    connection: psycopg.Connection, mock_client: MockClient, kalshi_auth: KalshiAuth
) -> None:
    _scannable_pair(connection)
    client = mock_client(lambda _: httpx.Response(404, content=b""))

    assert scan(connection=connection, client=client, auth=kalshi_auth) == 0


def test_scanning_with_no_confirmed_pair_is_a_no_op(
    connection: psycopg.Connection, mock_client: MockClient, kalshi_auth: KalshiAuth
) -> None:
    client = mock_client(lambda _: httpx.Response(500, content=b""))

    assert scan(connection=connection, client=client, auth=kalshi_auth) == 0
