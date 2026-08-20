import { loadConfig } from "./config";
import { Engine } from "./engine";
import { KalshiApi } from "./kalshi/api";
import { KalshiAuth } from "./kalshi/auth";
import { KalshiBook } from "./kalshi/book";
import { parseFill } from "./kalshi/fills";
import { KalshiStream } from "./kalshi/stream";
import { log } from "./logger";
import { loadPairs } from "./pairs";
import { PolymarketAuth } from "./polymarket/auth";
import { PolymarketExecutor } from "./polymarket/executor";
import { PolymarketStream } from "./polymarket/stream";
import type { Pair } from "./types";

const SHUTDOWN_GRACE_MS = 5_000;

async function main(): Promise<void> {
  const config = loadConfig();
  const pairs = await loadPairs(config.postgresDsn);

  if (pairs.length === 0) {
    log.warn("no_confirmed_pairs", {
      hint: "run the matching pipeline and confirm a candidate first",
    });
    return;
  }

  log.info("starting", {
    live: config.live,
    pairs: pairs.length,
    min_edge_bps: config.minEdgeBps,
  });

  const auth = new KalshiAuth(config.kalshiKeyId, config.kalshiPrivateKey);
  const api = new KalshiApi(auth, config.live);
  const executor = new PolymarketExecutor(
    config.polymarket === null ? null : new PolymarketAuth(config.polymarket),
    config.live,
  );

  const engines = new Map<string, Engine>();
  const books = new Map<string, KalshiBook>();
  const byToken = new Map<string, Pair>();

  for (const pair of pairs) {
    engines.set(
      pair.kalshiTicker,
      new Engine(pair, api, executor, {
        minEdgeBps: config.minEdgeBps,
        orderSize: config.orderSize,
        hedgeThreshold: config.hedgeThreshold,
      }),
    );
    books.set(pair.kalshiTicker, new KalshiBook(pair.kalshiTicker));
    byToken.set(pair.polyTokenYes, pair);
    byToken.set(pair.polyTokenNo, pair);
  }

  const tickers = new Set(pairs.map((pair) => pair.kalshiTicker));
  const polymarket = new PolymarketStream([...byToken.keys()]);
  const kalshi = new KalshiStream(auth);

  const tick = (pair: Pair): void => {
    const engine = engines.get(pair.kalshiTicker);
    const book = books.get(pair.kalshiTicker);
    if (!engine || !book) {
      return;
    }
    void engine
      .onQuotes({
        kalshiYesBid: book.bestBid("yes"),
        kalshiNoBid: book.bestBid("no"),
        polyYesAsk: polymarket.bestAsk(pair.polyTokenYes),
        polyNoAsk: polymarket.bestAsk(pair.polyTokenNo),
      })
      .catch((error: unknown) =>
        log.error("tick_failed", {
          pair: pair.label,
          message: error instanceof Error ? error.message : String(error),
        }),
      );
  };

  kalshi.on("message", (message: unknown) => {
    for (const pair of pairs) {
      if (books.get(pair.kalshiTicker)?.apply(message)) {
        tick(pair);
      }
    }
    const fill = parseFill(message, tickers);
    if (fill) {
      void engines.get(fill.ticker)?.onFill(fill);
    }
  });

  // A dead stream means the bot is flying blind while its orders stay live
  // on the venue. Pulling them is the only safe response.
  kalshi.on("dead", () => void shutdown("stream_dead"));

  polymarket.on("book", (tokenId: string) => {
    const pair = byToken.get(tokenId);
    if (pair) {
      tick(pair);
    }
  });

  kalshi.connect();
  kalshi.subscribe([...tickers]);
  polymarket.connect();

  let shuttingDown = false;
  async function shutdown(reason: string): Promise<void> {
    if (shuttingDown) {
      return;
    }
    shuttingDown = true;
    log.warn("shutting_down", { reason });

    // Every resting order is cancelled before the process goes away, with a
    // deadline: an order left behind keeps trading without anything watching
    // the hedge.
    await Promise.race([
      Promise.all([...engines.values()].map((engine) => engine.withdraw(reason))),
      new Promise((resolve) => setTimeout(resolve, SHUTDOWN_GRACE_MS)),
    ]);

    const stranded = [...engines.values()].filter(
      (engine) => engine.state().activeOrder !== null || engine.state().unhedged > 0,
    );
    for (const engine of stranded) {
      log.error("stranded_on_exit", {
        pair: engine.pair.label,
        order_id: engine.state().activeOrder?.orderId ?? null,
        unhedged: engine.state().unhedged,
      });
    }

    kalshi.close();
    polymarket.close();
    process.exitCode = stranded.length > 0 ? 1 : 0;
  }

  for (const signal of ["SIGINT", "SIGTERM"] as const) {
    process.on(signal, () => void shutdown(signal));
  }
  process.on("uncaughtException", (error) => {
    log.error("uncaught_exception", { message: error.message });
    void shutdown("uncaught_exception");
  });
  process.on("unhandledRejection", (reason) => {
    log.error("unhandled_rejection", { message: String(reason) });
    void shutdown("unhandled_rejection");
  });
}

main().catch((error: unknown) => {
  log.error("startup_failed", {
    message: error instanceof Error ? error.message : String(error),
  });
  process.exitCode = 1;
});
