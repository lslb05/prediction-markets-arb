import pytest

from prediction_markets.arbitrage import pricing


def test_box_that_settles_below_one_has_positive_edge() -> None:
    # 0.40 on Kalshi YES, fee 0.07*0.40*0.60 = 0.0168 -> 0.02 rounded up,
    # 0.55 on Polymarket NO. Total 0.97, so 3c of the $1 settlement is left.
    (leg,) = pricing.evaluate(
        kalshi_yes_bid=0.40,
        kalshi_no_bid=None,
        polymarket_yes_ask=None,
        polymarket_no_ask=0.55,
    )

    assert leg.total_cost == pytest.approx(0.97)
    assert leg.edge_bps == 300
    assert leg.is_profitable


def test_fee_alone_turns_a_thin_spread_negative() -> None:
    """The reason the fee is modelled at all.

    Half a cent of raw spread looks tradeable and is not: at the middle of
    the price range Kalshi's fee is nearly two cents per contract, which is
    what the original screen missed.
    """
    (leg,) = pricing.evaluate(
        kalshi_yes_bid=0.50,
        kalshi_no_bid=None,
        polymarket_yes_ask=None,
        polymarket_no_ask=0.495,
    )

    assert leg.edge_bps == -150
    assert not leg.is_profitable


def test_fee_rounds_up_to_the_cent() -> None:
    # 0.07 * 0.10 * 0.90 = 0.0063, which the venue bills as a full cent.
    assert pricing.kalshi_fee(0.10) == pytest.approx(0.01)


def test_fee_scales_with_contracts() -> None:
    assert pricing.kalshi_fee(0.50, contracts=100) == pytest.approx(1.75)


def test_empty_book_side_produces_no_leg() -> None:
    """An absent quote must not be priced as zero.

    Reading a missing bid as 0.00 manufactures a spread out of an empty
    book, which is the failure mode most likely to send real money at
    nothing.
    """
    assert (
        pricing.evaluate(
            kalshi_yes_bid=None,
            kalshi_no_bid=None,
            polymarket_yes_ask=0.40,
            polymarket_no_ask=0.55,
        )
        == []
    )


def test_both_directions_are_priced_independently() -> None:
    legs = pricing.evaluate(
        kalshi_yes_bid=0.40,
        kalshi_no_bid=0.30,
        polymarket_yes_ask=0.60,
        polymarket_no_ask=0.55,
    )

    assert [leg.strategy for leg in legs] == ["kalshi_yes", "kalshi_no"]
    # kalshi_yes costs 0.40 + 0.02 + 0.55; kalshi_no costs 0.30 + 0.02 + 0.60.
    # The NO side is five cents cheaper even though its bid is lower, which is
    # why both are priced instead of inferring one from the other.
    best = pricing.best(legs)
    assert best is not None
    assert best.strategy == "kalshi_no"
    assert best.edge_bps == 800


def test_best_of_nothing_is_nothing() -> None:
    assert pricing.best([]) is None


@pytest.mark.parametrize("price", [-0.01, 1.01])
def test_price_outside_the_settlement_range_is_rejected(price: float) -> None:
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        pricing.evaluate(
            kalshi_yes_bid=price,
            kalshi_no_bid=None,
            polymarket_yes_ask=None,
            polymarket_no_ask=0.5,
        )
