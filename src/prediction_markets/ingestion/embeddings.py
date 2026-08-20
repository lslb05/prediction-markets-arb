"""Use case: embed the open markets that don't have a vector yet."""

import logging

import psycopg

from prediction_markets.matching.encoders import Encoder
from prediction_markets.repository import (
    PendingMarket,
    markets_pending_embedding,
    store_embeddings,
)

_log = logging.getLogger(__name__)

_BATCH = 256


def embed_pending(
    *, connection: psycopg.Connection, encoder: Encoder, limit: int = 100_000
) -> int:
    """Embeds up to `limit` markets and returns how many were written.

    Batched and committed as it goes rather than in one pass: encoding tens
    of thousands of markets on CPU takes long enough that a crash near the
    end would otherwise throw away the whole run's work, and the next run
    would redo all of it.
    """
    written = 0
    while written < limit:
        pending = markets_pending_embedding(
            connection, model=encoder.name, limit=min(_BATCH, limit - written)
        )
        if not pending:
            break

        vectors = encoder.encode([describe(market) for market in pending])
        store_embeddings(
            connection,
            model=encoder.name,
            vectors=zip((m.market_id for m in pending), vectors, strict=True),
        )
        written += len(pending)
        _log.info("embedded batch=%d total=%d", len(pending), written)

    _log.info("embedding finished model=%s written=%d", encoder.name, written)
    return written


def describe(market: PendingMarket) -> str:
    """The text a market is matched on.

    Title plus outcome and nothing else. Category and dates are deliberately
    left out: they are near-constant across a venue's contracts and would
    pull every pair's similarity toward the same value, compressing the
    range the threshold has to discriminate over.
    """
    return " ".join(
        filter(None, (market.title.strip(), (market.specific_outcome or "").strip()))
    )
