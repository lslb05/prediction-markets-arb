"""Command-line interface. Logic lives in the package; this only coordinates."""

import argparse
import logging
import sys
from datetime import UTC, datetime, timedelta

import httpx
import psycopg

from prediction_markets.config import Settings
from prediction_markets.errors import CredentialsMissing, SourceError
from prediction_markets.ingestion.arbitrage import scan
from prediction_markets.ingestion.embeddings import embed_pending
from prediction_markets.ingestion.markets import ingest_kalshi, ingest_polymarket
from prediction_markets.ingestion.matching import propose_candidates
from prediction_markets.matching.encoders import load_encoder, load_reranker
from prediction_markets.postgres import connect
from prediction_markets.repository import (
    confirm_candidate,
    opportunities_since,
    pending_candidates,
)
from prediction_markets.sources.kalshi.auth import KalshiAuth


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="prediction-markets")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest = subparsers.add_parser("ingest", help="collects a venue's catalogue")
    ingest.add_argument("--venue", required=True, choices=["polymarket", "kalshi"])
    ingest.add_argument(
        "--min-volume",
        type=float,
        default=100.0,
        help="Polymarket only: skips contracts below this traded volume",
    )

    subparsers.add_parser("embed", help="embeds open markets that lack a vector")

    match = subparsers.add_parser("match", help="proposes equivalences for review")
    match.add_argument("--min-similarity", type=float)
    match.add_argument("--min-rerank", type=float)

    candidates = subparsers.add_parser("candidates", help="the review queue")
    candidates.add_argument("--limit", type=int, default=20)

    confirm = subparsers.add_parser("confirm", help="promotes a candidate to a pair")
    confirm.add_argument("--candidate", required=True, type=int)

    subparsers.add_parser("scan", help="prices every confirmed pair and records it")

    report = subparsers.add_parser("opportunities", help="recorded edges over a window")
    report.add_argument("--hours", type=int, default=24)
    report.add_argument("--min-edge-bps", type=int)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    config = Settings()
    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        connection = connect(config.postgres_dsn)
    except psycopg.OperationalError as error:
        print(f"could not connect to Postgres: {error}", file=sys.stderr)
        return 2

    try:
        return _dispatch(args, config, connection)
    except CredentialsMissing as error:
        print(error, file=sys.stderr)
        return 2
    except SourceError as error:
        print(f"venue failure: {error}", file=sys.stderr)
        return 1
    finally:
        connection.close()


def _dispatch(
    args: argparse.Namespace, config: Settings, connection: psycopg.Connection
) -> int:
    if args.command == "ingest":
        with httpx.Client() as client:
            if args.venue == "polymarket":
                total = ingest_polymarket(
                    root=config.data_dir,
                    client=client,
                    connection=connection,
                    min_volume=args.min_volume,
                )
            else:
                total = ingest_kalshi(
                    root=config.data_dir,
                    client=client,
                    auth=_kalshi_auth(config),
                    connection=connection,
                )
        print(f"{total} markets on {args.venue}")
        return 0

    if args.command == "embed":
        written = embed_pending(
            connection=connection, encoder=load_encoder(config.embedding_model)
        )
        print(f"{written} markets embedded")
        return 0

    if args.command == "match":
        queued = propose_candidates(
            connection=connection,
            reranker=load_reranker(config.reranker_model),
            embedding_model=config.embedding_model,
            min_similarity=args.min_similarity or config.min_similarity,
            min_rerank=args.min_rerank
            if args.min_rerank is not None
            else config.min_rerank,
        )
        print(f"{queued} candidates queued for review")
        return 0

    if args.command == "candidates":
        rows = pending_candidates(connection, limit=args.limit)
        if not rows:
            print("review queue is empty", file=sys.stderr)
            return 0
        print("id,polymarket,kalshi,similarity,rerank")
        for candidate_id, poly, kalshi, similarity, rerank in rows:
            print(
                f"{candidate_id},{_csv(poly)},{_csv(kalshi)},"
                f"{similarity:.4f},{rerank:.4f}"
            )
        return 0

    if args.command == "confirm":
        try:
            confirm_candidate(connection, candidate_id=args.candidate)
        except psycopg.errors.RaiseException as error:
            print(f"could not confirm: {error}", file=sys.stderr)
            return 2
        print(f"candidate {args.candidate} confirmed")
        return 0

    if args.command == "scan":
        with httpx.Client() as client:
            written = scan(
                connection=connection, client=client, auth=_kalshi_auth(config)
            )
        print(f"{written} observations recorded")
        return 0

    since = datetime.now(UTC) - timedelta(hours=args.hours)
    threshold = (
        args.min_edge_bps if args.min_edge_bps is not None else config.min_edge_bps
    )
    observations = opportunities_since(connection, since=since, min_edge_bps=threshold)
    print("observed_at,kalshi_ticker,strategy,edge_bps,total_cost")
    for observed_at, ticker, strategy, edge_bps, cost in observations:
        print(f"{observed_at.isoformat()},{ticker},{strategy},{edge_bps},{cost:.4f}")
    return 0


def _kalshi_auth(config: Settings) -> KalshiAuth:
    if not config.kalshi_key_id or not config.kalshi_private_key_path:
        raise CredentialsMissing(
            "set PREDICTION_MARKETS_KALSHI_KEY_ID and "
            "PREDICTION_MARKETS_KALSHI_PRIVATE_KEY_PATH. Kalshi signs every "
            "endpoint, including the public catalogue, so there is no "
            "anonymous read path."
        )
    return KalshiAuth.from_file(config.kalshi_key_id, config.kalshi_private_key_path)


def _csv(value: str) -> str:
    """Quotes a field for CSV. Market titles routinely contain commas."""
    escaped = value.replace('"', '""')
    return f'"{escaped}"'
