import { ClobClient, OrderType, Side } from "@polymarket/clob-client";

import { log } from "../logger";
import { PolymarketAuth } from "./auth";

const POLYGON_CHAIN_ID = 137;
const CLOB_URL = "https://clob.polymarket.com";

/**
 * A price the hedge will never exceed. The hedge is fill-or-kill and the
 * whole point is to close a position that already exists, so it crosses the
 * book rather than joining it -- but not at any price: 0.99 caps the loss if
 * the book has emptied out between the fill and the hedge.
 */
const MAX_HEDGE_PRICE = 0.99;

/**
 * The only place in this process that can buy on Polymarket.
 *
 * Like the Kalshi side, the live switch lives at the boundary: in dry-run the
 * hedge is logged and reported as filled, so the engine's state machine is
 * exercised end to end without a wallet.
 */
export class PolymarketExecutor {
  private readonly client: ClobClient | null;

  constructor(auth: PolymarketAuth | null, private readonly live: boolean) {
    this.client =
      auth === null
        ? null
        : new ClobClient(
            CLOB_URL,
            POLYGON_CHAIN_ID,
            auth.signer() as never,
            auth.creds(),
            1,
            auth.funderAddress(),
          );
  }

  /** Returns whether the hedge is on. A false here leaves the position naked. */
  async buyHedge(tokenId: string, quantity: number): Promise<boolean> {
    if (!this.live || this.client === null) {
      log.info("hedge.simulated", { token: tokenId.slice(0, 10), quantity });
      return true;
    }

    const response = await this.client.createAndPostOrder({
      tokenID: tokenId,
      price: MAX_HEDGE_PRICE,
      side: Side.BUY,
      size: quantity,
      orderType: OrderType.FOK,
    } as never);

    const accepted = Boolean((response as { success?: boolean }).success);
    if (!accepted) {
      log.error("hedge.rejected", {
        token: tokenId.slice(0, 10),
        quantity,
        reason: String((response as { errorMsg?: string }).errorMsg ?? "unknown"),
      });
    }
    return accepted;
  }
}
