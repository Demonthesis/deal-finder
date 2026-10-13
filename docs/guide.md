# Deal Finder guide

Everything here can be done in the web app (Watchlists and Sites pages) or by editing
`config/watchlists.yaml` (`/var/lib/dealfinder/watchlists.yaml` on the server). The web app
checks every change before saving and keeps the previous file as `watchlists.yaml.bak`; a
running `serve` picks up changes before its next run. After a hand edit, run
`dealfinder validate-config`.

## Add a list or an item

A **watchlist** is a set of items checked on one **site group**:

```yaml
watchlists:
  - name: Buy soon
    site_group: general-retail
    items:
      - name: Sony WH-1000XM6
        keywords: [sony, wh-1000xm6]      # every keyword must be in the title
        exclude: [case, ear pads, cable]  # any of these rejects the listing
        max_price: 330                    # highest price you'd pay
        currency: USD                     # listings in other currencies are ignored
        condition: [new, open_box, refurbished, excellent]   # empty = any
        query: sony wh-1000xm6            # optional: what to type into site searches
        digest: false                     # optional: true = daily digest only
        quiet_hours: {start: "23:00", end: "08:00"}           # optional, overrides alerts
```

Tips:
- Keep keywords short and specific: the brand plus a model number works best. Model numbers
  match with or without dashes and spaces ("wh-1000xm6" matches "WH1000XM6").
- Use `exclude` for accessories and lookalikes (case, pads, "for", kids, womens, vest).
- Conditions: `new`, `new_with_tags`, `open_box`, `refurbished`, `excellent`, `good`, `fair`,
  `used`. A listing whose condition can't be read is still accepted.

## Add a site and choose an adapter

Sites belong to a **site group**, which also sets how often they're checked:

```yaml
site_groups:
  - name: resale-clothing
    interval_minutes: 30
    sites:
      - name: poshmark
        url: https://poshmark.com
        search_url: https://poshmark.com/search?query={query}&type=listings
        adapter: llm
        rate_limit: {requests: 1, per_seconds: 15}
```

`search_url` is the site's search page with `{query}` where the search text goes (search the
site in a browser and copy the address). Without it, `url` itself is fetched once per run and
matched against every item, which suits a feed or a category page.

Pick the cheapest adapter that works, in this order:

| Adapter | When | Cost |
|---|---|---|
| `ebay_api` | eBay | free, needs eBay developer keys |
| `api` | a site with an official API client here (now: `bestbuy`) | free, needs that site's key |
| `rss` | the site offers an RSS/Atom feed of search results | free |
| `css` | the search page is plain HTML with a stable layout | free |
| `llm` | anything else | LLM tokens per page (cached when unchanged) |

For `css`, give selectors for one listing and, inside it, its parts. Each can end in `@attr`
to read an attribute:

```yaml
        adapter: css
        selectors:
          listing: li.search-result
          title: h3
          url: a@href          # the default
          price: .price
          condition: .condition   # optional
```

Add `fetcher: browser` when the search page only shows listings after JavaScript runs (test
with and without). It needs the `browser` extra and Chromium installed.

Then try it: the **Test this site** button on the Sites page, or
`dealfinder test-site <site> "<search>"`. If it returns nothing, check the selectors or switch
to `llm`. Write down what you chose and why in `docs/sites.md`.

**Etiquette.** Every scraped request obeys robots.txt and the site's `rate_limit`; keep
limits gentle (one request every 10–15 s is plenty for a search page). If a site blocks bots
(robots.txt, captchas, login walls), mark it unsupported in `docs/sites.md` instead of trying
to get around it. Scraped listings must link back to the site they were found on.

## How matching and scoring work

1. **Match.** Any `exclude` word -> no. All keywords -> match, unless the price is under a
   quarter of `max_price` (usually an accessory) -> unclear. At least half the keywords ->
   unclear. Unclear listings go to the LLM ("is this listing really the item?") when an LLM
   key is set; the answer is cached per title. Without an LLM they are dropped.
2. **Deal.** A match is a deal when it is in stock, in the item's currency, at or under
   `max_price`, and in an accepted condition.
