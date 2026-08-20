# prediction-markets-arb

Two venues list contracts on the same events and quote them independently.
This finds the pairs that are genuinely the same contract, records what the
spread between them was at every moment it was measured, and — when told to —
trades it.

## The problem

Kalshi and Polymarket both list "will the Fed cut 25bps in March". A binary
contract settles at $1 or $0, so Kalshi YES and Polymarket NO on the same
event pay exactly $1 together, whichever way it resolves. If the two legs
cost less than $1, the difference is locked in at entry and does not depend on
the outcome.

The hard part is not the arithmetic. It is that neither venue has any idea
the other exists. There is no shared identifier, no shared taxonomy, and no
shared wording:

```
Polymarket   Fed decision in March 2030 | Will the Fed cut by 25bps in March 2030?
Kalshi       Fed decision in March 2030 | 25 bps cut
```

Those are the same contract. `Fed cuts in March` and `Fed cuts in June` are
almost the same sentence and are opposite contracts. Establishing which is
which, across tens of thousands of listings, is the actual work — everything
downstream is bookkeeping.

## Result

The whole pipeline, end to end:

```console
$ prediction-markets ingest --venue polymarket
2 markets on polymarket

$ prediction-markets ingest --venue kalshi
2 markets on kalshi

$ prediction-markets embed
4 markets embedded

$ prediction-markets match
2 candidates queued for review

$ prediction-markets candidates
id,polymarket,kalshi,similarity,rerank
1,"Fed decision in March 2030 | Will the Fed cut by 25bps in March 2030?","Fed decision in March 2030 | 25 bps cut",1.0000,0.9800
2,"Government shutdown before July 2030","Government shutdown before July 2030 | Shutdown occurs",1.0000,0.9800

$ prediction-markets confirm --candidate 1
candidate 1 confirmed

$ prediction-markets opportunities --hours 1
observed_at,kalshi_ticker,strategy,edge_bps,total_cost
2026-08-20T20:00:34+00:00,KXFEDDECISION-30MAR-C25,kalshi_yes,300,0.9700
```

That last row says: at 20:00, buying Kalshi YES at 41c and Polymarket NO at
55c cost 97c including Kalshi's fee, against a $1 settlement — 300 basis
points of edge.

The transcript above was produced against the fixtures versioned in
`tests/fixtures/`, with a deterministic stand-in for the embedding model,
because a live run needs Kalshi credentials and 3 GB of checkpoints. The code
path is the same; only the HTTP calls and the encoder are swapped.

## Problem and scope

- **User:** me. The recorded spread series is the research output; the trader
  is the part that acts on it.
- **Outcome:** answers "does this edge exist, how often, and how wide" with
  data rather than recollection — and can act on it when it does.
- **Input:** Polymarket's public Gamma API and CLOB; Kalshi's trade API.
  Both public, both free. Kalshi requires a key pair but not a funded
  account for reads.
- **Out of scope:** venues beyond these two; sizing and portfolio
  construction; anything resembling a backtest, since the recorded series
  only starts when the scanner does.

## Architecture

```
Airflow: prediction_markets_catalogue (@daily)
    │
    ├─ ingest_polymarket ─┐   Gamma API      → raw zone → core.markets
    ├─ ingest_kalshi ─────┘   trade API v2   → raw zone → core.markets
    │
    ├─ embed                  bge-m3         → core.market_embeddings (pgvector)
    │
    └─ match                  HNSW k-NN in Postgres, then bge-reranker
                                             → core.market_match_candidates
                                                        │
                              human confirms ───────────┘
                              CALL core.match_market(id)
                                             → core.matched_markets
                                                        │
Airflow: arbitrage_scan (*/15)               ───────────┤
    └─ sweep    both live books → pricing.py → core.arbitrage_scans
                                                        │
trader/ (TypeScript, websockets)             ───────────┘
    reads core.vw_matched_markets, quotes passively on Kalshi,
    hedges on Polymarket. Dry-run unless TRADER_LIVE=true.
```

### Why two languages

The Python half is a scheduled research pipeline: it answers questions about
history, runs on a fifteen-minute clock at its fastest, and its expensive step
is a transformer. The TypeScript half is an event loop reacting to websocket
frames in milliseconds, and it exists because Kalshi and Polymarket's
execution SDKs are JavaScript.

