import base64
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

from prediction_markets.errors import CredentialsMissing, SourceError
from prediction_markets.sources.kalshi import client as kalshi
from prediction_markets.sources.kalshi.auth import KalshiAuth

Always = Callable[[int, bytes], tuple[httpx.Client, list[str]]]
MockClient = Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client]


def _verify(auth: KalshiAuth, headers: dict[str, str], message: str) -> None:
    auth.private_key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        message.encode(),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )


def test_signature_covers_the_path_without_the_query_string(
    kalshi_auth: KalshiAuth,
) -> None:
    """Kalshi signs the bare path.

    Including the query string produces a signature the venue rejects on
    every paginated request, and the error it returns reads like clock skew,
    so it is worth pinning.
    """
    headers = kalshi_auth.headers("GET", "/trade-api/v2/markets?cursor=abc")
    timestamp = headers["KALSHI-ACCESS-TIMESTAMP"]

    _verify(kalshi_auth, headers, f"{timestamp}GET/trade-api/v2/markets")


def test_signature_does_not_cover_a_different_path(kalshi_auth: KalshiAuth) -> None:
    headers = kalshi_auth.headers("GET", "/trade-api/v2/markets")
    timestamp = headers["KALSHI-ACCESS-TIMESTAMP"]

    with pytest.raises(InvalidSignature):
        _verify(kalshi_auth, headers, f"{timestamp}GET/trade-api/v2/portfolio/balance")


def test_a_non_rsa_key_is_refused_with_an_actionable_message(tmp_path: Path) -> None:
    path = tmp_path / "ed25519.pem"
    path.write_bytes(
        ed25519.Ed25519PrivateKey.generate().private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    with pytest.raises(CredentialsMissing, match="RSA"):
        KalshiAuth.from_file("key-id", path)


def test_missing_key_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CredentialsMissing):
        KalshiAuth.from_file("key-id", tmp_path / "absent.pem")


def _catalogue(
    mock_client: MockClient, kalshi_body: bytes
) -> Callable[[httpx.Request], httpx.Response]:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/series"):
            return httpx.Response(
                200,
                content=json.dumps(
                    {"series": [{"ticker": "KXFEDDECISION", "category": "Economics"}]}
                ).encode(),
            )
        return httpx.Response(200, content=kalshi_body)

    return respond


def test_multivariate_legs_are_excluded(
    mock_client: MockClient, kalshi_body: bytes, kalshi_auth: KalshiAuth
) -> None:
    """They have no counterpart to pair against and they outnumber everything else."""
    _, markets = kalshi.fetch_markets(
        mock_client(_catalogue(mock_client, kalshi_body)), kalshi_auth
    )

    tickers = {m.external_id for m in markets}
    assert tickers == {"KXFEDDECISION-30MAR-C25", "KXSHUTDOWN-30JUL"}


def test_outcome_suffix_is_stripped(
    mock_client: MockClient, kalshi_body: bytes, kalshi_auth: KalshiAuth
) -> None:
    _, markets = kalshi.fetch_markets(
        mock_client(_catalogue(mock_client, kalshi_body)), kalshi_auth
    )

    fed = next(m for m in markets if m.external_id == "KXFEDDECISION-30MAR-C25")
    assert fed.specific_outcome == "25 bps cut"
    assert fed.category == "Economics"


def test_quote_reports_both_sides_in_dollars(
    mock_client: MockClient, kalshi_auth: KalshiAuth
) -> None:
    body = json.dumps({"market": {"yes_bid": 41, "no_bid": 57}}).encode()
    client = mock_client(lambda _: httpx.Response(200, content=body))

    yes_bid, no_bid = kalshi.fetch_quote(client, kalshi_auth, "TICKER")

    # 0.41 + 0.57 is 0.98, not 1.00: the two sides quote independently and
    # the gap between them is the venue's own spread.
    assert yes_bid == pytest.approx(0.41)
    assert no_bid == pytest.approx(0.57)


def test_zero_bid_is_an_empty_book_not_a_free_contract(
    mock_client: MockClient, kalshi_auth: KalshiAuth
) -> None:
    body = json.dumps({"market": {"yes_bid": 0, "no_bid": 94}}).encode()
    client = mock_client(lambda _: httpx.Response(200, content=body))

    yes_bid, no_bid = kalshi.fetch_quote(client, kalshi_auth, "TICKER")

    assert yes_bid is None
    assert no_bid == pytest.approx(0.94)


def test_rejected_signature_explains_itself(
    always: Always, kalshi_auth: KalshiAuth
) -> None:
    client, seen = always(401, b"")

    with pytest.raises(SourceError, match="clock"):
        kalshi.fetch_quote(client, kalshi_auth, "TICKER")
    # Retrying a rejected signature just burns the rate limit.
    assert len(seen) == 1
