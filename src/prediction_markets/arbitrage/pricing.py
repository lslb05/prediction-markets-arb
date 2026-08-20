"""What a cross-venue pair is worth, as pure arithmetic.

No HTTP, no database. Everything that decides whether capital should move
lives here so it can be tested against hand-computed numbers.

The trade is a box across two venues. A Kalshi contract and a Polymarket
contract that resolve on the same event settle at $1 and $0 in opposite
cases, so holding Kalshi YES together with Polymarket NO pays exactly $1
whichever way the event goes. If the two legs together cost less than $1,
the difference is locked in at entry and does not depend on the outcome.
"""

import math
from dataclasses import dataclass
from typing import Literal

Strategy = Literal["kalshi_yes", "kalshi_no"]

# Kalshi's published trading fee: 0.07 x contracts x price x (1 - price),
# rounded up to the next cent. It peaks at the middle of the range, which is
# exactly where these pairs trade, so ignoring it overstates the edge by up
# to 1.75c per contract -- more than most of the spreads worth chasing.
_KALSHI_FEE_RATE = 0.07

# Polymarket's CLOB charges no maker or taker fee, so the Polymarket leg
# costs its ask and nothing more. If that ever changes it becomes a second
# term here, not a fudge factor somewhere upstream.

_SETTLEMENT = 1.0


@dataclass(frozen=True, slots=True)
class Opportunity:
    """One evaluated leg pairing, whether or not it is profitable.

    `edge_bps` is basis points of the $1 settlement: 100 bps is one cent per
    contract. It is negative when the pair costs more than it settles for,
    which is the normal case and is recorded rather than discarded.
    """

    strategy: Strategy
    kalshi_bid: float
    polymarket_ask: float
    total_cost: float
    edge_bps: int

    @property
    def is_profitable(self) -> bool:
        return self.edge_bps > 0


def kalshi_fee(price: float, contracts: int = 1) -> float:
    """Kalshi's fee for `contracts` at `price`, in dollars, rounded up to the cent."""
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price outside [0, 1]: {price}")
    exact_cents = _KALSHI_FEE_RATE * contracts * price * (1.0 - price) * 100.0
    # Rounded before the ceiling: 0.07 * 100 * 0.5 * 0.5 is 175 in decimal and
    # 175.00000000000003 in binary floating point, and a bare ceiling would
    # bill a whole extra cent on every exactly-representable fee.
    return math.ceil(round(exact_cents, 9)) / 100.0


def evaluate(
    *,
    kalshi_yes_bid: float | None,
    kalshi_no_bid: float | None,
    polymarket_yes_ask: float | None,
    polymarket_no_ask: float | None,
) -> list[Opportunity]:
    """Prices both directions of the box. Legs without a quote are skipped.

    A missing quote means one side of the book is empty, which is common on
    thin contracts. That is not an error and not a zero: an absent leg is
    simply not tradeable, and pricing it as 0 would manufacture a spread out
    of an empty book -- the failure mode most likely to move real money.

    `kalshi_no_bid` is the best bid on the NO side of Kalshi's book, not one
    minus the YES bid: the two sides quote independently and the gap between
    them is the venue's spread.
    """
    legs: list[Opportunity] = []
    if kalshi_yes_bid is not None and polymarket_no_ask is not None:
        legs.append(_price("kalshi_yes", kalshi_yes_bid, polymarket_no_ask))
    if kalshi_no_bid is not None and polymarket_yes_ask is not None:
        legs.append(_price("kalshi_no", kalshi_no_bid, polymarket_yes_ask))
    return legs


def best(opportunities: list[Opportunity]) -> Opportunity | None:
    """The widest edge among the evaluated legs."""
    return max(opportunities, key=lambda o: o.edge_bps, default=None)


def _price(strategy: Strategy, kalshi_bid: float, polymarket_ask: float) -> Opportunity:
    for label, price in (
        ("kalshi bid", kalshi_bid),
        ("polymarket ask", polymarket_ask),
    ):
        if not 0.0 <= price <= 1.0:
            raise ValueError(f"{label} outside [0, 1]: {price}")

    total = kalshi_bid + kalshi_fee(kalshi_bid) + polymarket_ask
    return Opportunity(
        strategy=strategy,
        kalshi_bid=kalshi_bid,
        polymarket_ask=polymarket_ask,
        total_cost=total,
        edge_bps=round((_SETTLEMENT - total) * 10_000),
    )
