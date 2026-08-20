"""Use case: price every confirmed pair against both live books and record it."""

import logging
from datetime import UTC, datetime

import httpx
import psycopg

from prediction_markets.arbitrage import pricing
from prediction_markets.errors import SourceError
from prediction_markets.repository import (
    MatchedPair,
    ScanRecord,
    matched_pairs,
    record_scans,
)
from prediction_markets.sources import polymarket
from prediction_markets.sources.kalshi import client as kalshi
from prediction_markets.sources.kalshi.auth import KalshiAuth

_log = logging.getLogger(__name__)


def scan(
    *,
    connection: psycopg.Connection,
    client: httpx.Client,
    auth: KalshiAuth,
) -> int:
    """Records one observation per tradeable leg. Returns how many rows landed.

    Every scan writes a single `observed_at` for the whole sweep. The books
    are read a few seconds apart, so pretending each row has its own instant
    would imply a precision the collection doesn't have -- and the run, not
    the individual quote, is the unit anyone would group by later.
    """
    observed_at = datetime.now(UTC).replace(microsecond=0)
    pairs = matched_pairs(connection)
    if not pairs:
        _log.info("no confirmed pair to scan")
        return 0

    records: list[ScanRecord] = []
    unreachable = 0
    for pair in pairs:
        try:
            records.extend(_price_pair(pair, client, auth, observed_at))
        except SourceError as error:
            # One venue refusing to quote one contract shouldn't cost the
            # sweep the other forty pairs. It is logged and counted, and the
            # absence of a row for this pair at this instant is itself the
            # record that it couldn't be priced.
            unreachable += 1
            _log.warning("could not price %s: %s", pair.kalshi_ticker, error)

    written = record_scans(connection, records=records)
    _log.info(
        "scan finished pairs=%d priced=%d unreachable=%d rows=%d",
        len(pairs),
        len(records),
        unreachable,
        written,
    )
    return written


def _price_pair(
    pair: MatchedPair,
    client: httpx.Client,
    auth: KalshiAuth,
    observed_at: datetime,
) -> list[ScanRecord]:
    yes_bid, no_bid = kalshi.fetch_quote(client, auth, pair.kalshi_ticker)
    # Fetched in the order the legs are consumed: the Kalshi side is the one
    # that would be entered passively, so it is read first and the hedge is
    # priced against the book that existed after it.
    poly_yes_ask = polymarket.fetch_best_ask(client, pair.poly_token_yes)
    poly_no_ask = polymarket.fetch_best_ask(client, pair.poly_token_no)

    opportunities = pricing.evaluate(
        kalshi_yes_bid=yes_bid,
        kalshi_no_bid=no_bid,
        polymarket_yes_ask=poly_yes_ask,
        polymarket_no_ask=poly_no_ask,
    )
    return [
        ScanRecord(
            matched_market_id=pair.matched_market_id,
            observed_at=observed_at,
            strategy=opportunity.strategy,
            kalshi_bid=opportunity.kalshi_bid,
            polymarket_ask=opportunity.polymarket_ask,
            total_cost=opportunity.total_cost,
            edge_bps=opportunity.edge_bps,
        )
        for opportunity in opportunities
    ]
