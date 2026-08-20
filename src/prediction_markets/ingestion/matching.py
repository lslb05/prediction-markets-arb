"""Use case: propose equivalences between the two venues' catalogues.

Two gates in sequence, cheap before expensive. Vector search over the HNSW
index narrows tens of thousands of contracts to a shortlist per market; the
cross-encoder then reads each shortlisted pair as a pair and decides. Nothing
here confirms anything -- the output is a queue a human approves, because a
wrong equivalence doesn't produce a bad row, it produces an unhedged
position.
"""

import logging

import psycopg

from prediction_markets.matching.encoders import Reranker
from prediction_markets.repository import nearest_neighbours, store_candidates

_log = logging.getLogger(__name__)

_NEIGHBOURS_PER_MARKET = 5


def propose_candidates(
    *,
    connection: psycopg.Connection,
    reranker: Reranker,
    embedding_model: str,
    min_similarity: float,
    min_rerank: float,
) -> int:
    """Returns how many new candidates were queued."""
    neighbours = nearest_neighbours(
        connection,
        model=embedding_model,
        min_similarity=min_similarity,
        per_market=_NEIGHBOURS_PER_MARKET,
    )
    if not neighbours:
        _log.info("no pair cleared similarity>=%.2f", min_similarity)
        return 0

    scores = reranker.score([(n.poly_text, n.kalshi_text) for n in neighbours])
    accepted = [
        (n.poly_id, n.kalshi_id, n.similarity, score)
        for n, score in zip(neighbours, scores, strict=True)
        if score >= min_rerank
    ]

    queued = store_candidates(connection, candidates=_one_per_leg(accepted))
    _log.info(
        "matching finished neighbours=%d reranked=%d queued=%d",
        len(neighbours),
        len(accepted),
        queued,
    )
    return queued


def _one_per_leg(
    accepted: list[tuple[int, int, float, float]],
) -> list[tuple[int, int, float, float]]:
    """Keeps the best-scoring candidate for each market on either side.

    A contract can only be equivalent to one contract on the other venue, so
    proposing five variants of the same question fills the review queue with
    alternatives that are mutually exclusive by construction. The database
    enforces this at confirmation time, but leaving it to the procedure means
    a human reads four rows to reject three of them.
    """
    claimed_poly: set[int] = set()
    claimed_kalshi: set[int] = set()
    best: list[tuple[int, int, float, float]] = []

    for candidate in sorted(accepted, key=lambda c: c[3], reverse=True):
        poly_id, kalshi_id, _, _ = candidate
        if poly_id in claimed_poly or kalshi_id in claimed_kalshi:
            continue
        claimed_poly.add(poly_id)
        claimed_kalshi.add(kalshi_id)
        best.append(candidate)
    return best
