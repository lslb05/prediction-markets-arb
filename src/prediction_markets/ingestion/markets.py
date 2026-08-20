"""Use case: one catalogue ingestion run, from a venue to core.markets."""

import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg

from prediction_markets import raw
from prediction_markets.contracts import Market
from prediction_markets.repository import upsert_markets
from prediction_markets.sources import polymarket
from prediction_markets.sources.kalshi import client as kalshi
from prediction_markets.sources.kalshi.auth import KalshiAuth

_log = logging.getLogger(__name__)


def ingest_polymarket(
    *,
    root: Path,
    client: httpx.Client,
    connection: psycopg.Connection,
    min_volume: float,
) -> int:
    """Returns how many Polymarket markets are known after this run."""
    started = datetime.now(UTC)
    _log.info("ingestion started venue=polymarket min_volume=%s", min_volume)

    body, markets = polymarket.fetch_markets(client, min_volume=min_volume)
    _archive(
        root,
        source="polymarket",
        body=body,
        url=polymarket.GAMMA_URL,
        contract_version=polymarket.CONTRACT_VERSION,
        rows=len(markets),
        collected_at=started,
    )
    return _publish(connection, "polymarket", markets, started)


def ingest_kalshi(
    *,
    root: Path,
    client: httpx.Client,
    auth: KalshiAuth,
    connection: psycopg.Connection,
) -> int:
    """Returns how many Kalshi markets are known after this run."""
    started = datetime.now(UTC)
    _log.info("ingestion started venue=kalshi")

    body, markets = kalshi.fetch_markets(client, auth)
    _archive(
        root,
        source="kalshi",
        body=body,
        # The signed headers, not the URL, carry the credential, so the
        # manifest can record the endpoint verbatim.
        url=kalshi.BASE_URL + kalshi.MARKETS_PATH,
        contract_version=kalshi.CONTRACT_VERSION,
        rows=len(markets),
        collected_at=started,
    )
    return _publish(connection, "kalshi", markets, started)


def _archive(
    root: Path,
    *,
    source: str,
    body: bytes,
    url: str,
    contract_version: str,
    rows: int,
    collected_at: datetime,
) -> None:
    manifest = raw.write(
        root / "raw",
        source=source,
        dataset="markets",
        body=body,
        url=url,
        contract_version=contract_version,
        rows=rows,
        collected_at=collected_at,
    )
    if manifest is None:
        _log.info("catalogue identical to the previous run venue=%s", source)


def _publish(
    connection: psycopg.Connection,
    venue: str,
    markets: list[Market],
    started: datetime,
) -> int:
    total = upsert_markets(connection, platform=venue, markets=markets)
    _log.info(
        "ingestion finished venue=%s collected=%d total=%d duration_s=%.2f",
        venue,
        len(markets),
        total,
        (datetime.now(UTC) - started).total_seconds(),
    )
    return total
