export interface Level {
  readonly price: number;
  readonly size: number;
}

/** Top of a book, best price first, at most three levels deep. */
export type BookSide = readonly Level[];

/** One confirmed cross-venue equivalence, as `core.vw_matched_markets` reports it. */
export interface Pair {
  readonly matchedMarketId: number;
  readonly kalshiTicker: string;
  readonly polyTokenYes: string;
  readonly polyTokenNo: string;
  readonly label: string;
}

export interface Fill {
  readonly orderId: string;
  readonly ticker: string;
  readonly side: "yes" | "no";
  readonly count: number;
  readonly priceCents: number;
}
