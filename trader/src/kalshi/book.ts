import type { BookSide, Level } from "../types";

const DEPTH = 3;

/**
 * Rebuilds one contract's book from Kalshi's snapshot and delta messages.
 *
 * Kalshi quotes both sides as bids: `yes` is the best price someone will pay
 * for YES, `no` the best price someone will pay for NO. They are not
 * complements -- the gap between them is the venue's spread -- so both are
 * tracked separately and neither is derived from the other.
 *
 * Prices arrive in whole cents and are kept that way internally; converting
 * once at the edge avoids accumulating float error across thousands of
 * deltas.
 */
export class KalshiBook {
  private readonly yesBids = new Map<number, number>();
  private readonly noBids = new Map<number, number>();

  constructor(private readonly ticker: string) {}

  /** Returns true when the message changed this contract's book. */
  apply(message: unknown): boolean {
    if (!isBookMessage(message) || message.msg.market_ticker !== this.ticker) {
      return false;
    }

    if (message.type === "orderbook_snapshot") {
      this.yesBids.clear();
      this.noBids.clear();
      load(this.yesBids, message.msg.yes);
      load(this.noBids, message.msg.no);
      return true;
    }

    if (message.type === "orderbook_delta") {
      const { price, delta, side } = message.msg;
      if (typeof price !== "number" || typeof delta !== "number") {
        return false;
      }
      const book = side === "yes" ? this.yesBids : this.noBids;
      const quantity = (book.get(price) ?? 0) + delta;
      if (quantity > 0) {
        book.set(price, quantity);
      } else {
        book.delete(price);
      }
      return true;
    }

    return false;
  }

  side(which: "yes" | "no"): BookSide {
    const book = which === "yes" ? this.yesBids : this.noBids;
    return [...book.entries()]
      .sort(([a], [b]) => b - a)
      .slice(0, DEPTH)
      .map(([priceCents, size]): Level => ({ price: priceCents / 100, size }));
  }

  /** Best bid in dollars, or null when that side of the book is empty. */
  bestBid(which: "yes" | "no"): number | null {
    return this.side(which)[0]?.price ?? null;
  }
}

interface BookMessage {
  type: string;
  msg: {
    market_ticker?: string;
    yes?: unknown;
    no?: unknown;
    price?: unknown;
    delta?: unknown;
    side?: unknown;
  };
}

function isBookMessage(message: unknown): message is BookMessage {
  return (
    typeof message === "object" &&
    message !== null &&
    "type" in message &&
    "msg" in message &&
    typeof (message as BookMessage).msg === "object" &&
    (message as BookMessage).msg !== null
  );
}

function load(book: Map<number, number>, levels: unknown): void {
  if (!Array.isArray(levels)) {
    return;
  }
  for (const level of levels) {
    if (Array.isArray(level) && typeof level[0] === "number" && typeof level[1] === "number") {
      book.set(level[0], level[1]);
    }
  }
}
