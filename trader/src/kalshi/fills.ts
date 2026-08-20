import type { Fill } from "../types";

/**
 * Reads fill frames off the shared stream and reports the ones that matter.
 *
 * A fill is the only event that turns a quote into inventory, so it is what
 * arms the hedge. Anything that isn't a recognisable fill for a ticker the
 * bot trades is ignored rather than guessed at.
 */
export function parseFill(message: unknown, tickers: ReadonlySet<string>): Fill | null {
  if (
    typeof message !== "object" ||
    message === null ||
    (message as { type?: unknown }).type !== "fill"
  ) {
    return null;
  }
  const body = (message as { msg?: Record<string, unknown> }).msg;
  if (!body) {
    return null;
  }

  const ticker = body.market_ticker;
  const count = body.count;
  const side = body.side;
  const orderId = body.order_id;

  if (
    typeof ticker !== "string" ||
    !tickers.has(ticker) ||
    typeof count !== "number" ||
    count <= 0 ||
    (side !== "yes" && side !== "no") ||
    typeof orderId !== "string"
  ) {
    return null;
  }

  return { orderId, ticker, side, count, priceCents: priceOf(body) };
}

function priceOf(body: Record<string, unknown>): number {
  if (typeof body.yes_price === "number") {
    return body.yes_price;
  }
  if (typeof body.no_price === "number") {
    return body.no_price;
  }
  // Some series report the price as a dollar string instead of cents.
  if (typeof body.yes_price_dollars === "string") {
    return Math.round(Number(body.yes_price_dollars) * 100);
  }
  return 0;
}
