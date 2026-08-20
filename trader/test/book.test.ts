import { describe, expect, it } from "vitest";

import { KalshiBook } from "../src/kalshi/book";
import { parseFill } from "../src/kalshi/fills";

const TICKER = "KXFED-30MAR-C25";

const snapshot = (yes: number[][], no: number[][]) => ({
  type: "orderbook_snapshot",
  msg: { market_ticker: TICKER, yes, no },
});

const delta = (side: "yes" | "no", price: number, change: number) => ({
  type: "orderbook_delta",
  msg: { market_ticker: TICKER, side, price, delta: change },
});

describe("kalshi book", () => {
  it("reports both sides as independent bids", () => {
    const book = new KalshiBook(TICKER);
    book.apply(
      snapshot(
        [
          [40, 100],
          [39, 250],
        ],
        [[57, 80]],
      ),
    );

    // 0.40 and 0.57 sum to 0.97, not 1.00: the gap is the venue's spread,
    // which is why neither side is derived from the other.
    expect(book.bestBid("yes")).toBeCloseTo(0.4, 10);
    expect(book.bestBid("no")).toBeCloseTo(0.57, 10);
  });

  it("has no best bid when the side is empty", () => {
    const book = new KalshiBook(TICKER);
    book.apply(snapshot([], [[57, 80]]));

    expect(book.bestBid("yes")).toBeNull();
  });

  it("applies a delta on top of the snapshot", () => {
    const book = new KalshiBook(TICKER);
    book.apply(snapshot([[40, 100]], []));
    book.apply(delta("yes", 41, 30));

    expect(book.bestBid("yes")).toBeCloseTo(0.41, 10);
    expect(book.side("yes")).toEqual([
      { price: 0.41, size: 30 },
      { price: 0.4, size: 100 },
    ]);
  });

  it("removes a level the delta empties", () => {
    const book = new KalshiBook(TICKER);
    book.apply(snapshot([[40, 100]], []));
    book.apply(delta("yes", 40, -100));

    expect(book.bestBid("yes")).toBeNull();
  });

  it("a snapshot replaces the book rather than merging into it", () => {
    const book = new KalshiBook(TICKER);
    book.apply(snapshot([[40, 100]], []));
    book.apply(snapshot([[35, 10]], []));

    expect(book.side("yes")).toEqual([{ price: 0.35, size: 10 }]);
  });

  it("ignores a message for a different contract", () => {
    const book = new KalshiBook(TICKER);
    const foreign = { ...snapshot([[90, 1]], []), msg: { market_ticker: "OTHER" } };

    expect(book.apply(foreign)).toBe(false);
    expect(book.bestBid("yes")).toBeNull();
  });

  it("keeps only the top of book", () => {
    const book = new KalshiBook(TICKER);
    book.apply(
      snapshot(
        [
          [40, 1],
          [39, 1],
          [38, 1],
          [37, 1],
        ],
        [],
      ),
    );

    expect(book.side("yes")).toHaveLength(3);
  });
});

describe("fills", () => {
  const tickers = new Set([TICKER]);

  it("reads a fill for a contract the bot trades", () => {
    const fill = parseFill(
      {
        type: "fill",
        msg: {
          market_ticker: TICKER,
          order_id: "order-1",
          side: "yes",
          count: 5,
          yes_price: 40,
        },
      },
      tickers,
    );

    expect(fill).toEqual({
      orderId: "order-1",
      ticker: TICKER,
      side: "yes",
      count: 5,
      priceCents: 40,
    });
  });

  it("ignores a fill for a contract the bot does not trade", () => {
    const fill = parseFill(
      {
        type: "fill",
        msg: { market_ticker: "OTHER", order_id: "x", side: "yes", count: 5 },
      },
      tickers,
    );

    expect(fill).toBeNull();
  });

  it("ignores a malformed fill rather than arming a hedge from it", () => {
    const fill = parseFill(
      { type: "fill", msg: { market_ticker: TICKER, order_id: "x", side: "yes" } },
      tickers,
    );

    expect(fill).toBeNull();
  });

  it("ignores anything that is not a fill", () => {
    expect(parseFill({ type: "orderbook_delta", msg: {} }, tickers)).toBeNull();
    expect(parseFill(null, tickers)).toBeNull();
  });
});
