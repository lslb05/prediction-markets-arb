/**
 * What a cross-venue pair is worth, as pure arithmetic.
 *
 * This is the TypeScript half of a contract that also exists in
 * `src/prediction_markets/arbitrage/pricing.py`. The two must agree: the
 * Python side decides what gets recorded and reported, this side decides
 * what gets ordered, and a disagreement means the bot chases an edge the
 * research half never saw. `test/spread.test.ts` asserts the same
 * hand-computed numbers as `tests/test_pricing.py`, which is what keeps them
 * honest.
 *
 * Why two implementations at all: the scanner runs on a fifteen-minute
 * schedule inside Airflow, the bot reacts to websocket ticks in
 * milliseconds. Reaching across a process boundary per tick would cost more
 * than the arithmetic it replaces.
 */

export type Strategy = "kalshi_yes" | "kalshi_no";

/** Kalshi's published fee: 0.07 x contracts x price x (1 - price), rounded up to the cent. */
const KALSHI_FEE_RATE = 0.07;

const SETTLEMENT = 1.0;

export interface Opportunity {
  readonly strategy: Strategy;
  readonly kalshiBid: number;
  readonly polymarketAsk: number;
  readonly totalCost: number;
  /** Basis points of the $1 settlement. Negative when the box costs more than it pays. */
  readonly edgeBps: number;
}

export function kalshiFee(price: number, contracts = 1): number {
  if (!(price >= 0 && price <= 1)) {
    throw new RangeError(`price outside [0, 1]: ${price}`);
  }
  const exactCents = KALSHI_FEE_RATE * contracts * price * (1 - price) * 100;
  // Rounded before the ceiling: 0.07 * 100 * 0.5 * 0.5 is 175 in decimal and
  // 175.00000000000003 in binary floating point, and a bare ceiling would
  // bill a whole extra cent on every exactly-representable fee.
  return Math.ceil(Number(exactCents.toFixed(9))) / 100;
}

export interface Quotes {
  readonly kalshiYesBid: number | null;
  readonly kalshiNoBid: number | null;
  readonly polymarketYesAsk: number | null;
  readonly polymarketNoAsk: number | null;
}

/**
 * Prices both directions of the box. A leg without a quote is skipped.
 *
 * An absent quote means one side of the book is empty. Treating it as 0
 * manufactures a spread out of nothing, which is the failure mode most
 * likely to route real money at an empty book.
 */
export function evaluate(quotes: Quotes): Opportunity[] {
  const legs: Opportunity[] = [];
  if (quotes.kalshiYesBid !== null && quotes.polymarketNoAsk !== null) {
    legs.push(price("kalshi_yes", quotes.kalshiYesBid, quotes.polymarketNoAsk));
  }
  if (quotes.kalshiNoBid !== null && quotes.polymarketYesAsk !== null) {
    legs.push(price("kalshi_no", quotes.kalshiNoBid, quotes.polymarketYesAsk));
  }
  return legs;
}

export function best(legs: readonly Opportunity[]): Opportunity | null {
  return legs.reduce<Opportunity | null>(
    (widest, leg) => (widest === null || leg.edgeBps > widest.edgeBps ? leg : widest),
    null,
  );
}

function price(
  strategy: Strategy,
  kalshiBid: number,
  polymarketAsk: number,
): Opportunity {
  for (const [label, value] of [
    ["kalshi bid", kalshiBid],
    ["polymarket ask", polymarketAsk],
  ] as const) {
    if (!(value >= 0 && value <= 1)) {
      throw new RangeError(`${label} outside [0, 1]: ${value}`);
    }
  }

  const totalCost = kalshiBid + kalshiFee(kalshiBid) + polymarketAsk;
  return {
    strategy,
    kalshiBid,
    polymarketAsk,
    totalCost,
    edgeBps: Math.round((SETTLEMENT - totalCost) * 10_000),
  };
}