3. **New deal.** A deal is alerted when the listing is new, or when its price drops below the
   price it was last alerted at. The same item cross-posted on another site (same title words
   and rounded price within 14 days) is not alerted twice.
4. **Score (0–100)**, shown in alerts and used to sort the feed:
   - up to 50 points for the price under `max_price` (full marks at 50% under),
   - up to 35 for the price under the item's 30-day median (full marks at 40% under). The
     median is over the latest price of every other matching listing seen in the last 30
     days, and needs at least 3 of them,
   - up to 15 for condition (new 15, open box 13.5, excellent 12.75, refurbished 12, good
     10.5, used or unknown 9, fair 6).
   Without a median, the price-under-target part counts for 85 points.

Alerts show the price, your target, the % below the median when known, the site, the
condition, the score and the link.

## Alerts

```yaml
alerts:
  channel: discord          # console | discord | ntfy | telegram | email
  timezone: America/New_York
  quiet_hours: {start: "22:00", end: "07:00"}   # held, then sent when quiet hours end
  digest: false             # true: every item goes into one daily message
  digest_time: "08:00"
```

Set the channel's env vars (see README). If they're missing, `serve` logs a warning and
prints alerts to its log instead. An alert that fails to send is retried at the next
flush (every 5 minutes), up to 5 times.

## The LLM token budget

```yaml
llm:
  provider: anthropic        # or openai_compatible (with base_url and LLM_API_KEY)
  model: claude-haiku-5-5
  daily_token_budget: 2000000
```

- Tokens (input + output) are counted per day (UTC) across page extraction and match
  checks. Once the budget is used up, `llm` sites are skipped and unclear matches are dropped
  until midnight UTC. Other adapters keep running. `0` means no limit.
- A page whose cleaned text hasn't changed is never sent to the model twice (cached for 14
  days), so a stable search page costs tokens only when its listings change.
- Rough cost with Haiku 5.5 ($0.10 / $0.50 per million input/output tokens): about $0.0012
  for a page of ~8K input and ~800 output tokens. Spending the whole default budget of 2M
  tokens costs about $0.20–0.30 at that mix (at most $1 if it were all output).
- The Runs page shows tokens per day and model, and each run's total.
- To compare models on your own pages: `dealfinder capture-page`, label the saved
  `meta.json` files, then `dealfinder bakeoff` (see `src/dealfinder/bakeoff.py`).

## Deploy and update

The service runs on Debian (for example a Proxmox LXC with 1 vCPU, 1 GB RAM, 8 GB disk).
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) first; the install
script doesn't download it, and stops if it can't find uv.

```sh
git clone https://github.com/Demonthesis/deal-finder.git /root/deal-finder && cd /root/deal-finder
deploy/install.sh              # or BROWSER=1 deploy/install.sh for fetcher: browser sites
nano /etc/dealfinder/env       # DEALFINDER_HOST, keys; see deploy/env.example
systemctl restart dealfinder
```

- Code: `/opt/dealfinder`. Data and config: `/var/lib/dealfinder` (mount a persistent volume
  there). Secrets: `/etc/dealfinder/env`, readable only by root and the service.
- **Web app access**, set in `/etc/dealfinder/env`:
  - `DEALFINDER_HOST=127.0.0.1` (the default): the web app is reachable only from inside the
    container. Put a tunnel or reverse proxy in front that does its own login (for example
    Cloudflare Tunnel + Access pointing at `http://127.0.0.1:8080`), and leave
    `DEALFINDER_PASSWORD` empty so you don't log in twice.
  - `DEALFINDER_HOST=0.0.0.0`: the web app is at `http://<container-ip>:8080` on your network,
    and `DEALFINDER_PASSWORD` is required (the service won't start without it).
- Logs: `journalctl -u dealfinder -f`.
- **Update:** `cd /root/deal-finder && git pull && deploy/install.sh`. It replaces the code,
  applies database migrations, keeps data, config and secrets, and restarts the service.
- **Back up:** `/var/lib/dealfinder` (stop the service or use `sqlite3 dealfinder.db .backup`).
