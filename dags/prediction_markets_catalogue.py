"""Daily DAG: refresh both catalogues, embed what is new, propose equivalences.

The work lives in `prediction_markets` and is tested by pytest outside
Airflow; this module only handles orchestration. Every task is idempotent on
its own, which is what lets the DAG be rerun or backfilled without care.

Deliberately not scheduled more often than daily: the catalogues change on
the order of tens of contracts a day, and the embedding step is the expensive
one. Live prices are a different concern, on a different cadence, in
`arbitrage_scan.py`.
"""

from __future__ import annotations

from datetime import datetime

import httpx
from airflow.decorators import dag, task

from prediction_markets.config import Settings
from prediction_markets.ingestion.embeddings import embed_pending
from prediction_markets.ingestion.markets import ingest_kalshi, ingest_polymarket
from prediction_markets.ingestion.matching import propose_candidates
from prediction_markets.matching.encoders import load_encoder, load_reranker
from prediction_markets.postgres import connect
from prediction_markets.sources.kalshi.auth import KalshiAuth


@dag(
    dag_id="prediction_markets_catalogue",
    schedule="@daily",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["prediction-markets", "ingestion"],
)
def catalogue() -> None:
    @task
    def polymarket() -> int:
        config = Settings()
        connection = connect(config.postgres_dsn)
        try:
            with httpx.Client() as client:
                return ingest_polymarket(
                    root=config.data_dir,
                    client=client,
                    connection=connection,
                    min_volume=100.0,
                )
        finally:
            connection.close()

    @task
    def kalshi() -> int:
        config = Settings()
        if not config.kalshi_key_id or not config.kalshi_private_key_path:
            raise RuntimeError(
                "PREDICTION_MARKETS_KALSHI_KEY_ID and "
                "PREDICTION_MARKETS_KALSHI_PRIVATE_KEY_PATH are required: Kalshi "
                "signs every endpoint, including the public catalogue"
            )
        auth = KalshiAuth.from_file(
            config.kalshi_key_id, config.kalshi_private_key_path
        )
        connection = connect(config.postgres_dsn)
        try:
            with httpx.Client() as client:
                return ingest_kalshi(
                    root=config.data_dir,
                    client=client,
                    auth=auth,
                    connection=connection,
                )
        finally:
            connection.close()

    @task
    def embed() -> int:
        config = Settings()
        connection = connect(config.postgres_dsn)
        try:
            return embed_pending(
                connection=connection,
                encoder=load_encoder(config.embedding_model),
            )
        finally:
            connection.close()

    @task
    def match() -> int:
        config = Settings()
        connection = connect(config.postgres_dsn)
        try:
            return propose_candidates(
                connection=connection,
                reranker=load_reranker(config.reranker_model),
                embedding_model=config.embedding_model,
                min_similarity=config.min_similarity,
                min_rerank=config.min_rerank,
            )
        finally:
            connection.close()

    # The two venues are independent and run in parallel; embedding needs
    # both catalogues in place, and matching needs both sides embedded before
    # a nearest-neighbour lookup means anything.
    [polymarket(), kalshi()] >> embed() >> match()


catalogue()
