import { Client } from "pg";

import type { Pair } from "./types";

/**
 * The bot's universe comes from the database, not from a checked-in list.
 *
 * The original build hardcoded two Fed contracts and their Polymarket token
 * ids in a TypeScript file. That is the same equivalence the matching
 * pipeline already establishes and a human already confirmed, duplicated by
 * hand -- so it drifts the moment a pair is added, and nothing detects the
 * drift until an order goes to a token that no longer trades.
 */
export async function loadPairs(dsn: string): Promise<Pair[]> {
  const client = new Client({ connectionString: dsn });
  await client.connect();
  try {
    const { rows } = await client.query<{
      id: number;
      kalshi_ticker: string;
      poly_token_yes: string;
      poly_token_no: string;
      label: string;
    }>(
      `SELECT id,
              kalshi_ticker,
              poly_token_yes,
              poly_token_no,
              concat_ws(' | ', kalshi_title, kalshi_outcome) AS label
       FROM core.vw_matched_markets
       WHERE poly_token_yes IS NOT NULL
         AND poly_token_no IS NOT NULL
         AND (end_date IS NULL OR end_date > now())
       ORDER BY end_date`,
    );

    return rows.map((row) => ({
      matchedMarketId: row.id,
      kalshiTicker: row.kalshi_ticker,
      polyTokenYes: row.poly_token_yes,
      polyTokenNo: row.poly_token_no,
      label: row.label,
    }));
  } finally {
    await client.end();
  }
}