They meet in exactly one place: the `core.matched_markets` table. The bot does
not import Python and the pipeline does not import the bot.

### Layout

```
src/prediction_markets/
├── sources/            adapters: talk to a venue, validate, return `Market`
│   ├── polymarket.py       Gamma catalogue + CLOB book
│   └── kalshi/             a folder, not a file, because signing is its own concern
│       ├── auth.py         RSA-PSS request signing
│       └── client.py       catalogue pagination + top of book
├── ingestion/          use cases: wire an adapter to the database
│   ├── markets.py          catalogue → raw zone → core.markets
│   ├── embeddings.py       pending markets → vectors
│   ├── matching.py         vectors → reranked candidate queue
│   └── arbitrage.py        confirmed pairs → priced observations
├── matching/encoders.py    Encoder and Reranker Protocols
├── arbitrage/pricing.py    the box arithmetic, pure
├── repository.py           every SQL statement in the package
├── contracts.py            the `Market` shape both venues normalise to
├── raw.py, postgres.py, schema.sql, config.py, errors.py, cli.py

trader/src/
├── strategy/spread.ts      the same box arithmetic, in TypeScript
├── engine.ts               one pair's state machine (quote → fill → hedge)
├── kalshi/                 auth, REST orders, websocket, book, fills
├── polymarket/             auth, CLOB executor, websocket
├── pairs.ts                loads the universe from core.vw_matched_markets
└── main.ts                 orchestration and shutdown
```

The split between `sources/` and `ingestion/` is not ceremony: an adapter
changes when a venue changes its payload, a use case changes when we decide
to store something differently. They have different reasons to change, so
they are different files.

## The matching problem, concretely

Two gates, cheap before expensive.

**Gate one — bi-encoder recall.** `bge-m3` embeds `title + outcome` into 1024
dimensions. For each open Polymarket market, Postgres returns the five nearest
Kalshi markets by cosine distance, using the HNSW index. Threshold 0.75, set
loose: this stage is allowed to be wrong, it is not allowed to miss.

Only `title + outcome` is embedded. Category and dates are left out on
purpose — they are near-constant within a venue and would pull every pair's
score toward the same value, compressing the range the threshold has to
discriminate over.

**Gate two — cross-encoder precision.** `bge-reranker-v2-m3` reads each
surviving pair as a pair. This is the whole point: a bi-encoder sees each
text alone and cannot tell "March" from "June", because the difference is one
token in an otherwise identical sentence. A cross-encoder attends across both
and can.

**Gate three — a person.** Nothing promotes itself. A wrong equivalence does
not produce a bad row in a report; it produces a hedge on an unrelated event,
which means two outright positions instead of one riskless box. There are
dozens of pairs, not thousands, so a human read is cheap relative to that.

`core.match_market()` does the promotion in one transaction: confirm the pair
and delete every other candidate claiming either leg, since a Kalshi contract
cannot be equivalent to two different Polymarket contracts.

## Pricing: the fee is not a detail

The naive screen is `kalshi_bid + polymarket_ask < 1`. Kalshi charges
`0.07 × contracts × price × (1 − price)`, rounded up to the cent. That
function peaks at 50c — exactly where these pairs trade — at 1.75c per
contract:

| Kalshi bid | Polymarket ask | Raw gap | Fee | Real edge |
|---|---|---|---|---|
| 0.40 | 0.55 | 5.0c | 2c | **+300 bps** |
| 0.50 | 0.495 | 0.5c | 2c | **−150 bps** |

The second row looks like an opportunity and is a loss. Polymarket's CLOB
charges no trading fee, so that leg costs its ask and nothing more.

Both directions of every pair are priced and recorded, including the negative
ones. Without the rejections there is no denominator, and "how often does this
edge exist" becomes unanswerable after the fact.

## The database

Postgres 16 with `pgvector`, schema `core`. Recreated from an earlier
hand-managed database; the table and column names are kept so existing
queries still read.

