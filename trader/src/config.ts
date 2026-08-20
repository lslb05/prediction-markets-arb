import fs from "node:fs";
import path from "node:path";

import dotenv from "dotenv";

dotenv.config({ path: path.resolve(__dirname, "../../.env") });

export interface TraderConfig {
  /**
   * Whether the process is allowed to send an order.
   *
   * The default is false and the only value that flips it is the exact
   * string "true". A typo, an empty variable or a missing one all land in
   * dry-run, where the bot prices, decides and logs but never routes. The
   * asymmetry is deliberate: an accidental dry run costs nothing, an
   * accidental live run costs money.
   */
  readonly live: boolean;
  readonly postgresDsn: string;
  readonly kalshiKeyId: string;
  readonly kalshiPrivateKey: string;
  readonly polymarket: PolymarketCredentials | null;
  /** Minimum edge, in basis points, before the bot will quote at all. */
  readonly minEdgeBps: number;
  /** Contracts per Kalshi order. */
  readonly orderSize: number;
  /** Unhedged contracts tolerated before the Polymarket leg is forced. */
  readonly hedgeThreshold: number;
}

export interface PolymarketCredentials {
  readonly privateKey: string;
  readonly apiKey: string;
  readonly apiSecret: string;
  readonly passphrase: string;
  readonly funderAddress: string | undefined;
}

export function loadConfig(): TraderConfig {
  const live = process.env.TRADER_LIVE === "true";
  const polymarket = readPolymarket();

  if (live && polymarket === null) {
    // Quoting on Kalshi without a working hedge is a naked directional
    // position, not arbitrage. Refusing here is the difference between a
    // configuration mistake and an unhedged book.
    throw new Error(
      "TRADER_LIVE=true requires the Polymarket credentials: without a hedge " +
        "leg the Kalshi orders are an outright position, not a box",
    );
  }

  return {
    live,
    postgresDsn:
      process.env.PREDICTION_MARKETS_POSTGRES_DSN ??
      "postgresql://markets:markets@localhost:5434/prediction_markets",
    kalshiKeyId: required("TRADER_KALSHI_KEY_ID"),
    kalshiPrivateKey: readKey(required("TRADER_KALSHI_PRIVATE_KEY_PATH")),
    polymarket,
    minEdgeBps: number("TRADER_MIN_EDGE_BPS", 200),
    orderSize: number("TRADER_ORDER_SIZE", 5),
    hedgeThreshold: number("TRADER_HEDGE_THRESHOLD", 5),
  };
}

function readPolymarket(): PolymarketCredentials | null {
  const privateKey = process.env.TRADER_POLYMARKET_PRIVATE_KEY;
  if (!privateKey) {
    return null;
  }
  return {
    privateKey,
    apiKey: process.env.TRADER_POLYMARKET_API_KEY ?? "",
    apiSecret: process.env.TRADER_POLYMARKET_API_SECRET ?? "",
    passphrase: process.env.TRADER_POLYMARKET_PASSPHRASE ?? "",
    funderAddress: process.env.TRADER_POLYMARKET_FUNDER,
  };
}

function required(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(`${name} is not set; see .env.example`);
  }
  return value;
}

function readKey(keyPath: string): string {
  const resolved = path.isAbsolute(keyPath)
    ? keyPath
    : path.resolve(__dirname, "../../", keyPath);
  try {
    return fs.readFileSync(resolved, "utf8").trim();
  } catch (error) {
    throw new Error(
      `could not read the Kalshi private key at ${resolved}: ${String(error)}`,
    );
  }
}

function number(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === "") {
    return fallback;
  }
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) {
    throw new Error(`${name} is not a number: ${raw}`);
  }
  return parsed;
}
