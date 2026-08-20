import { describe, expect, it } from "vitest";

import { best, evaluate, kalshiFee } from "../src/strategy/spread";

const NO_QUOTES = {
  kalshiYesBid: null,
  kalshiNoBid: null,
  polymarketYesAsk: null,
  polymarketNoAsk: null,
};

/**
 * These are the same cases, with the same hand-computed numbers, as
 * `tests/test_pricing.py`. They exist twice on purpose: the two
 * implementations are the contract between what the research half records
 * and what the bot acts on, and a silent divergence between them is the one
 * bug that costs money without failing anything.
 */
describe("cross-venue box", () => {
  it("locks in the gap when both legs cost less than the settlement", () => {
    // 0.40 on Kalshi YES, fee 0.07*0.40*0.60 = 0.0168 -> 0.02 rounded up,
    // 0.55 on Polymarket NO. Total 0.97, leaving 3c of the $1 settlement.
    const [leg] = evaluate({ ...NO_QUOTES, kalshiYesBid: 0.4, polymarketNoAsk: 0.55 });

    expect(leg?.totalCost).toBeCloseTo(0.97, 10);
    expect(leg?.edgeBps).toBe(300);
  });

  it("turns a thin spread negative once the fee is charged", () => {
    const [leg] = evaluate({
      ...NO_QUOTES,
      kalshiYesBid: 0.5,
      polymarketNoAsk: 0.495,
    });

    expect(leg?.edgeBps).toBe(-150);
  });

  it("prices both directions independently", () => {
    const legs = evaluate({
      kalshiYesBid: 0.4,
      kalshiNoBid: 0.3,
      polymarketYesAsk: 0.6,
      polymarketNoAsk: 0.55,
    });

    // kalshi_no costs 0.30 + 0.02 + 0.60; kalshi_yes costs 0.40 + 0.02 + 0.55.
    // The NO side wins despite the lower bid, which is why neither side is
    // inferred from the other.
    expect(legs.map((leg) => leg.strategy)).toEqual(["kalshi_yes", "kalshi_no"]);
    expect(best(legs)?.strategy).toBe("kalshi_no");
    expect(best(legs)?.edgeBps).toBe(800);
  });

  it("does not price a leg whose book side is empty", () => {
    expect(
      evaluate({ ...NO_QUOTES, polymarketYesAsk: 0.4, polymarketNoAsk: 0.55 }),
    ).toEqual([]);
  });

  it("has no best leg when nothing was priced", () => {
    expect(best([])).toBeNull();
  });

  it.each([-0.01, 1.01])("rejects a price outside the settlement range: %s", (bad) => {
    expect(() =>
      evaluate({ ...NO_QUOTES, kalshiYesBid: bad, polymarketNoAsk: 0.5 }),
    ).toThrow(RangeError);
  });
});

describe("kalshi fee", () => {
  it("rounds up to the whole cent", () => {
    // 0.07 * 0.10 * 0.90 = 0.0063, which the venue bills as a full cent.
    expect(kalshiFee(0.1)).toBeCloseTo(0.01, 10);
  });

  it("does not overcharge a fee that is exact in decimal", () => {
    expect(kalshiFee(0.5, 100)).toBeCloseTo(1.75, 10);
  });
});
