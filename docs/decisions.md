# Decisions

Each entry records what was chosen, what it rules out, and what would make it
wrong. Entries are appended, never rewritten.

## Two vector gates, not one

Cosine similarity over `bge-m3` runs against every open market and is cheap;
the `bge-reranker-v2-m3` cross-encoder reads a pair as a pair and is roughly
two orders of magnitude more expensive per comparison. Running only the first
produces confident nonsense — "Fed cuts in March" and "Fed cuts in June" are
near-identical as independent vectors and are opposite contracts. Running only
the second is unaffordable across a catalogue of tens of thousands.

So the bi-encoder is tuned for recall (0.75, loose) and the cross-encoder for
precision, and neither confirms anything on its own.

**Wrong if** a bi-encoder appears that resolves temporal and numeric
qualifiers well enough that the second stage stops rejecting anything. The
signal would be the reranker's veto rate falling toward zero on the queue.

## Nearest neighbours in Postgres, not in Python

The original pipeline loaded every embedding into memory and multiplied two
matrices with torch. That is quadratic in the number of open markets, it
redoes on every run work the index already holds, and it made the HNSW index
decorative.

The lookup is now a `LATERAL` join with `ORDER BY embedding <=> ...  LIMIT k`,
which is what the index exists for.

**Wrong if** the catalogue ever gets small enough that the round trip
dominates, which is the opposite of the direction it is moving.

## HNSW rather than IVFFlat

IVFFlat trains its lists from the rows present when the index is created.
`schema.sql` runs against an empty database on first connection, so an IVFFlat
index here would be trained on nothing and return poor neighbours forever,
silently. HNSW builds incrementally and has no such precondition.

**Wrong if** the vector count grows past what HNSW's build memory tolerates on
the target machine; at that point IVFFlat becomes viable precisely because
there is then data to train it on.

## Human confirmation between candidate and pair

`market_match_candidates` is a queue; `matched_markets` is a decision. Nothing
promotes automatically.

A wrong equivalence does not produce a bad row that someone notices in a
report. It produces a hedge on an unrelated event, which means two outright
positions instead of one box. The cost of a false positive is asymmetric
enough that a per-pair human read is cheap by comparison — there are dozens of
pairs, not thousands.

**Wrong if** the confirmed set ever grows to where the queue cannot be read by
a person, which would mean building a measured precision estimate first.

## Kalshi's fee is part of the price

The original screen compared `kalshi_bid + polymarket_ask <= 0.98` and called
the remainder profit. Kalshi charges `0.07 × contracts × price × (1 - price)`,
rounded up to the cent — which peaks at exactly the mid-range prices these
pairs trade at, up to 1.75c per contract. Most spreads that looked tradeable
were not.

The fee is therefore inside `total_cost`, not a haircut applied later.
Polymarket's CLOB charges no trading fee, so that leg costs its ask; if that
changes it becomes a second term in the same function.

**Wrong if** either venue changes its fee schedule. The rate is a named
constant in one place per language for that reason.

## Every observation is recorded, not just the profitable ones

`arbitrage_scans` stores both directions of every pair on every sweep,
including the ones that cost more than they settle for. Without the rejections
there is no denominator: "this edge appeared 40 times" is unanswerable, and so
is "the median spread on this pair is -180 bps".

**Wrong if** the row count outgrows what a single table wants — at
15-minute sweeps across a few dozen pairs that is around a million rows a
year, which it does not.

## The spread arithmetic exists twice, in two languages

`arbitrage/pricing.py` and `trader/src/strategy/spread.ts` implement the same
function. This is a duplication accepted on purpose: the scanner runs on a
15-minute Airflow schedule, the bot reacts to websocket ticks, and a process
hop per tick would cost more than the arithmetic.

The risk is silent divergence, so both test suites assert the same
hand-computed numbers, and the tests say so at the top of each file.

**Wrong if** a third consumer appears. Two implementations kept in step by
shared test vectors is manageable; three is not, and at that point the pricing
belongs behind a service or a shared WASM build.

## The trader defaults to dry-run

`TRADER_LIVE` must be the exact string `true`. Anything else — a typo, an
empty value, an unset variable — runs the bot in dry-run, where it prices,
decides, logs and never routes. The switch is enforced inside `KalshiApi` and
`PolymarketExecutor` rather than in the engine, so no later caller can route
around it.

The asymmetry is the point: an accidental dry run costs nothing, an accidental
live run costs money.

## Live trading refuses to start without a hedge venue

If `TRADER_LIVE=true` and the Polymarket credentials are absent, startup
fails. Quoting on Kalshi with no hedge leg is an outright directional
position, which is a different strategy with a different risk profile, and
arriving there by misconfiguration is the worst way to arrive.

## Structured logs instead of a live dashboard

The original rendered a terminal dashboard with `console.clear()`. It reads
well over a shoulder and leaves no record: it overwrites itself, so there is
no way to reconstruct what the bot saw at the moment it decided. A process
that moves money needs the record more than the picture.

## Sequential HTTP, not concurrent

Polymarket's catalogue is 100 pages; Kalshi's is a cursor walk that is
sequential by construction. Fetching the Polymarket pages concurrently would
save under a minute on a daily job, at the cost of an event loop, a Windows
event-loop-policy workaround, and interleaving no test can pin down.

**Wrong if** the catalogue grows past what one ingestion window absorbs.

## `extra="ignore"` on the Gamma contract

Elsewhere in this portfolio the source contracts use `extra="forbid"`, so a
new field stops ingestion rather than slipping through. Gamma returns several
dozen presentation fields and adds to them regularly; forbidding extras would
turn a copy change on Polymarket's website into an outage.

The fields that matter are required instead, so their disappearance still
fails loudly. This is a weaker guarantee than the FRED adapter's, and it is
weaker because the source is genuinely less stable, not because it was
convenient.
