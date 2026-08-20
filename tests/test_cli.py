import psycopg
import pytest

from prediction_markets import cli, repository


@pytest.fixture(autouse=True)
def _point_at_the_container(
    _postgres_dsn: str, connection: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PREDICTION_MARKETS_POSTGRES_DSN", _postgres_dsn)


def test_a_missing_database_is_explained_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "PREDICTION_MARKETS_POSTGRES_DSN",
        "postgresql://nobody@127.0.0.1:1/nothing?connect_timeout=1",
    )

    assert cli.main(["candidates"]) == 2
    assert "could not connect" in capsys.readouterr().err


def test_ingesting_kalshi_without_credentials_says_which_ones(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Kalshi signs even the public catalogue, so this is the first wall anyone hits."""
    assert cli.main(["ingest", "--venue", "kalshi"]) == 2
    assert "KALSHI_KEY_ID" in capsys.readouterr().err


def test_confirming_an_unknown_candidate_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli.main(["confirm", "--candidate", "4242"]) == 2
    assert "could not confirm" in capsys.readouterr().err


def test_the_report_is_csv_even_when_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """So the output stays pipeable instead of switching shape on no rows."""
    assert cli.main(["opportunities", "--hours", "1"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "observed_at,kalshi_ticker,strategy,edge_bps,total_cost"
    ]


def test_titles_with_commas_are_quoted_in_the_queue(
    connection: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    """Market titles routinely contain commas; unquoted they shift every column."""
    poly_id, kalshi_id = _pair_with_commas(connection)
    repository.store_candidates(
        connection, candidates=[(poly_id, kalshi_id, 0.95, 0.9)]
    )

    assert cli.main(["candidates"]) == 0
    body = capsys.readouterr().out.splitlines()[1]
    assert '"Fed cuts 25bps, then holds"' in body


def _pair_with_commas(connection: psycopg.Connection) -> tuple[int, int]:
    from datetime import UTC, datetime

    from prediction_markets.contracts import Market

    def market(external_id: str) -> Market:
        return Market(
            external_id=external_id,
            title="Fed cuts 25bps, then holds",
            specific_outcome=None,
            category=None,
            start_date=None,
            end_date=datetime(2030, 1, 1, tzinfo=UTC),
            status="open",
            execution_data={},
        )

    repository.upsert_markets(
        connection, platform="polymarket", markets=[market("p-1")]
    )
    repository.upsert_markets(connection, platform="kalshi", markets=[market("k-1")])
    rows = connection.execute(
        """
        SELECT p.slug, m.id FROM core.markets m
        JOIN core.platforms p ON p.id = m.platform_id
        """
    ).fetchall()
    by_platform = {slug: int(identifier) for slug, identifier in rows}
    return by_platform["polymarket"], by_platform["kalshi"]
