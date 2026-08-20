import axios, { type AxiosInstance } from "axios";
import { v4 as uuid } from "uuid";

import { log } from "../logger";
import { KalshiAuth } from "./auth";

const BASE_URL = "https://api.elections.kalshi.com/trade-api/v2";
const PREFIX = "/trade-api/v2";

export type Side = "yes" | "no";

export interface OrderRequest {
  readonly ticker: string;
  readonly side: Side;
  /** Limit price in whole cents, as Kalshi quotes it. */
  readonly priceCents: number;
  readonly count: number;
}

export interface PlacedOrder {
  readonly orderId: string;
  readonly priceCents: number;
  readonly side: Side;
  readonly count: number;
}

/**
 * The only place in this process that can create or cancel a Kalshi order.
 *
 * The live switch is enforced here rather than in the engine so that no
 * future caller can route around it: in dry-run the methods return a
 * synthetic order and never open a socket, and the engine's state machine
 * runs unchanged against it.
 */
export class KalshiApi {
  private readonly client: AxiosInstance;
  private dryRunSequence = 0;

  constructor(
    private readonly auth: KalshiAuth,
    private readonly live: boolean,
  ) {
    this.client = axios.create({
      baseURL: BASE_URL,
      timeout: 10_000,
      // 5xx still throws; 4xx comes back as a response so the body can be
      // logged instead of disappearing into an axios error message.
      validateStatus: (status) => status < 500,
    });
  }

  async createOrder(request: OrderRequest): Promise<PlacedOrder> {
    if (!this.live) {
      this.dryRunSequence += 1;
      const orderId = `dry-run-${this.dryRunSequence}`;
      log.info("order.simulated", {
        ticker: request.ticker,
        side: request.side,
        price_cents: request.priceCents,
        count: request.count,
        order_id: orderId,
      });
      return { orderId, priceCents: request.priceCents, side: request.side, count: request.count };
    }

    const path = "/portfolio/orders";
    const payload = {
      action: "buy",
      client_order_id: uuid(),
      count: request.count,
      side: request.side,
      ticker: request.ticker,
      type: "limit",
      ...(request.side === "yes"
        ? { yes_price: request.priceCents }
        : { no_price: request.priceCents }),
    };

    const response = await this.client.post(path, payload, {
      headers: this.auth.headers("POST", PREFIX + path),
    });
    if (response.status !== 201) {
      throw new Error(
        `Kalshi refused the order (${response.status}): ${JSON.stringify(response.data)}`,
      );
    }

    const orderId: unknown = response.data?.order?.order_id;
    if (typeof orderId !== "string") {
      // Treating a malformed acknowledgement as success would leave a live
      // order the process no longer has an id to cancel.
      throw new Error("Kalshi accepted the order but returned no order id");
    }
    return { orderId, priceCents: request.priceCents, side: request.side, count: request.count };
  }

  /** Returns false when the order was already gone, which is not a failure. */
  async cancelOrder(orderId: string): Promise<boolean> {
    if (!this.live) {
      log.info("order.cancel_simulated", { order_id: orderId });
      return true;
    }

    const path = `/portfolio/orders/${orderId}`;
    const response = await this.client.delete(path, {
      headers: this.auth.headers("DELETE", PREFIX + path),
    });

    if (response.status === 200 || response.status === 204) {
      return true;
    }
    if (response.status === 404) {
      return false;
    }
    throw new Error(`Kalshi refused the cancellation (${response.status})`);
  }
}
