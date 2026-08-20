"""Every SQL statement the package issues, in one place.

Nothing above this module writes SQL, and this module knows nothing about
HTTP or models. That split is what lets the ingestion, matching and
arbitrage use cases be read without also reading a query planner.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

import psycopg
from psycopg.types.json import Jsonb

from prediction_markets.contracts import Market


@dataclass(frozen=True, slots=True)
class PendingMarket:
    """A market whose text has not been embedded with the current model."""

    market_id: int
    title: str
    specific_outcome: str | None


@dataclass(frozen=True, slots=True)
class Neighbour:
    """A near-duplicate pair proposed by vector search, before reranking."""

    poly_id: int
    poly_text: str
    kalshi_id: int
    kalshi_text: str
    similarity: float


@dataclass(frozen=True, slots=True)
class MatchedPair:
    """A confirmed equivalence, with what the scanner needs to price both legs."""

    matched_market_id: int
    kalshi_ticker: str
    poly_token_yes: str
    poly_token_no: str
    label: str


@dataclass(frozen=True, slots=True)
class ScanRecord:
    matched_market_id: int
    observed_at: datetime
    strategy: str
    kalshi_bid: float
    polymarket_ask: float
    total_cost: float
    edge_bps: int


def platform_id(connection: psycopg.Connection, slug: str) -> int:
    row = connection.execute(
        "SELECT id FROM core.platforms WHERE slug = %s", (slug,)
    ).fetchone()
    if row is None:
        raise LookupError(f"platform not seeded: {slug}")
    return int(row[0])


def upsert_markets(
    connection: psycopg.Connection, *, platform: str, markets: Sequence[Market]
) -> int:
    """Writes a batch and returns how many markets the platform now has.

    Only the mutable fields are overwritten. `first_seen_at` is left alone on
    conflict so the row keeps the moment the contract first appeared, which
    is the only record of when a venue listed it.
    """
    if not markets:
        return count_markets(connection, platform=platform)

    identifier = platform_id(connection, platform)
    connection.cursor().executemany(
        """
        INSERT INTO core.markets
            (platform_id, external_id, title, category, specific_outcome,
             start_date, end_date, status, execution_data)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (platform_id, external_id) DO UPDATE SET
            title = EXCLUDED.title,
            category = EXCLUDED.category,
            specific_outcome = EXCLUDED.specific_outcome,
            end_date = EXCLUDED.end_date,
            status = EXCLUDED.status,
            execution_data = EXCLUDED.execution_data,
            updated_at = now()
        """,
        [
            (
                identifier,
                m.external_id,
                m.title,
                m.category,
                m.specific_outcome,
                m.start_date,
                m.end_date,
                m.status,
                Jsonb(m.execution_data),
            )
            for m in markets
        ],
    )
    return count_markets(connection, platform=platform)


def count_markets(connection: psycopg.Connection, *, platform: str) -> int:
    row = connection.execute(
        """
        SELECT count(*) FROM core.markets m
        JOIN core.platforms p ON p.id = m.platform_id
        WHERE p.slug = %s
        """,
        (platform,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def markets_pending_embedding(
    connection: psycopg.Connection, *, model: str, limit: int
) -> list[PendingMarket]:
    """Open markets with no vector from `model` yet.

    Filtered to open markets on purpose: a resolved contract can never be one
    leg of a live spread, and embedding the whole historical tail would
    dominate the run for no benefit.
    """
    rows = connection.execute(
        """
        SELECT m.id, m.title, m.specific_outcome
        FROM core.markets m
        LEFT JOIN core.market_embeddings e
               ON e.market_id = m.id AND e.model = %s
        WHERE e.market_id IS NULL AND m.status = 'open'
        ORDER BY m.id
        LIMIT %s
        """,
        (model, limit),
    ).fetchall()
    return [PendingMarket(int(r[0]), r[1], r[2]) for r in rows]


def store_embeddings(
    connection: psycopg.Connection,
    *,
    model: str,
    vectors: Iterable[tuple[int, Sequence[float]]],
) -> None:
    connection.cursor().executemany(
        """
        INSERT INTO core.market_embeddings (market_id, model, embedding)
        VALUES (%s, %s, %s)
        ON CONFLICT (market_id) DO UPDATE SET
            model = EXCLUDED.model,
            embedding = EXCLUDED.embedding,
            created_at = now()
        """,
        [(market_id, model, list(vector)) for market_id, vector in vectors],
    )


def nearest_neighbours(
    connection: psycopg.Connection,
    *,
    model: str,
    min_similarity: float,
    per_market: int,
) -> list[Neighbour]:
    """Candidate pairs from vector search, cheapest gate first.

    The k-nearest lookup runs inside Postgres against the HNSW index rather
    than as a full cross product in Python. The original pipeline pulled
    every embedding into memory and multiplied two matrices, which is
    quadratic in the number of open markets and redoes on every run the work
    the index already holds.

    Pairs already queued or already confirmed are excluded here so the
    reranker -- the expensive half -- never sees them.
    """
    rows = connection.execute(
        """
        SELECT poly.id,
               concat_ws(' ', poly.title, poly.specific_outcome),
               kalshi.id,
               concat_ws(' ', kalshi.title, kalshi.specific_outcome),
               1 - (poly_vec.embedding <=> neighbour.embedding) AS similarity
        FROM core.markets poly
        JOIN core.market_embeddings poly_vec
             ON poly_vec.market_id = poly.id AND poly_vec.model = %(model)s
        JOIN LATERAL (
            SELECT candidate_vec.market_id, candidate_vec.embedding
            FROM core.market_embeddings candidate_vec
            JOIN core.markets candidate ON candidate.id = candidate_vec.market_id
            WHERE candidate.platform_id = %(kalshi)s
              AND candidate.status = 'open'
              AND candidate_vec.model = %(model)s
            ORDER BY candidate_vec.embedding <=> poly_vec.embedding
            LIMIT %(per_market)s
        ) AS neighbour ON true
        JOIN core.markets kalshi ON kalshi.id = neighbour.market_id
        WHERE poly.platform_id = %(poly)s
          AND poly.status = 'open'
          AND 1 - (poly_vec.embedding <=> neighbour.embedding) >= %(min_similarity)s
          AND NOT EXISTS (
              SELECT 1 FROM core.market_match_candidates c
              WHERE c.poly_id = poly.id OR c.kalshi_id = kalshi.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM core.matched_markets m
              WHERE m.poly_id = poly.id OR m.kalshi_id = kalshi.id
          )
        ORDER BY similarity DESC
        """,
        {
            "model": model,
            "poly": platform_id(connection, "polymarket"),
            "kalshi": platform_id(connection, "kalshi"),
            "per_market": per_market,
            "min_similarity": min_similarity,
        },
    ).fetchall()
    return [Neighbour(int(r[0]), r[1], int(r[2]), r[3], float(r[4])) for r in rows]


def store_candidates(
    connection: psycopg.Connection,
    *,
    candidates: Sequence[tuple[int, int, float, float]],
) -> int:
    """Queues reranked pairs for human confirmation. Returns how many were new."""
    if not candidates:
        return 0
    cursor = connection.cursor()
    cursor.executemany(
        """
        INSERT INTO core.market_match_candidates
            (poly_id, kalshi_id, score_similarity, score_reranker)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (poly_id, kalshi_id) DO NOTHING
        """,
        candidates,
    )
    return cursor.rowcount


def pending_candidates(
    connection: psycopg.Connection, *, limit: int
) -> list[tuple[int, str, str, float, float]]:
    rows = connection.execute(
        """
        SELECT id,
               concat_ws(' | ', poly_title, poly_outcome),
               concat_ws(' | ', kalshi_title, kalshi_outcome),
               score_similarity,
               score_reranker
        FROM core.market_match_candidates
        ORDER BY score_reranker DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [(int(r[0]), r[1], r[2], float(r[3]), float(r[4])) for r in rows]


