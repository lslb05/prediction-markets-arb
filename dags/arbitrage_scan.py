"""Every fifteen minutes: price each confirmed pair against both live books.

Separated from the catalogue DAG because it answers a different question on a
different clock. The catalogue asks what contracts exist, which changes
daily; this asks what they cost right now, which changes continuously. Wiring
them together would either embed thirty thousand markets every quarter hour
or leave the price series too coarse to say anything about how long an edge
survives.

Fifteen minutes is a compromise, not a claim: it is enough to characterise
how often a spread appears and roughly how long it lasts, and far too coarse
to trade from. Acting on an edge is `trader/`'s job, over websockets.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx
from airflow.decorators import dag, task

from prediction_markets.config import Settings
from prediction_markets.ingestion.arbitrage import scan
from prediction_markets.postgres import connect
from prediction_markets.sources.kalshi.auth import KalshiAuth


@dag(
    dag_id="arbitrage_scan",
    schedule="*/15 * * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    # A scan is only meaningful at the moment it runs. Letting runs queue up
    # would fill the table with observations stamped for one instant and
    # collected at another.
    max_active_runs=1,
    dagrun_timeout=timedelta(minutes=10),
    tags=["prediction-markets", "arbitrage"],
)
def arbitrage_scan() -> None:
    @task
    def sweep() -> int:
        config = Settings()
        if not config.kalshi_key_id or not config.kalshi_private_key_path:
            raise RuntimeError(
                "PREDICTION_MARKETS_KALSHI_KEY_ID and "
                "PREDICTION_MARKETS_KALSHI_PRIVATE_KEY_PATH are required"
            )
        auth = KalshiAuth.from_file(
            config.kalshi_key_id, config.kalshi_private_key_path
        )
        connection = connect(config.postgres_dsn)
        try:
            with httpx.Client() as client:
                return scan(connection=connection, client=client, auth=auth)
        finally:
            connection.close()

    sweep()


arbitrage_scan()
