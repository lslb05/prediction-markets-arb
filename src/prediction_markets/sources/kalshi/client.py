"""Kalshi adapter: the market catalogue and the top of book."""

import json
import logging
import time
from datetime import datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from prediction_markets.contracts import Market
from prediction_markets.errors import SourceError, SourceUnavailable, UnexpectedSchema
from prediction_markets.sources.kalshi.auth import KalshiAuth

BASE_URL = "https://api.elections.kalshi.com"
MARKETS_PATH = "/trade-api/v2/markets"
SERIES_PATH = "/trade-api/v2/series"
CONTRACT_VERSION = "kalshi.trade-api.v2.markets.v1"

_PAGE_SIZE = 1000
_ATTEMPTS = 3
_TIMEOUT_S = 30.0
_CENTS = 100.0

_log = logging.getLogger(__name__)


class KalshiMarket(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ticker: str
    event_ticker: str = ""
    title: str
    subtitle: str | None = None
    yes_sub_title: str | None = None
    open_time: datetime | None = None
    expiration_time: datetime | None = None
    yes_bid: int | None = None
    no_bid: int | None = None
    # Present on legs of a multivariate collection, which are excluded below.
    mve_collection_ticker: str | None = None


class MarketsPage(BaseModel):
    model_config = ConfigDict(extra="ignore")

    markets: list[KalshiMarket] = Field(default_factory=list)
    cursor: str | None = None


class Quote(BaseModel):
    """Top of book for one contract, in cents as the API reports it."""

    model_config = ConfigDict(extra="ignore")

    yes_bid: int | None = None
    no_bid: int | None = None


def fetch_markets(client: httpx.Client, auth: KalshiAuth) -> tuple[bytes, list[Market]]:
    """Walks the cursor to the end and returns the raw pages and the markets.

    Sequential by construction: the API paginates by opaque cursor, so page
    n+1 is unknowable until page n comes back.
    """
    categories = _series_categories(client, auth)
    pages: list[object] = []
    markets: list[Market] = []
    cursor: str | None = None

    while True:
        params = {"limit": str(_PAGE_SIZE), "status": "open"}
        if cursor:
            params["cursor"] = cursor

        body = _get(client, MARKETS_PATH, params, auth)
        pages.append(json.loads(body))
        try:
            page = MarketsPage.model_validate_json(body)
        except ValidationError as error:
            raise UnexpectedSchema("Kalshi markets payload's shape changed") from error

        if not page.markets:
            break
        markets.extend(_to_markets(page.markets, categories))
        cursor = page.cursor
        if not cursor:
            break
        # Courtesy pacing. The venue's published limit is well above this;
        # the pause exists so a full catalogue walk doesn't look like a
        # scrape from the other side.
        time.sleep(0.05)

    return json.dumps(pages).encode("utf-8"), markets


def fetch_quote(
    client: httpx.Client, auth: KalshiAuth, ticker: str
) -> tuple[float | None, float | None]:
    """Best YES bid and best NO bid in dollars, None where the side is empty.

    Kalshi quotes the two sides independently; the NO bid is not one minus
    the YES bid, and the gap between them is the venue's spread.
    """
    body = _get(client, f"{MARKETS_PATH}/{ticker}", {}, auth)
    try:
        payload = json.loads(body)["market"]
        quote = Quote.model_validate(payload)
    except (json.JSONDecodeError, KeyError, ValidationError) as error:
        raise UnexpectedSchema(f"Kalshi quote payload changed for {ticker}") from error

    return _to_dollars(quote.yes_bid), _to_dollars(quote.no_bid)


def _to_dollars(cents: int | None) -> float | None:
    # Zero means an empty side of the book, not a contract trading at zero,
    # and pricing it as free would invent an arbitrage out of thin air.
    if not cents:
        return None
    return cents / _CENTS


def _series_categories(client: httpx.Client, auth: KalshiAuth) -> dict[str, str]:
    """Category per series ticker. An individual market doesn't carry one."""
    body = _get(client, SERIES_PATH, {}, auth)
    try:
        series = json.loads(body).get("series", [])
        return {
            item["ticker"]: item["category"]
            for item in series
            if item.get("ticker") and item.get("category")
        }
    except (json.JSONDecodeError, AttributeError, TypeError) as error:
        raise UnexpectedSchema("Kalshi series payload changed") from error


def _to_markets(
    markets: list[KalshiMarket], categories: dict[str, str]
) -> list[Market]:
    result: list[Market] = []
    for market in markets:
        # Multivariate collections are combinatorial legs of one parent
        # question. They have no Polymarket counterpart to pair against and
        # they outnumber every other contract type on the venue, so letting
        # them in would swamp the embedding step with unmatchable text.
        if market.mve_collection_ticker or "KXMV" in market.event_ticker:
            continue

        series = market.event_ticker.split("-")[0]
        outcome = market.subtitle or market.yes_sub_title
        result.append(
            Market(
                external_id=market.ticker,
                title=market.title,
                # Kalshi appends a scoping suffix after '::' on some series.
                specific_outcome=outcome.split("::")[0].strip() if outcome else None,
                category=categories.get(series),
                start_date=market.open_time,
                end_date=market.expiration_time,
                # The listing is requested with status=open, so anything that
                # comes back is open by construction.
                status="open",
                execution_data={"ticker": market.ticker},
            )
        )
    return result


def _get(
    client: httpx.Client, path: str, params: dict[str, str], auth: KalshiAuth
) -> bytes:
    last: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        # Signed per attempt: the signature carries a timestamp and Kalshi
        # rejects a replay of one that has aged past its window.
        headers = auth.headers("GET", path)
        try:
            response = client.get(
                BASE_URL + path, params=params, headers=headers, timeout=_TIMEOUT_S
            )
        except httpx.TransportError as error:
            last = error
        else:
            if response.status_code < 400:
                return response.content
            if response.status_code in (401, 403):
                raise SourceError(
                    "Kalshi rejected the signature; check the key id and the "
                    "private key, and that this machine's clock is correct"
                )
            if response.status_code < 500:
                raise SourceError(f"Kalshi responded {response.status_code}")
            last = SourceError(f"Kalshi responded {response.status_code}")
        if attempt < _ATTEMPTS:
            _log.warning("attempt %d failed for %s; retrying", attempt, path)
            time.sleep(float(attempt))

    raise SourceUnavailable(f"Kalshi did not respond in {_ATTEMPTS} attempts") from last
