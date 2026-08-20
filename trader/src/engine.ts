import type { OrderRequest, PlacedOrder, Side } from "./kalshi/api";
import { log } from "./logger";
import { best, evaluate, type Opportunity } from "./strategy/spread";
import type { Fill, Pair } from "./types";

/**
 * What the engine needs from Kalshi and from Polymarket, and nothing more.
 *
 * Narrowed to these two shapes so the state machine can be driven against
 * doubles in a test: the real classes satisfy them structurally, and the
 * live/dry-run switch stays where it belongs, inside the implementations.
 */
export interface OrderRouter {
  createOrder(request: OrderRequest): Promise<PlacedOrder>;
  cancelOrder(orderId: string): Promise<boolean>;
}

export interface Hedger {
  buyHedge(tokenId: string, quantity: number): Promise<boolean>;
}

export type Status = "idle" | "working" | "hedging" | "stopped";

export interface EngineState {
  readonly status: Status;
  readonly edgeBps: number;
  readonly targetSide: Side | null;
  readonly activeOrder: PlacedOrder | null;
  readonly unhedged: number;
}

export interface Quotes {
  readonly kalshiYesBid: number | null;
  readonly kalshiNoBid: number | null;
  readonly polyYesAsk: number | null;
  readonly polyNoAsk: number | null;
}

export interface EngineOptions {
  readonly minEdgeBps: number;
  readonly orderSize: number;
  /** Unhedged contracts tolerated before the Polymarket leg is forced. */
  readonly hedgeThreshold: number;
}

/**
 * One market pair's state machine: quote passively on Kalshi, hedge on Polymarket.
 *
 * The strategy is to join the best Kalshi bid rather than cross it. Crossing
 * pays the spread and the edge here is a few cents wide, so a taker order
 * eats most of it before the hedge is even placed. Joining means the fill is
 * uncertain, which is why the hedge is asynchronous: the position exists
 * from the moment Kalshi reports a fill, and closing it is a separate race
 * against the Polymarket book.
 *
 * The engine holds no timers and does no I/O scheduling of its own. It reacts
 * to ticks and fills the orchestrator hands it, which is what makes its
 * decisions reproducible.
 */
export class Engine {
  private status: Status = "idle";
  private activeOrder: PlacedOrder | null = null;
  private targetSide: Side | null = null;
  private hedgeToken: string | null = null;
  private edgeBps = 0;

  /** Contracts filled on Kalshi that Polymarket has not offset yet. */
  private unhedged = 0;
  private hedging = false;

  constructor(
    readonly pair: Pair,
    private readonly kalshi: OrderRouter,
    private readonly polymarket: Hedger,
    private readonly options: EngineOptions,
  ) {}

  async onQuotes(quotes: Quotes): Promise<void> {
    if (this.status === "stopped") {
      return;
    }

    const leg = best(
      evaluate({
        kalshiYesBid: quotes.kalshiYesBid,
        kalshiNoBid: quotes.kalshiNoBid,
        polymarketYesAsk: quotes.polyYesAsk,
        polymarketNoAsk: quotes.polyNoAsk,
      }),
    );

    if (leg === null) {
      // One of the books is empty. Standing in a market nobody is quoting
      // is a position waiting for someone to pick off.
      await this.withdraw("book_empty");
      return;
    }

    this.edgeBps = leg.edgeBps;
    this.targetSide = sideOf(leg);
    this.hedgeToken =
      leg.strategy === "kalshi_yes" ? this.pair.polyTokenNo : this.pair.polyTokenYes;

    if (leg.edgeBps < this.options.minEdgeBps) {
      await this.withdraw("edge_too_thin");
      return;
    }

    const targetCents = Math.round(leg.kalshiBid * 100);
    if (this.activeOrder === null) {
      await this.quote(targetCents);
    } else if (
      this.activeOrder.priceCents !== targetCents ||
      this.activeOrder.side !== this.targetSide
    ) {
      // The book moved under the resting order. Cancel now and re-quote on
      // the next tick rather than in the same one: cancel and create are two
      // round trips, and holding both in flight is how a duplicate position
      // appears.
      await this.withdraw("price_moved");
    }

    await this.hedgeIfNeeded();
  }