| Table | Holds |
|---|---|
| `platforms` | the two venues, seeded by the schema |
| `markets` | one row per contract per venue, keyed `(platform_id, external_id)` |
| `market_embeddings` | one vector per market, tagged with the model that made it |
| `market_match_candidates` | the review queue, with both scores |
| `matched_markets` | confirmed equivalences |
| `arbitrage_scans` | every spread ever observed, profitable or not |
| `vw_matched_markets` | the pairs view the trader reads |

Five things changed from the original schema, each for a reason:

- **`platforms` is seeded by `schema.sql`.** The original matching code
  compared `platform_id == 3` and `== 4`, which was true only on the machine
  where those rows happened to receive those serials.
- **`market_embeddings.market_id` is the primary key.** It was previously a
  plain column, so a re-run silently stored a second vector per market and
  the k-NN search returned the same market twice.
- **`embedding_model` is a column.** Swapping checkpoints otherwise mixes two
  coordinate systems in one column, and every cosine score after the swap is
  quietly meaningless.
- **HNSW, not IVFFlat.** IVFFlat trains its lists from the rows present at
  creation. This schema runs on an empty database, so the original index was
  trained on nothing — it existed, it was used, and it returned poor
  neighbours forever without ever erroring.
- **`status` has a `CHECK` constraint.** One venue wrote `OPEN` and the other
  `open` into the same column, and the matcher had to query
  `status IN ('OPEN','open')` to find anything.

Plus one addition: `arbitrage_scans`, which is what turns a script that prints
opportunities into a series someone can ask questions of.

## Running it