def confirm_candidate(connection: psycopg.Connection, *, candidate_id: int) -> None:
    """Promotes a candidate to a confirmed pair.

    Delegated to the stored procedure so the confirmation and the removal of
    the now-impossible competing candidates happen in one transaction,
    whether the caller is this CLI or a psql session.
    """
    connection.execute("CALL core.match_market(%s)", (candidate_id,))


def matched_pairs(connection: psycopg.Connection) -> list[MatchedPair]:
    """Confirmed pairs that are still live and routable on both venues."""
    rows = connection.execute(
        """
        SELECT id, kalshi_ticker, poly_token_yes, poly_token_no,
               concat_ws(' | ', kalshi_title, kalshi_outcome)
        FROM core.vw_matched_markets
        WHERE poly_token_yes IS NOT NULL
          AND poly_token_no IS NOT NULL
          AND (end_date IS NULL OR end_date > now())
        ORDER BY end_date
        """
    ).fetchall()
    return [MatchedPair(int(r[0]), r[1], r[2], r[3], r[4]) for r in rows]


def record_scans(
    connection: psycopg.Connection, *, records: Sequence[ScanRecord]
) -> int:
    if not records:
        return 0
    cursor = connection.cursor()
    cursor.executemany(
        """
        INSERT INTO core.arbitrage_scans
            (matched_market_id, observed_at, strategy, kalshi_bid,
             polymarket_ask, total_cost, edge_bps)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (matched_market_id, observed_at, strategy) DO NOTHING
        """,
        [
            (
                r.matched_market_id,
                r.observed_at,
                r.strategy,
                r.kalshi_bid,
                r.polymarket_ask,
                r.total_cost,
                r.edge_bps,
            )
            for r in records
        ],
    )
    return cursor.rowcount


def opportunities_since(
    connection: psycopg.Connection, *, since: datetime, min_edge_bps: int
) -> list[tuple[datetime, str, str, int, float]]:
    rows = connection.execute(
        """
        SELECT s.observed_at, v.kalshi_ticker, s.strategy, s.edge_bps, s.total_cost
        FROM core.arbitrage_scans s
        JOIN core.vw_matched_markets v ON v.id = s.matched_market_id
        WHERE s.observed_at >= %s AND s.edge_bps >= %s
        ORDER BY s.edge_bps DESC, s.observed_at DESC
        """,
        (since, min_edge_bps),
    ).fetchall()
    return [(r[0], r[1], r[2], int(r[3]), float(r[4])) for r in rows]
