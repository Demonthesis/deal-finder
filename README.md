# Deal Finder

Watches resale and retail sites for listings that match your watchlists, scores them, and
alerts you (Discord, ntfy, Telegram or email) when something is at or under your price.
A small web app shows the deals and lets you edit watchlists and sites.

```
site group -> fetch (official API / RSS / CSS selectors / LLM extraction) -> match -> score
           -> new deal? -> alert (instant, after quiet hours, or in the daily digest)
```

How-tos (adding lists and sites, scoring, the token budget, deploying and updating) are in
[docs/guide.md](docs/guide.md). Per-site notes are in [docs/sites.md](docs/sites.md).

## Quick start

```sh
uv sync                                    # Python 3.12 venv in .venv
uv run dealfinder validate-config          # checks config/watchlists.yaml
uv run dealfinder test-site poshmark "patagonia better sweater"   # try one site
uv run dealfinder run-once                 # check every site group once
uv run dealfinder serve                    # keep checking + web app on http://127.0.0.1:8080
uv run pytest
```

Keys are read from environment variables. None are needed to start: sites whose key is
missing are skipped with a message, and alerts go to the console until a channel is set up.

| Variable | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | `llm` sites (Claude Haiku 5.5 by default), and checking unclear matches |
| `LLM_API_KEY` | instead, for an `openai_compatible` provider such as Z.ai GLM |
| `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | eBay Browse API (`ebay_api`) |
| `BESTBUY_API_KEY` | Best Buy Products API (`api`) |
| `DISCORD_WEBHOOK_URL` / `NTFY_TOPIC_URL` (+`NTFY_TOKEN`) / `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` / `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_EMAIL_TO` (+`SMTP_PORT`, `SMTP_FROM`) | the alert channel set in `alerts.channel` |
| `DEALFINDER_HOST` | web app address for `serve` (default `127.0.0.1`; `0.0.0.0` for the network) |
| `DEALFINDER_PASSWORD` | web app login; required unless it only listens on localhost, where empty turns it off |
| `DEALFINDER_SECRET` | keeps web logins valid across restarts |

## Commands

| Command | What it does |
|---|---|
| `validate-config` | Check the config and list every problem. |
| `init-db` | Create the database or apply pending migrations (`-c` also syncs sites). |
| `test-site SITE QUERY` | Search one site and print what it returns. Nothing is stored or alerted. |
| `run-once [-g GROUP]` | Check site groups once, then send due alerts. Exit code 2 if every site failed. |
| `serve` | Check each group on its interval, flush alerts every 5 min, serve the web app. |
| `capture-page SITE QUERY` | Save a site's cleaned page as a fixture for the LLM bake-off. |
| `bakeoff -m anthropic:MODEL -m openai:MODEL@URL` | Compare LLMs on labelled fixtures. |

## Layout

| Module | Role |
|---|---|
| `config.py` | Schema for `config/watchlists.yaml`; unknown keys are rejected. |
| `fetch.py` | Polite fetcher: robots.txt, per-site rate limits, retries, HTTP caching, optional headless Chromium, cleaned markdown. |
| `adapters.py` | One function per adapter type: site + query -> listings. |
| `extract.py` | LLM extraction (Anthropic or OpenAI-compatible), cached by page hash, with a daily token budget. |
| `match.py` | Keyword matching, optional LLM check, price history, 30-day median, score, duplicates, new-deal decisions. |
| `notify.py` | Alert channels and the alert queue (quiet hours, digest, no repeats). |
| `runner.py` | The pipeline and the scheduler. |
| `web/` | FastAPI app and templates. `config_edit.py` writes its edits back to the YAML. |
| `bakeoff.py` | LLM comparison harness. |

### Fetching pages

`Fetcher` checks robots.txt for every request (rules for `DealFinder`, else `*`; Crawl-delay
is honoured), waits for the site's `rate_limit` (keyed by host, `www.` ignored; unknown hosts
get 1 request / 2 s), retries 429/5xx and network errors with exponential backoff (honouring
`Retry-After`), and revalidates cached pages with ETag / Last-Modified (`data/http_cache/`).
`FetchResult.text` is a cleaned markdown version of the main content, which is what the LLM
sees.

A site whose listings are built with JavaScript can set `fetcher: browser` to be rendered in
headless Chromium via Playwright:

```sh
uv sync --extra browser
uv run playwright install chromium   # on a bare Linux box, also: playwright install-deps
```

### Database

SQLite in `data/dealfinder.db` (WAL mode, so the web app can read during a run).
Migrations are numbered SQL files in `src/dealfinder/migrations/`, applied in order and
tracked in `PRAGMA user_version`. To change the schema add the next `NNNN_<what>.sql`; never
edit one that has been deployed. `init-db` and `serve` apply pending migrations.