Requirements: Python 3.12, [`uv`](https://docs.astral.sh/uv/), Node 20+, and
Docker.

```bash
uv sync
cp .env.example .env          # fill in the Kalshi key id
(cd trader && npm ci)
docker compose up -d postgres # Postgres with pgvector on localhost:5434
```

Kalshi issues an RSA key pair under Account → API Keys. The PEM is shown
once and cannot be downloaded again; put it at `secrets/kalshi.pem`. That
directory is gitignored, as is `*.pem` anywhere in the tree.

```bash
uv run python -m prediction_markets ingest --venue polymarket
uv run python -m prediction_markets ingest --venue kalshi
uv run python -m prediction_markets embed          # needs the ml extra, below
uv run python -m prediction_markets match
uv run python -m prediction_markets candidates     # read the queue
uv run python -m prediction_markets confirm --candidate 7
uv run python -m prediction_markets scan
uv run python -m prediction_markets opportunities --hours 24
```

Without credentials, `ingest --venue kalshi` explains which variables are
missing and exits 2 rather than crashing. Without Postgres up, every command
does the same.

The schema is created on first connection; there is no separate migration
step.

### The models

`sentence-transformers` and `torch` are an optional extra, not a default
dependency:

```bash
uv sync --extra ml
```

They are ~3 GB with the two BGE checkpoints, and the encoder and reranker are
Protocols — so the test suite and CI exercise the entire matching pipeline
against deterministic fakes and never download a model. The Airflow image is
the only thing that installs the extra, from the CPU wheel index, because
this workload never touches a GPU.

### Airflow

```bash
docker compose up -d              # postgres + airflow-init + scheduler
docker compose up -d airflow-webserver
```

UI at <http://localhost:8082>, `admin`/`admin`. Two DAGs:

- `prediction_markets_catalogue` (`@daily`) — both venues in parallel, then
  embed, then match.
- `arbitrage_scan` (`*/15 * * * *`) — prices every confirmed pair. Capped at
  one active run, because a scan is only meaningful at the moment it runs.

Trigger manually:

```bash
docker compose exec airflow-scheduler airflow dags trigger arbitrage_scan
```

Airflow's metadata lives in a separate `airflow` database on the same
Postgres instance — two databases, one server, no extra infrastructure. The
HuggingFace cache is a named volume, so a rebuild does not re-download three
gigabytes of checkpoints.

### The trader

```bash
cd trader
npm start
```

**It will not send an order unless `TRADER_LIVE` is exactly the string
`true`.** A typo, an empty value or an unset variable all land in dry-run,
where the bot loads the real pairs, subscribes to the real books, prices
them, decides, and logs what it would have done. The switch is enforced
inside `KalshiApi` and `PolymarketExecutor`, not in the engine, so no later
caller can route around it.

Setting `TRADER_LIVE=true` without Polymarket credentials fails at startup:
quoting on Kalshi with no hedge leg is an outright directional position, not
arbitrage, and arriving there by misconfiguration is the worst way to arrive.

What it does: joins the best Kalshi bid on the wider side rather than
crossing it — crossing pays a spread wider than the edge — and treats the
hedge as asynchronous, because a passive quote's fill is uncertain. Once
filled contracts accumulate past `TRADER_HEDGE_THRESHOLD`, it buys the
opposite Polymarket token fill-or-kill. If that hedge fails it stops, cancels
everything resting, and leaves the position for a human: unwinding it
automatically at whatever the book offers is how a small loss becomes a large
one.

Its universe comes from `core.vw_matched_markets`, not from a checked-in
list. The original hardcoded two Fed contracts and their token ids in a
TypeScript file, duplicating by hand the equivalence the pipeline already
establishes — so it drifted the moment a pair was added, and nothing detected
the drift until an order went to a token that no longer traded.

### Verification

```bash
uv run python scripts/check.py    # format, lint, types, tests — both halves
uv run python scripts/format.py
```

One command, one list. CI calls the same script rather than restating the
steps in the workflow.

## How this was validated

62 Python tests at 89% coverage, 32 TypeScript tests. What they pin down:

- the box arithmetic against hand-computed numbers, in both languages, from
  the same cases — a fee that turns a 0.5c gap into a 150 bps loss, a fee
  that must not overcharge a cent to floating-point error;
- an empty side of a book yields no leg, rather than a leg priced at zero;
- Polymarket's YES token is chosen by outcome label, not array position —
  Gamma lists NO first, and inverting it produces a hedge that fills on the
  wrong side of the same event;
- the CLOB's best ask is the minimum, not the last element, which today
  happens to be the same thing;
- a resolved contract and a thin one never reach the database;
- Kalshi's signature covers the path without the query string, verified
  against a real RSA key pair generated in the test;
- a `yes_bid` of 0 is an empty book, not a free contract;
- multivariate collection legs are excluded — they have no counterpart and
  outnumber everything else on the venue;
- a 4xx is not retried, a 5xx exhausts three attempts, a rejected signature
  says what to check;
- the similarity floor, the reranker's veto, and one-candidate-per-leg, all
  against a real Postgres with pgvector;
- vectors from a different model are never compared with each other;
- confirming a pair deletes the competing candidates, through the stored
  procedure;
- re-running any stage changes nothing — no duplicate partition, no duplicate
  row, no re-embedding;
- a partial fill leaves the rest of the order resting and cancellable, rather
  than stranding it on the venue;
- a rejected hedge stops the engine and pulls its quotes;
- one venue failing to quote one contract does not abort the sweep.

The database tests run against a real Postgres via testcontainers, not a
mock: HNSW behaviour, the trigger, the `CHECK` constraint and the stored
procedure are the parts most worth testing and the parts a mock cannot test.

## Limitations

- **The recorded series starts when the scanner does.** There is no history
  before that and no way to reconstruct one, so nothing here is a backtest.
- **Fifteen minutes is far too coarse to trade from.** It characterises how
  often an edge appears and roughly how long it lasts. Acting on one is the
  trader's job, over websockets.
- **Depth is ignored.** Both sides use the best price only, so a spread that
  exists for 5 contracts and one that exists for 500 look identical. Sizing
  is the obvious next cut.
- **The hedge is a race, not a guarantee.** The Kalshi fill happens before
  the Polymarket order is sent. Between them the position is naked, and if
  the fill-or-kill is rejected it stays naked until a person intervenes. This
  is the strategy's real risk and no amount of code removes it.
- **Matching precision is not measured.** The human queue is the control, and
  no labelled set exists to quantify what the two gates let through.
- **`LocalExecutor`, single machine, `docker-compose`.** Production concerns
  — credentials, backup, availability — are deferred until there is a real
  environment to host it in.
- **Kalshi's fee model is hardcoded to the published schedule** and does not
  account for maker rebates or volume tiers.

## Decisions

The reasoning behind the choices above, and what would make each one wrong,
is in [`docs/decisions.md`](docs/decisions.md).

## License

MIT.
