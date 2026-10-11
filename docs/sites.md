# Sites

Which adapter each site uses and why. A site that blocks automated access (robots.txt,
bot walls) is marked **unsupported** instead of being worked around.

Check a site by hand with `uv run dealfinder test-site <site> "<search>"`.

| Site | Adapter | Status | Notes |
|---|---|---|---|
| eBay | `ebay_api` | Needs `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | Official Browse API; no scraping. |
| Best Buy | `api` | Needs `BESTBUY_API_KEY` | Official Products API. |
| Poshmark | `llm` | Not yet checked against the live site | Search page URL is a first guess. |
| Grailed | `llm` + `fetcher: browser` | Not yet checked against the live site | Listings are built with JavaScript. |
| Slickdeals | – | **Unsupported** | robots.txt has `Disallow: /newsearch.php?*rss=*`, which covers the RSS search. Checked 2026-10-11. |

The sites in the owner's `uploads/watchlists.md` get added here as they're checked.