  async onFill(fill: Fill): Promise<void> {
    if (fill.ticker !== this.pair.kalshiTicker) {
      return;
    }
    this.unhedged += fill.count;
    log.info("fill", {
      pair: this.pair.label,
      ticker: fill.ticker,
      side: fill.side,
      count: fill.count,
      unhedged: this.unhedged,
    });

    const order = this.activeOrder;
    if (order?.orderId === fill.orderId) {
      // A partial fill leaves the rest of the order resting on the venue.
      // Forgetting it here would strand it: nothing would cancel it on
      // shutdown, and it would keep trading with no hedge behind it.
      const remaining = order.count - fill.count;
      if (remaining > 0) {
        this.activeOrder = { ...order, count: remaining };
      } else {
        this.activeOrder = null;
        this.status = "idle";
      }
    }
    await this.hedgeIfNeeded();
  }

  /** Cancels any resting order. Called on shutdown and on a hedge failure. */
  async withdraw(reason: string): Promise<void> {
    const order = this.activeOrder;
    if (order === null) {
      return;
    }
    try {
      await this.kalshi.cancelOrder(order.orderId);
      this.activeOrder = null;
      if (this.status !== "stopped") {
        this.status = "idle";
      }
      log.info("order.cancelled", {
        pair: this.pair.label,
        order_id: order.orderId,
        reason,
      });
    } catch (error) {
      // The order is still live and still ours. Keeping it in state means
      // the next tick, and the shutdown path, will try again.
      log.error("order.cancel_failed", {
        pair: this.pair.label,
        order_id: order.orderId,
        message: messageOf(error),
      });
    }
  }

  state(): EngineState {
    return {
      status: this.status,
      edgeBps: this.edgeBps,
      targetSide: this.targetSide,
      activeOrder: this.activeOrder,
      unhedged: this.unhedged,
    };
  }

  private async quote(priceCents: number): Promise<void> {
    if (this.targetSide === null || priceCents <= 0) {
      return;
    }
    try {
      this.activeOrder = await this.kalshi.createOrder({
        ticker: this.pair.kalshiTicker,
        side: this.targetSide,
        priceCents,
        count: this.options.orderSize,
      });
      this.status = "working";
      log.info("order.placed", {
        pair: this.pair.label,
        side: this.targetSide,
        price_cents: priceCents,
        count: this.options.orderSize,
        edge_bps: this.edgeBps,
      });
    } catch (error) {
      this.status = "idle";
      log.error("order.rejected", {
        pair: this.pair.label,
        message: messageOf(error),
      });
    }
  }

  private async hedgeIfNeeded(): Promise<void> {
    if (
      this.hedging ||
      this.hedgeToken === null ||
      this.unhedged < this.options.hedgeThreshold
    ) {
      return;
    }

    const quantity = Math.floor(this.unhedged);
    const token = this.hedgeToken;
    this.hedging = true;
    this.status = "hedging";

    try {
      const filled = await this.polymarket.buyHedge(token, quantity);
      if (!filled) {
        throw new Error("Polymarket rejected the fill-or-kill hedge");
      }
      this.unhedged -= quantity;
      this.status = this.activeOrder === null ? "idle" : "working";
      log.info("hedge.filled", {
        pair: this.pair.label,
        quantity,
        unhedged: this.unhedged,
      });
    } catch (error) {
      // An unhedged Kalshi position is an outright directional bet, which is
      // not the strategy. Stop quoting, pull what is resting, and leave the
      // position for a human: unwinding it automatically at whatever the
      // book offers is how a small loss becomes a large one.
      this.status = "stopped";
      log.error("hedge.failed", {
        pair: this.pair.label,
        quantity,
        unhedged: this.unhedged,
        message: messageOf(error),
      });
      await this.withdraw("hedge_failed");
    } finally {
      // In a `finally` on purpose: leaving this set after a failure was a
      // real bug in the original, and it silently disabled every later hedge
      // attempt for the life of the process.
      this.hedging = false;
    }
  }
}

function sideOf(leg: Opportunity): Side {
  return leg.strategy === "kalshi_yes" ? "yes" : "no";
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
