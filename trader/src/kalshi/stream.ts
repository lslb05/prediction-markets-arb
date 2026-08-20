import { EventEmitter } from "node:events";

import WebSocket from "ws";

import { log } from "../logger";
import { KalshiAuth } from "./auth";

const WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2";
const WS_PATH = "/trade-api/ws/v2";

const MAX_RECONNECTS = 10;
const BASE_DELAY_MS = 1_000;

/**
 * One authenticated websocket carrying every subscription.
 *
 * The original build had two near-identical copies of this class, one per
 * runner, which had already drifted apart in how they batched subscriptions.
 * There is one connection here and the subscription list is a set, so
 * reconnecting restores exactly what was subscribed.
 *
 * On giving up it emits `dead` rather than calling `process.exit`. A stream
 * that kills the process takes the open orders with it: the owner of those
 * orders has to cancel them first, and only the orchestrator knows about
 * them.
 */
export class KalshiStream extends EventEmitter {
  private socket: WebSocket | undefined;
  private readonly subscriptions = new Set<string>();
  private connected = false;
  private closing = false;
  private attempts = 0;

  constructor(private readonly auth: KalshiAuth) {
    super();
  }

  connect(): void {
    if (this.closing) {
      return;
    }
    this.socket = new WebSocket(WS_URL, {
      headers: this.auth.headers("GET", WS_PATH),
      perMessageDeflate: false,
    });

    this.socket.on("open", () => {
      this.connected = true;
      this.attempts = 0;
      log.info("kalshi.stream.open", { subscriptions: this.subscriptions.size });
      if (this.subscriptions.size > 0) {
        this.send([...this.subscriptions]);
      }
    });

    this.socket.on("message", (data: WebSocket.RawData) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(data.toString());
      } catch {
        // A frame that isn't JSON is the venue's problem, not a reason to
        // drop the connection carrying every other subscription.
        log.warn("kalshi.stream.unparseable_frame");
        return;
      }
      this.emit("message", parsed);
    });

    this.socket.on("error", (error: Error) => {
      log.error("kalshi.stream.error", { message: error.message });
    });

    this.socket.on("close", () => this.reconnect());
  }

  subscribe(tickers: readonly string[]): void {
    const fresh = tickers.filter((ticker) => !this.subscriptions.has(ticker));
    for (const ticker of fresh) {
      this.subscriptions.add(ticker);
    }
    if (this.connected && fresh.length > 0) {
      this.send(fresh);
    }
  }

  close(): void {
    this.closing = true;
    this.subscriptions.clear();
    this.socket?.terminate();
  }

  private send(tickers: readonly string[]): void {
    if (this.socket?.readyState !== WebSocket.OPEN) {
      return;
    }
    this.socket.send(
      JSON.stringify({
        id: Date.now(),
        cmd: "subscribe",
        params: {
          channels: ["orderbook_delta", "fill"],
          market_tickers: tickers,
        },
      }),
    );
    log.info("kalshi.stream.subscribe", { count: tickers.length });
  }

  private reconnect(): void {
    this.connected = false;
    if (this.closing) {
      return;
    }
    if (this.attempts >= MAX_RECONNECTS) {
      log.error("kalshi.stream.gave_up", { attempts: this.attempts });
      this.emit("dead");
      return;
    }
    const delay = Math.round(BASE_DELAY_MS * Math.pow(1.5, this.attempts));
    this.attempts += 1;
    log.warn("kalshi.stream.reconnecting", { attempt: this.attempts, delay_ms: delay });
    setTimeout(() => this.connect(), delay);
  }
}
