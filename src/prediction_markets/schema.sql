CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS core;

CREATE TABLE IF NOT EXISTS core.platforms (
    id SERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

-- Seeded here rather than assumed: the original pipeline compared
-- platform_id against the literals 3 and 4, which only held on the machine
-- where those rows happened to get those serials.
INSERT INTO core.platforms (slug, name)
VALUES ('polymarket', 'Polymarket'), ('kalshi', 'Kalshi')
ON CONFLICT (slug) DO NOTHING;

CREATE TABLE IF NOT EXISTS core.markets (
    id SERIAL PRIMARY KEY,
    platform_id INTEGER NOT NULL REFERENCES core.platforms(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    category TEXT,
    specific_outcome TEXT,
    start_date TIMESTAMPTZ,
    end_date TIMESTAMPTZ,
    -- Lowercase is enforced instead of assumed: the source pipeline wrote
    -- 'OPEN' from one venue and 'open' from the other, and the matcher had
    -- to query for both spellings to find anything.
    status TEXT NOT NULL CHECK (status IN ('open', 'closed', 'inactive')),
    execution_data JSONB NOT NULL DEFAULT '{}'::jsonb,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform_id, external_id)
);

CREATE INDEX IF NOT EXISTS markets_open_by_platform
    ON core.markets (platform_id, end_date) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS core.market_embeddings (
    market_id INTEGER PRIMARY KEY REFERENCES core.markets(id) ON DELETE CASCADE,
    -- Which checkpoint produced the vector. Without it, swapping models
    -- mixes two vector spaces in one column and every cosine score after
    -- the swap is quietly meaningless.
    model TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- HNSW, not IVFFlat: an IVFFlat index has to be built after the table holds
-- representative rows, because its lists are trained from the data present
-- at creation time. This schema runs on an empty database, so IVFFlat here
-- would index into a single centroid and silently return poor neighbours.
CREATE INDEX IF NOT EXISTS market_embeddings_cosine
    ON core.market_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS core.market_match_candidates (
    id SERIAL PRIMARY KEY,
    poly_id INTEGER NOT NULL REFERENCES core.markets(id) ON DELETE CASCADE,
    kalshi_id INTEGER NOT NULL REFERENCES core.markets(id) ON DELETE CASCADE,
    score_similarity DOUBLE PRECISION NOT NULL,
    score_reranker DOUBLE PRECISION NOT NULL,
    -- Denormalised by the trigger below so a human reviewing the queue reads
    -- the two questions side by side without a four-way join.
    poly_title TEXT,
    poly_outcome TEXT,
    kalshi_title TEXT,
    kalshi_outcome TEXT,
    proposed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (poly_id, kalshi_id)
);

CREATE TABLE IF NOT EXISTS core.matched_markets (
    id SERIAL PRIMARY KEY,
    poly_id INTEGER NOT NULL REFERENCES core.markets(id) ON DELETE CASCADE,
    kalshi_id INTEGER NOT NULL REFERENCES core.markets(id) ON DELETE CASCADE,
    confirmed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (poly_id, kalshi_id)
);

CREATE INDEX IF NOT EXISTS matched_markets_poly ON core.matched_markets (poly_id);
CREATE INDEX IF NOT EXISTS matched_markets_kalshi ON core.matched_markets (kalshi_id);

-- Every spread the scanner ever saw, not just the ones that were actionable.
-- The original pipeline printed opportunities and dropped them; without the
-- rejected observations there is no denominator, so "how often does this
-- edge exist" is unanswerable after the fact.
CREATE TABLE IF NOT EXISTS core.arbitrage_scans (
    id BIGSERIAL PRIMARY KEY,
    matched_market_id INTEGER NOT NULL
        REFERENCES core.matched_markets(id) ON DELETE CASCADE,
    observed_at TIMESTAMPTZ NOT NULL,
    strategy TEXT NOT NULL CHECK (strategy IN ('kalshi_yes', 'kalshi_no')),
    kalshi_bid DOUBLE PRECISION NOT NULL,
    polymarket_ask DOUBLE PRECISION NOT NULL,
    total_cost DOUBLE PRECISION NOT NULL,
    edge_bps INTEGER NOT NULL,
    UNIQUE (matched_market_id, observed_at, strategy)
);

CREATE INDEX IF NOT EXISTS arbitrage_scans_recent
    ON core.arbitrage_scans (observed_at DESC, edge_bps DESC);

CREATE OR REPLACE FUNCTION core.fill_market_match_fields() RETURNS trigger
    LANGUAGE plpgsql AS $$
BEGIN
    SELECT title, specific_outcome INTO NEW.poly_title, NEW.poly_outcome
    FROM core.markets WHERE id = NEW.poly_id;

    SELECT title, specific_outcome INTO NEW.kalshi_title, NEW.kalshi_outcome
    FROM core.markets WHERE id = NEW.kalshi_id;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fill_market_match_fields ON core.market_match_candidates;
CREATE TRIGGER fill_market_match_fields
    BEFORE INSERT OR UPDATE ON core.market_match_candidates
    FOR EACH ROW EXECUTE FUNCTION core.fill_market_match_fields();

-- Promotion is a human decision, so it is one transaction: confirm the pair
-- and drop every other candidate that claims either leg, since a Kalshi
-- contract cannot be equivalent to two different Polymarket contracts.
CREATE OR REPLACE PROCEDURE core.match_market(IN p_candidate_id INTEGER)
    LANGUAGE plpgsql AS $$
DECLARE
    v_poly_id INTEGER;
    v_kalshi_id INTEGER;
BEGIN
    SELECT poly_id, kalshi_id INTO v_poly_id, v_kalshi_id
    FROM core.market_match_candidates WHERE id = p_candidate_id;

    IF v_poly_id IS NULL THEN
        RAISE EXCEPTION 'candidate % not found', p_candidate_id;
    END IF;

    INSERT INTO core.matched_markets (poly_id, kalshi_id)
    VALUES (v_poly_id, v_kalshi_id)
    ON CONFLICT (poly_id, kalshi_id) DO NOTHING;

    DELETE FROM core.market_match_candidates
    WHERE poly_id = v_poly_id OR kalshi_id = v_kalshi_id;
END;
$$;

CREATE OR REPLACE VIEW core.vw_matched_markets AS
SELECT mm.id,
       mm.poly_id,
       mm.kalshi_id,
       mp.title AS poly_title,
       mp.specific_outcome AS poly_outcome,
       mp.execution_data ->> 'token_yes' AS poly_token_yes,
       mp.execution_data ->> 'token_no' AS poly_token_no,
       mk.title AS kalshi_title,
       mk.specific_outcome AS kalshi_outcome,
       mk.external_id AS kalshi_ticker,
       least(mp.end_date, mk.end_date) AS end_date
FROM core.matched_markets mm
JOIN core.markets mp ON mp.id = mm.poly_id
JOIN core.markets mk ON mk.id = mm.kalshi_id;
