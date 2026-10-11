# Deal Finder

Watches resale and retail sites for listings that match your watchlists.

## Setup

```sh
uv sync                      # Python 3.12 venv in .venv
uv run dealfinder validate-config                 # checks config/watchlists.yaml
uv run dealfinder init-db -c config/watchlists.yaml   # creates data/dealfinder.db, syncs sites
uv run pytest
```

## Config

`config/watchlists.yaml` has `site_groups` (each with sites: `name`, `url`, `adapter`, `rate_limit`)
and `watchlists` (each with a `site_group` and `items`: `keywords`, `exclude`, `max_price`,
`currency`, `condition`). The schema is in `src/dealfinder/config.py`. Unknown keys are rejected.

## Fetching pages

`dealfinder.fetch.Fetcher` downloads pages for the scraping adapters (`css`, `llm`). For every
request it checks robots.txt (rules for `DealFinder`, else `*`; Crawl-delay is honoured), waits
for the site's `rate_limit` (keyed by host, `www.` ignored; unknown hosts get 1 request / 2 s),
retries 429/5xx and network errors with exponential backoff (honouring `Retry-After`), and
revalidates cached pages with ETag / Last-Modified (`FileCache` stores them in `data/http_cache/`).
`FetchResult.text` is a cleaned markdown version of the main content; send that to the LLM, not
`html`.

A site whose listings are built with JavaScript can set `fetcher: browser` to be rendered in
headless Chromium via Playwright. That needs the optional extra:

```sh
uv sync --extra browser
uv run playwright install chromium   # on a bare Linux box, also: playwright install-deps
```

## Database

SQLite. Migrations are numbered SQL files in `src/dealfinder/migrations/`, applied in order
and tracked in `PRAGMA user_version`. To change the schema, add `0002_<what>.sql`; never edit
an existing migration. `init-db` is safe to rerun: it only applies pending migrations.
