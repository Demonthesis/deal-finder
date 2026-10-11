CREATE TABLE sites (
    id                     INTEGER PRIMARY KEY,
    name                   TEXT    NOT NULL UNIQUE,
    url                    TEXT    NOT NULL,
    adapter                TEXT    NOT NULL,
    rate_limit_requests    INTEGER NOT NULL CHECK (rate_limit_requests > 0),
    rate_limit_per_seconds REAL    NOT NULL CHECK (rate_limit_per_seconds > 0),
    created_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- A site can belong to several site groups (e.g. eBay is in both clothing and retail).
CREATE TABLE site_group_sites (
    site_group TEXT    NOT NULL,
    site_id    INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    PRIMARY KEY (site_group, site_id)
);

CREATE TABLE listings (
    id            INTEGER PRIMARY KEY,
    site_id       INTEGER NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    url           TEXT    NOT NULL UNIQUE,
    title         TEXT    NOT NULL,
    price         REAL,
    currency      TEXT    NOT NULL DEFAULT 'USD',
    first_seen_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    seen_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX ix_listings_site_id ON listings(site_id);

CREATE TABLE price_history (
    id          INTEGER PRIMARY KEY,
    listing_id  INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    price       REAL    NOT NULL,
    currency    TEXT    NOT NULL,
    observed_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX ix_price_history_listing ON price_history(listing_id, observed_at);

CREATE TABLE alerts_sent (
    id         INTEGER PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    watchlist  TEXT    NOT NULL,
    item       TEXT    NOT NULL,
    price      REAL,
    channel    TEXT    NOT NULL DEFAULT 'default',
    sent_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (listing_id, watchlist, item, channel)
);
