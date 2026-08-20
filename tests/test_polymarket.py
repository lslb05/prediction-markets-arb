import json
import time
from collections.abc import Callable

import httpx
import pytest

from prediction_markets.errors import SourceError, SourceUnavailable, UnexpectedSchema
from prediction_markets.sources import polymarket

Always = Callable[[int, bytes], tuple[httpx.Client, list[str]]]
MockClient = Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client]


def _one_page(body: bytes) -> Callable[[httpx.Request], httpx.Response]:
    """Serves the fixture as page 0 and an empty page after it."""

    def respond(request: httpx.Request) -> httpx.Response:
        offset = request.url.params.get("offset")
        return httpx.Response(200, content=body if offset == "0" else b"[]")

    return respond


def test_yes_token_comes_from_the_label_not_the_position(
    mock_client: MockClient, gamma_body: bytes
) -> None:
    """Gamma lists the NO token first, so trusting the array order inverts the hedge.

    A hedge placed on the wrong token doesn't fail loudly: it fills, and the
    position is then long both sides of the same event.
    """
    _, markets = polymarket.fetch_markets(
        mock_client(_one_page(gamma_body)), min_volume=100
    )

    fed = next(m for m in markets if m.external_id == "0xfedmarch")
    assert fed.execution_data == {"token_yes": "token-yes", "token_no": "token-no"}


def test_positional_fallback_when_labels_are_absent(
    mock_client: MockClient, gamma_body: bytes
) -> None:
    _, markets = polymarket.fetch_markets(
        mock_client(_one_page(gamma_body)), min_volume=100
    )

    shutdown = next(m for m in markets if m.external_id == "0xshutdown")
    assert shutdown.execution_data == {
        "token_yes": "shut-second",
        "token_no": "shut-first",
    }


def test_resolved_contract_is_not_ingested(
    mock_client: MockClient, gamma_body: bytes
) -> None:
    _, markets = polymarket.fetch_markets(
        mock_client(_one_page(gamma_body)), min_volume=100
    )

    assert "0xexpired" not in {m.external_id for m in markets}


def test_volume_floor_is_applied(mock_client: MockClient, gamma_body: bytes) -> None:
    _, markets = polymarket.fetch_markets(
        mock_client(_one_page(gamma_body)), min_volume=100
    )

    assert "0xthin" not in {m.external_id for m in markets}


def test_outcome_is_dropped_when_it_repeats_the_event_title(
    mock_client: MockClient, gamma_body: bytes
) -> None:
    """Otherwise the embedded text is the same phrase twice, which skews similarity."""
    _, markets = polymarket.fetch_markets(
        mock_client(_one_page(gamma_body)), min_volume=100
    )

    shutdown = next(m for m in markets if m.external_id == "0xshutdown")
    fed = next(m for m in markets if m.external_id == "0xfedmarch")
    assert shutdown.specific_outcome is None
    assert fed.specific_outcome == "Will the Fed cut by 25bps in March 2030?"


def test_category_falls_back_to_the_first_tag(
    mock_client: MockClient, gamma_body: bytes
) -> None:
    _, markets = polymarket.fetch_markets(
        mock_client(_one_page(gamma_body)), min_volume=100
    )

    shutdown = next(m for m in markets if m.external_id == "0xshutdown")
    assert shutdown.category == "Politics"


def test_best_ask_is_the_lowest_not_the_last(mock_client: MockClient) -> None:
    """The CLOB returns asks in descending price order today, and may not tomorrow.

    Taking the last element happens to work now; taking the minimum works
    regardless, and getting this wrong sizes a trade off the wrong end of
    the book.
    """
    book = json.dumps(
        {"asks": [{"price": "0.91"}, {"price": "0.62"}, {"price": "0.55"}]}
    ).encode()
    client, _ = _responding(mock_client, book)

    assert polymarket.fetch_best_ask(client, "token") == pytest.approx(0.55)


def test_empty_ask_side_is_none(mock_client: MockClient) -> None:
    client, _ = _responding(mock_client, b'{"asks": []}')

    assert polymarket.fetch_best_ask(client, "token") is None


def test_book_payload_change_stops_instead_of_guessing(
    mock_client: MockClient,
) -> None:
    client, _ = _responding(mock_client, b'{"asks": [{"px": "0.55"}]}')

    with pytest.raises(UnexpectedSchema):
        polymarket.fetch_best_ask(client, "token")


def test_event_payload_change_stops_ingestion(mock_client: MockClient) -> None:
    """A missing required field is a contract break, not a market to skip."""
    client = mock_client(_one_page(b'[{"slug": "no-title-anymore"}]'))

    with pytest.raises(UnexpectedSchema):
        polymarket.fetch_markets(client, min_volume=0)


def test_client_error_is_not_retried(always: Always) -> None:
    client, seen = always(404, b"")

    with pytest.raises(SourceError, match="404"):
        polymarket.fetch_best_ask(client, "token")
    assert len(seen) == 1


def test_server_error_exhausts_the_attempts(
    always: Always, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _: None)
    client, seen = always(503, b"")

    with pytest.raises(SourceUnavailable):
        polymarket.fetch_best_ask(client, "token")
    assert len(seen) == 3


def _responding(mock_client: MockClient, body: bytes) -> tuple[httpx.Client, list[str]]:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=body)

    return mock_client(respond), seen
