"""Request signing for Kalshi's trade API.

Kalshi authenticates every endpoint, including the public market catalogue,
so there is no anonymous read path: without a key pair the venue is simply
unreachable and ingestion cannot start.
"""

import base64
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from prediction_markets.errors import CredentialsMissing


@dataclass(frozen=True, slots=True)
class KalshiAuth:
    """Signs requests with the RSA key issued alongside the key id."""

    key_id: str
    private_key: rsa.RSAPrivateKey

    @classmethod
    def from_file(cls, key_id: str, private_key_path: Path) -> "KalshiAuth":
        try:
            pem = private_key_path.read_bytes()
        except OSError as error:
            raise CredentialsMissing(
                f"could not read the Kalshi private key at {private_key_path}"
            ) from error

        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise CredentialsMissing(
                f"{private_key_path} is not an RSA private key; Kalshi issues RSA keys"
            )
        return cls(key_id=key_id, private_key=key)

    def headers(self, method: str, path: str) -> dict[str, str]:
        """Headers for one request. The signature covers method and path, so it
        is valid for exactly that call and expires with the timestamp.
        """
        timestamp = str(int(time.time() * 1000))
        # The query string is excluded from the signed payload: Kalshi signs
        # the path only, and including it makes every paginated request fail
        # with a signature error that reads like a clock-skew problem.
        message = f"{timestamp}{method}{path.split('?')[0]}".encode()

        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json",
        }
