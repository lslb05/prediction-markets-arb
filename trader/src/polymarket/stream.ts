import { EventEmitter } from "node:events";

import WebSocket from "ws";

import { log } from "../logger";
import type { BookSide, Level } from "../types";

const WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market";
const DEPTH = 3;

/**
 * One websocket carrying every CLOB token the bot watches.
 *
 * Emits `book` with the token id, so the orchestrator can route an update to
 * the one engine that cares. The original build opened a socket per token
 * in the single-market runner and a shared one in the multi-market runner;
 * there is only the shared form here.
 */
export class PolymarketStream extends EventEmitter {
  private socket: WebSocket | undefined;
  private readonly books = new Map<
    string,
    { bids: Map<number, number>; asks: Map<number, number> }
  >();

  constructor(private readonly tokenIds: readonly string[]) {
    super();
    for (const id of tokenIds) {
      this.books.set(id, { bids: new Map(), asks: new Map() });
    }
  }

  connect(): void {
    this.socket = new WebSocket(WS_URL, { perMessageDeflate: false });

    this.socket.on("open", () => {
      log.info("polymarket.stream.open", { assets: this.tokenIds.length });
      this.socket?.send(
        JSON.stringify({ assets_ids: [...this.tokenIds], type: "market" }),
      );
    });

    this.socket.on("message", (data: WebSocket.RawData) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(data.toString());
      } catch {
        log.warn("polymarket.stream.unparseable_frame");
        return;
      }
      for (const event of Array.isArray(parsed) ? parsed : [parsed]) {
        this.apply(event);
      }
    });

    this.socket.on("error", (error: Error) =>
      log.error("polymarket.stream.error", { message: error.message }),
    );
  }

  close(): void {
    this.socket?.terminate();
  }

  /** Lowest ask in dollars, or null when nobody is offering. */
  bestAsk(tokenId: string): number | null {
    return this.asks(tokenId)[0]?.price ?? null;
  }

  asks(tokenId: string): BookSide {
    const book = this.books.get(tokenId);
    if (!book) {
      return [];
    }
    return [...book.asks.entries()]
      .sort(([a], [b]) => a - b)
      .slice(0, DEPTH)
      .map(([price, size]): Level => ({ price, size }));
  }

  private apply(event: unknown): void {
    if (typeof event !== "object" || event === null) {
      return;
    }
    const body = event as Record<string, unknown>;

    if (body.event_type === "book" && typeof body.asset_id === "string") {
      const book = this.books.get(body.asset_id);
      if (!book) {
        return;
      }
      book.bids.clear();
      book.asks.clear();
      replace(book.bids, body.bids);
      replace(book.asks, body.asks);
      this.emit("book", body.asset_id);
      return;
    }

    if (body.event_type === "price_change" && Array.isArray(body.price_changes)) {
      const touched = new Set<string>();
      for (const change of body.price_changes) {
        const id = applyChange(this.books, change);
        if (id !== null) {
          touched.add(id);
        }
      }
      for (const id of touched) {
        this.emit("book", id);
      }
    }
  }
}

function replace(target: Map<number, number>, levels: unknown): void {
  if (!Array.isArray(levels)) {
    return;
  }
  for (const level of levels) {
    const price = Number((level as { price?: unknown }).price);
    const size = Number((level as { size?: unknown }).size);
    if (Number.isFinite(price) && Number.isFinite(size) && size > 0) {
      target.set(price, size);
    }
  }
}

function applyChange(
  books: Map<string, { bids: Map<number, number>; asks: Map<number, number> }>,
  change: unknown,
): string | null {
  if (typeof change !== "object" || change === null) {
    return null;
  }
  const body = change as Record<string, unknown>;
  const assetId = body.asset_id;
  if (typeof assetId !== "string") {
    return null;
  }
  const book = books.get(assetId);
  if (!book) {
    return null;
  }

  const price = Number(body.price);
  const size = Number(body.size);
  if (!Number.isFinite(price) || !Number.isFinite(size)) {
    return null;
  }

  const side = body.side === "BUY" ? book.bids : book.asks;
  if (size === 0) {
    side.delete(price);
  } else {
    side.set(price, size);
  }
  return assetId;
}
