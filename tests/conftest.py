import math
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import httpx
import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from testcontainers.community.postgres import PostgresContainer

from prediction_markets.postgres import connect
from prediction_markets.sources.kalshi.auth import KalshiAuth

DIMENSIONS = 1024

Respond = Callable[[httpx.Request], httpx.Response]

_TABLES = (
    "core.arbitrage_scans",
    "core.matched_markets",
    "core.market_match_candidates",
    "core.market_embeddings",
    "core.markets",
)


def unit_vector(axis: int, tilt: float = 0.0) -> list[float]:
    """A vector on the `axis` plane, rotated by `tilt` radians.

    Two vectors on the same axis have cosine similarity cos(tilt difference),
    so a test can ask for "0.9 similar" and get exactly that instead of
    hoping a real model cooperates.
    """
    vector = [0.0] * DIMENSIONS
    vector[axis] = math.cos(tilt)
    vector[axis + 1] = math.sin(tilt)
    return vector


class FakeEncoder:
    """Returns the vector the test assigned to each text."""

    def __init__(self, vectors: dict[str, list[float]], name: str = "fake-encoder"):
        self._vectors = vectors
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vectors[text] for text in texts]


class FakeReranker:
    """Scores pairs from a lookup, defaulting to `fallback` for anything else."""

    def __init__(self, scores: dict[tuple[str, str], float], fallback: float = 0.0):
        self._scores = scores
        self._fallback = fallback
        self.seen: list[tuple[str, str]] = []

    @property
    def name(self) -> str:
        return "fake-reranker"

    def score(self, pairs: Sequence[tuple[str, str]]) -> list[float]:
        self.seen.extend(pairs)
        return [self._scores.get(pair, self._fallback) for pair in pairs]


@pytest.fixture
def gamma_body() -> bytes:
    return (Path(__file__).parent / "fixtures" / "gamma_events.json").read_bytes()


@pytest.fixture
def kalshi_body() -> bytes:
    return (Path(__file__).parent / "fixtures" / "kalshi_markets.json").read_bytes()


@pytest.fixture
def mock_client() -> Callable[[Respond], httpx.Client]:
    def make(respond: Respond) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(respond))

    return make


@pytest.fixture
def always(
    mock_client: Callable[[Respond], httpx.Client],
) -> Callable[[int, bytes], tuple[httpx.Client, list[str]]]:
    """A client that answers every request the same way and records the paths."""

    def make(status: int, body: bytes) -> tuple[httpx.Client, list[str]]:
        seen: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(status, content=body)

        return mock_client(respond), seen

    return make


@pytest.fixture(scope="session")
def kalshi_auth(tmp_path_factory: pytest.TempPathFactory) -> KalshiAuth:
    """A real RSA key pair, generated once, so signing is exercised for real."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path_factory.mktemp("kalshi") / "private.pem"
    path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return KalshiAuth.from_file("key-id", path)


@pytest.fixture(scope="session")
def _postgres_dsn() -> Iterator[str]:
    """An ephemeral Postgres with pgvector, one container for the whole session.

    The pgvector image rather than plain postgres:16: the schema declares a
    `vector` column and an HNSW index, so a stock image would fail at
    `CREATE EXTENSION` and the database tests would never run.
    """
    with PostgresContainer("pgvector/pgvector:pg16") as container:
        url = container.get_connection_url()
        yield "postgresql://" + url.split("://", 1)[1]


@pytest.fixture
def connection(_postgres_dsn: str) -> Iterator[psycopg.Connection]:
    connection = connect(_postgres_dsn)
    connection.execute(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE")
    yield connection
    connection.close()
