-- LLM extraction results, keyed by a hash of the page text and the extractor, so an
-- unchanged page is never sent to the model twice.
CREATE TABLE extraction_cache (
    content_hash  TEXT NOT NULL,
    extractor     TEXT NOT NULL,
    listings_json TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (content_hash, extractor)
);

CREATE TABLE runs (
    id             INTEGER PRIMARY KEY,
    site_group     TEXT    NOT NULL,
    started_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    finished_at    TEXT,
    sites_checked  INTEGER NOT NULL DEFAULT 0,
    sites_failed   INTEGER NOT NULL DEFAULT 0,
    listings       INTEGER NOT NULL DEFAULT 0,
    deals          INTEGER NOT NULL DEFAULT 0,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    errors         TEXT    NOT NULL DEFAULT '[]'
);

-- One row per LLM call, for the daily token budget and cost reporting.
CREATE TABLE llm_usage (
    id            INTEGER PRIMARY KEY,
    run_id        INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    purpose       TEXT    NOT NULL,
    model         TEXT    NOT NULL,
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX ix_llm_usage_created ON llm_usage(created_at);

ALTER TABLE listings ADD COLUMN condition TEXT;
ALTER TABLE listings ADD COLUMN in_stock INTEGER NOT NULL DEFAULT 1;
-- Normalised title + rounded price, to spot the same item listed twice (cross-posts, relists).
ALTER TABLE listings ADD COLUMN fingerprint TEXT;
CREATE INDEX ix_listings_fingerprint ON listings(fingerprint);

-- A listing that matched a watchlist item. `flagged_price` is the price when it was last
-- flagged as a deal: it is flagged again only if the price drops below that.
CREATE TABLE matches (
    id            INTEGER PRIMARY KEY,
    listing_id    INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    watchlist     TEXT    NOT NULL,
    item          TEXT    NOT NULL,
    score         REAL    NOT NULL,
    price         REAL,
    median_price  REAL,
    flagged_price REAL,
    flagged_at    TEXT,
    duplicate_of  INTEGER REFERENCES listings(id) ON DELETE SET NULL,
    first_seen_at TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL,
    UNIQUE (listing_id, watchlist, item)
);
CREATE INDEX ix_matches_item ON matches(watchlist, item);

-- Prices seen for each watchlist item, per listing and site, for the 30-day median.
-- A row is added when a listing first matches and whenever its price changes.
CREATE TABLE item_prices (
    id          INTEGER PRIMARY KEY,
    watchlist   TEXT    NOT NULL,
    item        TEXT    NOT NULL,
    site_id     INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    listing_id  INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    price       REAL    NOT NULL,
    currency    TEXT    NOT NULL,
    observed_at TEXT    NOT NULL
);
CREATE INDEX ix_item_prices_item ON item_prices(watchlist, item, observed_at);

-- Cached LLM answers to "is this listing really this item?".
CREATE TABLE match_checks (
    title_hash TEXT    NOT NULL,
    item_key   TEXT    NOT NULL,
    is_match   INTEGER NOT NULL,
    reason     TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (title_hash, item_key)
);
