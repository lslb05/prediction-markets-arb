"""Application configuration loaded from environment variables."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime settings."""

    model_config = SettingsConfigDict(env_prefix="PREDICTION_MARKETS_", extra="ignore")

    log_level: str = "INFO"
    data_dir: Path = Path("data")

    postgres_dsn: str = "postgresql://markets:markets@localhost:5434/prediction_markets"

    # Kalshi signs every request, including the public market listing, so
    # there is no anonymous read path: without these two the venue is simply
    # unreachable.
    kalshi_key_id: str | None = None
    kalshi_private_key_path: Path | None = None

    embedding_model: str = "BAAI/bge-m3"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"

    # Two gates, deliberately. Cosine over bge-m3 is cheap and runs over
    # every pair, so it is set loose enough to keep recall; the cross-encoder
    # is expensive and only sees what survived, so it decides precision.
    min_similarity: float = Field(default=0.75, ge=0.0, le=1.0)
    min_rerank: float = 0.0

    # A pair is only worth capital if both legs clear fees and slippage;
    # below this the scan still records the observation, it just isn't an
    # opportunity.
    min_edge_bps: int = 200
