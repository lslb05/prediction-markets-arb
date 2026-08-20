import { beforeEach, describe, expect, it, vi } from "vitest";

import { Engine, type Hedger, type OrderRouter } from "../src/engine";
import type { OrderRequest, PlacedOrder } from "../src/kalshi/api";
import type { Pair } from "../src/types";

const PAIR: Pair = {
  matchedMarketId: 1,
  kalshiTicker: "KXFED-30MAR-C25",
  polyTokenYes: "poly-yes",
  polyTokenNo: "poly-no",
  label: "Fed cuts 25bps",
};

// A ten-lot against a five-contract hedge threshold, so a five-contract fill
// is partial and the rest of the order is still resting -- which is the case
// where getting the accounting wrong strands a live order.
const OPTIONS = { minEdgeBps: 200, orderSize: 10, hedgeThreshold: 5 };

/** 0.40 + 0.02 fee + 0.55 = 0.97, a 300 bps edge on the YES side. */
const TRADEABLE = {
  kalshiYesBid: 0.4,
  kalshiNoBid: null,
  polyYesAsk: null,
  polyNoAsk: 0.55,
};

/** 0.50 + 0.02 + 0.49 = 1.01, below the floor. */
const TOO_THIN = {
  kalshiYesBid: 0.5,
  kalshiNoBid: null,
  polyYesAsk: null,
  polyNoAsk: 0.49,
};

class Router implements OrderRouter {
  readonly created: OrderRequest[] = [];
  readonly cancelled: string[] = [];
  private sequence = 0;
  rejectCreate = false;

  async createOrder(request: OrderRequest): Promise<PlacedOrder> {
    if (this.rejectCreate) {
      throw new Error("venue refused");
    }
    this.created.push(request);
    this.sequence += 1;
    return {
      orderId: `order-${this.sequence}`,
      priceCents: request.priceCents,
      side: request.side,
      count: request.count,
    };
  }

  async cancelOrder(orderId: string): Promise<boolean> {
    this.cancelled.push(orderId);
    return true;
  }
}

class Executor implements Hedger {
  readonly hedges: Array<{ tokenId: string; quantity: number }> = [];
  outcome: "fill" | "reject" | "throw" = "fill";

  async buyHedge(tokenId: string, quantity: number): Promise<boolean> {
    this.hedges.push({ tokenId, quantity });
    if (this.outcome === "throw") {
      throw new Error("network down");
    }
    return this.outcome === "fill";
  }
}

let router: Router;
let executor: Executor;
let engine: Engine;

beforeEach(() => {
  // The engine logs a line per decision; the assertions are about state.
  vi.spyOn(process.stdout, "write").mockReturnValue(true);
  vi.spyOn(process.stderr, "write").mockReturnValue(true);
  router = new Router();
  executor = new Executor();
  engine = new Engine(PAIR, router, executor, OPTIONS);
});

describe("quoting", () => {
  it("joins the best bid instead of crossing it", async () => {
    await engine.onQuotes(TRADEABLE);

    expect(router.created).toEqual([
      { ticker: PAIR.kalshiTicker, side: "yes", priceCents: 40, count: 10 },
    ]);
    expect(engine.state().status).toBe("working");
    expect(engine.state().edgeBps).toBe(300);
  });

  it("stays out when the edge does not clear the floor", async () => {
    await engine.onQuotes(TOO_THIN);

    expect(router.created).toEqual([]);
    expect(engine.state().status).toBe("idle");
  });

  it("pulls the resting order when the edge disappears", async () => {
    await engine.onQuotes(TRADEABLE);
    await engine.onQuotes(TOO_THIN);

    expect(router.cancelled).toEqual(["order-1"]);
    expect(engine.state().activeOrder).toBeNull();
  });

  it("pulls the resting order when a book side empties", async () => {
    await engine.onQuotes(TRADEABLE);
    await engine.onQuotes({ ...TRADEABLE, polyNoAsk: null });

    expect(router.cancelled).toEqual(["order-1"]);
  });

  it("re-quotes on the next tick rather than cancel-and-place in one", async () => {
    await engine.onQuotes(TRADEABLE);
    await engine.onQuotes({ ...TRADEABLE, kalshiYesBid: 0.39 });

    // The cancel happened; the replacement did not, because holding a cancel
    // and a create in flight together is how a duplicate position appears.
    expect(router.cancelled).toEqual(["order-1"]);
    expect(router.created).toHaveLength(1);

    await engine.onQuotes({ ...TRADEABLE, kalshiYesBid: 0.39 });
    expect(router.created).toHaveLength(2);
    expect(router.created[1]?.priceCents).toBe(39);
  });

  it("does not treat a rejected order as resting", async () => {
    router.rejectCreate = true;
    await engine.onQuotes(TRADEABLE);

    expect(engine.state().activeOrder).toBeNull();
    expect(engine.state().status).toBe("idle");
  });
});

describe("hedging", () => {
  const fill = (count: number) => ({
    orderId: "order-1",
    ticker: PAIR.kalshiTicker,
    side: "yes" as const,
    count,
    priceCents: 40,
  });

  it("waits until the threshold before crossing the Polymarket book", async () => {
    await engine.onQuotes(TRADEABLE);
    await engine.onFill(fill(3));

    expect(executor.hedges).toEqual([]);
    expect(engine.state().unhedged).toBe(3);
  });

  it("hedges the opposite token once the threshold is reached", async () => {
    await engine.onQuotes(TRADEABLE);
    await engine.onFill(fill(5));

    // Long Kalshi YES is hedged by buying Polymarket NO: together they settle
    // at exactly $1 whichever way the event resolves.
    expect(executor.hedges).toEqual([{ tokenId: "poly-no", quantity: 5 }]);
    expect(engine.state().unhedged).toBe(0);
  });

  it("ignores a fill for another contract", async () => {
    await engine.onFill({ ...fill(9), ticker: "SOMETHING-ELSE" });

    expect(engine.state().unhedged).toBe(0);
  });

  it("stops and withdraws when the hedge is rejected", async () => {
    executor.outcome = "reject";
    await engine.onQuotes(TRADEABLE);
    await engine.onFill(fill(5));

    // An unhedged Kalshi position is an outright bet, not the strategy.
    expect(engine.state().status).toBe("stopped");
    expect(router.cancelled).toEqual(["order-1"]);
    expect(engine.state().unhedged).toBe(5);
  });

  it("does not quote again after stopping", async () => {
    executor.outcome = "reject";
    await engine.onQuotes(TRADEABLE);
    await engine.onFill(fill(5));
    await engine.onQuotes(TRADEABLE);

    expect(router.created).toHaveLength(1);
  });

  it("can hedge again after a failure once it is restarted", async () => {
    // Regression: the original left its in-flight flag set when the hedge
    // threw, which silently disabled every later hedge for the life of the
    // process. Here a fresh engine after the same failure still hedges.
    executor.outcome = "throw";
    await engine.onQuotes(TRADEABLE);
    await engine.onFill(fill(5));
    expect(engine.state().status).toBe("stopped");

    executor.outcome = "fill";
    const restarted = new Engine(PAIR, router, executor, OPTIONS);
    await restarted.onQuotes(TRADEABLE);
    await restarted.onFill(fill(5));

    expect(executor.hedges).toHaveLength(2);
    expect(restarted.state().unhedged).toBe(0);
  });
});
