"""Polymarket adapter: the Gamma catalogue and the CLOB order book.

Two different services with two different jobs. Gamma lists what exists and
is the ingestion source; the CLOB holds the live book and is what the
arbitrage scan prices against. They are here in one module because they are
one venue and change together.
"""

import json
import logging
import time
from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from prediction_markets.contracts import Market, Status
from prediction_markets.errors import SourceError, SourceUnavailable, UnexpectedSchema

GAMMA_URL = "https://gamma-api.polymarket.com/events"
CLOB_BOOK_URL = "https://clob.polymarket.com/book"
CONTRACT_VERSION = "polymarket.gamma.events.v1"

_PAGE_SIZE = 100
_MAX_PAGES = 100
_ATTEMPTS = 3
_TIMEOUT_S = 30.0

_log = logging.getLogger(__name__)


class GammaMarket(BaseModel):
    """One binary contract inside a Gamma event.

    `extra="ignore"` here, unlike the strict contracts elsewhere in the
    portfolio: Gamma returns several dozen presentation fields and adds to
    them regularly, so forbidding extras would turn a copy change on
    Polymarket's website into an ingestion outage. What must not drift
    silently are the fields below, and those are required.
    """

    model_config = ConfigDict(extra="ignore")

    condition_id: str = Field(alias="conditionId")
    question: str
    end_date: datetime | None = Field(default=None, alias="endDate")
    start_date: datetime | None = Field(default=None, alias="startDate")
    volume: float = 0.0
    closed: bool = False
    active: bool = True
    # Gamma serialises these two as JSON-encoded strings inside the JSON
    # payload, not as arrays.
    clob_token_ids: str | None = Field(default=None, alias="clobTokenIds")
    outcomes: str | None = None


class GammaEvent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    category: str | None = None
    tags: list[dict[str, object]] = Field(default_factory=list)
    markets: list[GammaMarket] = Field(default_factory=list)


def fetch_markets(
    client: httpx.Client, *, min_volume: float
) -> tuple[bytes, list[Market]]:
    """Returns the raw catalogue and the markets worth tracking.

    Paged sequentially rather than concurrently. At one request per page and
    a daily schedule the wall-clock saving from parallelism is under a
    minute, and it would cost an event loop, a Windows loop-policy
    workaround, and interleaving that no test can pin down. Revisit if the
    catalogue grows past what a single ingestion window can absorb.
    """
    pages: list[list[object]] = []
    for page in range(_MAX_PAGES):
        body = _get(
            client,
            GAMMA_URL,
            {
                "active": "true",
                "limit": str(_PAGE_SIZE),
                "offset": str(page * _PAGE_SIZE),
                "order": "volume",
                "ascending": "false",
            },
        )
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as error:
            raise UnexpectedSchema("Gamma returned a body that is not JSON") from error
        if not decoded:
            break
        pages.append(decoded)

    markets = [
        market
        for page in pages
        for event in _parse(page)
        for market in _to_markets(event, min_volume=min_volume)
    ]
    # The raw zone stores the concatenation of the pages, so what is on disk
    # is the whole catalogue as of one run rather than an arbitrary slice.
    return json.dumps(pages).encode("utf-8"), markets


def fetch_best_ask(client: httpx.Client, token_id: str) -> float | None:
    """Lowest ask for a CLOB token, or None when that side of the book is empty."""
    body = _get(client, CLOB_BOOK_URL, {"token_id": token_id})
    try:
        asks = json.loads(body).get("asks") or []
        prices = [float(level["price"]) for level in asks]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise UnexpectedSchema(f"CLOB book payload changed for {token_id}") from error
    # Sorted explicitly instead of indexing the end of the list: the CLOB
    # happens to return asks in descending price order today, and a trade
    # sized off the wrong end of the book is not an error anyone would catch.
    return min(prices, default=None)


def _parse(page: list[object]) -> list[GammaEvent]:
    try:
        return [GammaEvent.model_validate(event) for event in page]
    except ValidationError as error:
        raise UnexpectedSchema("Gamma event payload's shape changed") from error


def _to_markets(event: GammaEvent, *, min_volume: float) -> list[Market]:
    now = datetime.now(UTC)
    category = _category(event)
    result: list[Market] = []

    for market in event.markets:
        if market.volume < min_volume:
            continue
        # A contract that already resolved cannot be a leg of a live spread,
        # and its book is empty, so it would only ever produce phantom edges.
        if market.end_date is None or market.end_date < now:
            continue

        tokens = _tokens(market)
        if tokens is None:
            continue

        yes_token, no_token = tokens
        result.append(
            Market(
                external_id=market.condition_id,
                title=event.title,
                # Gamma repeats the event title as the question on a
                # single-outcome event; keeping both would make the embedded
                # text a duplicated phrase and skew every similarity score.
                specific_outcome=(
                    None if market.question == event.title else market.question
                ),
                category=category,
                start_date=market.start_date,
                end_date=market.end_date,
                status=_status(market),
                execution_data={"token_yes": yes_token, "token_no": no_token},
            )
        )
    return result


def _tokens(market: GammaMarket) -> tuple[str, str] | None:
    """Maps outcome labels to CLOB token ids, or None if the pair is unusable.

    The labels are the authority. Positional order is a fallback only, and
    it is reversed from what reading the array suggests: Gamma lists the NO
    token first.
    """
    ids = _decode_list(market.clob_token_ids)
    labels = _decode_list(market.outcomes)
    if len(ids) != 2:
        return None

    # Only trust the labels when they line up one-to-one with the ids; a
    # mismatched pair of arrays would map YES onto whichever token happened
    # to share an index, which is how a hedge ends up on the wrong side.
    by_label = (
        {
            str(label).strip().lower(): token
            for label, token in zip(labels, ids, strict=True)
        }
        if len(labels) == len(ids)
        else {}
    )
    yes_token = by_label.get("yes", ids[1])
    no_token = by_label.get("no", ids[0])
    if not yes_token or not no_token or yes_token == no_token:
        return None
    return yes_token, no_token


def _decode_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in decoded] if isinstance(decoded, list) else []


def _status(market: GammaMarket) -> Status:
    if market.closed:
        return "closed"
    return "open" if market.active else "inactive"


def _category(event: GammaEvent) -> str | None:
    if event.category:
        return event.category
    for tag in event.tags:
        label = tag.get("label")
        if isinstance(label, str) and label:
            return label
    return None


def _get(client: httpx.Client, url: str, params: dict[str, str]) -> bytes:
    last: Exception | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            response = client.get(url, params=params, timeout=_TIMEOUT_S)
        except httpx.TransportError as error:
            last = error
        else:
            if response.status_code < 400:
                return response.content
            # A 4xx is our fault -- a malformed token id, a bad parameter.
            # Retrying cannot fix it and still spends the rate limit.
            if response.status_code < 500:
                raise SourceError(f"Polymarket responded {response.status_code}")
            last = SourceError(f"Polymarket responded {response.status_code}")
        if attempt < _ATTEMPTS:
            _log.warning("attempt %d failed for %s; retrying", attempt, url)
            time.sleep(float(attempt))

    raise SourceUnavailable(
        f"Polymarket did not respond in {_ATTEMPTS} attempts"
    ) from last
