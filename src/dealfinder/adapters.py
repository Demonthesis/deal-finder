"""Site adapters: turn one site + one search query into Listings.

- `llm`: fetch the page politely, hand the cleaned text to the LLM extractor.
- `css`: fetch the page politely, read listings with the site's CSS selectors.
- `rss`: fetch an RSS/Atom feed politely; the price is read from each entry's title.
- `ebay_api`: the official eBay Browse API (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET).
- `api`: another official API, chosen by site name (bestbuy: BESTBUY_API_KEY).

Official APIs are called directly (no robots.txt) but still share the per-site rate limit.
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import quote, urljoin, urlsplit

import feedparser
import httpx
from bs4 import BeautifulSoup, Tag

from dealfinder.config import AdapterType, Condition, CssSelectors, Site
from dealfinder.extract import Extractor, LLMConfigError, TokenUsage
from dealfinder.fetch import Fetcher, domain_key
from dealfinder.models import Listing, normalize_condition, parse_price


class AdapterError(Exception):
    """A site could not be searched."""


class AdapterUnavailable(AdapterError):
    """The adapter cannot run at all right now (missing credentials, no LLM); not a site fault."""


@dataclass
class SearchResult:
    listings: list[Listing]
    usage: TokenUsage = field(default_factory=TokenUsage)
    cached: bool = False


@dataclass
class AdapterContext:
    fetcher: Fetcher
    extractor: Extractor | None = None
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=30.0))
    env: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    clock: Callable[[], float] = time.time
    _tokens: dict[str, tuple[str, float]] = field(default_factory=dict)

    def api_request(self, site: Site, method: str, url: str, **kwargs) -> httpx.Response:
        """Call an official API under the site's rate limit."""
        domain = domain_key(url)
        self.fetcher.limiter.set_limit(domain, site.rate_limit)
        self.fetcher.limiter.wait(domain)
        try:
            response = self.http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise AdapterError(f"{site.name}: could not reach {urlsplit(url).netloc}: {exc}") from exc
        if response.status_code in (401, 403):
            raise AdapterUnavailable(f"{site.name}: the API rejected our credentials")
        if response.status_code >= 400:
            raise AdapterError(f"{site.name}: API answered {response.status_code}")
        return response

    def require_env(self, site: Site, *names: str) -> list[str]:
        missing = [n for n in names if not self.env.get(n)]
        if missing:
            raise AdapterUnavailable(f"{site.name}: set {', '.join(missing)} to use this site")
        return [self.env[n] for n in names]


def search(site: Site, query: str, ctx: AdapterContext) -> SearchResult:
    handler = _ADAPTERS[site.adapter]
    return handler(site, query, ctx)


def _llm(site: Site, query: str, ctx: AdapterContext) -> SearchResult:
    if ctx.extractor is None:
        raise AdapterUnavailable(f"{site.name}: no LLM configured for adapter llm")
    page = ctx.fetcher.fetch(site.page_url(query))
    try:
        extraction = ctx.extractor.extract(page.text, page.final_url, site=site.name)
    except LLMConfigError as exc:
        raise AdapterUnavailable(f"{site.name}: {exc}") from exc
    return SearchResult(extraction.listings, extraction.usage, extraction.cached)


def _select(element: Tag, selector: str | None) -> str | None:
    """Text (or `@attr` value) of the first match of `selector` inside `element`."""
    if not selector:
        return None
    css, _, attr = selector.partition("@")
    found = element.select_one(css.strip()) if css.strip() else element
    if found is None:
        return None
    value = found.get(attr.strip()) if attr else found.get_text(" ", strip=True)
    if isinstance(value, list):
        value = " ".join(value)
    return value or None


def parse_css(html: str, page_url: str, site_name: str, selectors: CssSelectors) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    listings: dict[str, Listing] = {}
    for element in soup.select(selectors.listing):
        title = _select(element, selectors.title)
        href = _select(element, selectors.url)
        if not title or not href:
            continue
        url = urljoin(page_url, href)
        if urlsplit(url).scheme not in ("http", "https") or url in listings:
            continue
        listings[url] = Listing(
            site=site_name,
            url=url,
            title=title,
            price=parse_price(_select(element, selectors.price)),
            condition=normalize_condition(_select(element, selectors.condition)),
        )
    return list(listings.values())


def _css(site: Site, query: str, ctx: AdapterContext) -> SearchResult:
    assert site.selectors is not None  # enforced by the config schema
    page = ctx.fetcher.fetch(site.page_url(query))
    return SearchResult(parse_css(page.html, page.final_url, site.name, site.selectors))


def parse_feed(xml: str, site_name: str) -> list[Listing]:
    feed = feedparser.parse(xml)
    if not feed.entries and (feed.bozo or not feed.get("version")):
        raise AdapterError(f"{site_name}: the response is not an RSS/Atom feed")
    listings = []
    for entry in feed.entries:
        title, link = entry.get("title"), entry.get("link")
        if not title or not link:
            continue
        listings.append(Listing(
            site=site_name, url=link, title=title, price=parse_price(title, require_currency=True),
            condition=normalize_condition(title),
        ))
    return listings


def _rss(site: Site, query: str, ctx: AdapterContext) -> SearchResult:
    page = ctx.fetcher.fetch(site.page_url(query))
    return SearchResult(parse_feed(page.html, site.name))


# --- official APIs ---------------------------------------------------------------------

EBAY_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
EBAY_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"


def _ebay_token(site: Site, ctx: AdapterContext) -> str:
    cached = ctx._tokens.get("ebay")
    if cached and cached[1] > ctx.clock() + 60:
        return cached[0]
    client_id, secret = ctx.require_env(site, "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET")
    basic = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    response = ctx.api_request(
        site, "POST", EBAY_TOKEN_URL,
        headers={"Authorization": f"Basic {basic}"},
        data={"grant_type": "client_credentials",
              "scope": "https://api.ebay.com/oauth/api_scope"},
    )
    data = response.json()
    ctx._tokens["ebay"] = (data["access_token"], ctx.clock() + float(data.get("expires_in", 7200)))
    return data["access_token"]


def _ebay_api(site: Site, query: str, ctx: AdapterContext) -> SearchResult:
    token = _ebay_token(site, ctx)
    response = ctx.api_request(
        site, "GET", EBAY_SEARCH_URL,
        params={"q": query, "limit": 50},
        headers={"Authorization": f"Bearer {token}",
                 "X-EBAY-C-MARKETPLACE-ID": ctx.env.get("EBAY_MARKETPLACE", "EBAY_US")},
    )
    listings = []
    for item in response.json().get("itemSummaries", []):
        price = item.get("price") or {}
        if not item.get("itemWebUrl") or not item.get("title"):
            continue
        listings.append(Listing(
            site=site.name,
            url=item["itemWebUrl"].split("?")[0],
            title=item["title"],
            price=float(price["value"]) if price.get("value") else None,
            currency=price.get("currency") or "USD",
            condition=normalize_condition(item.get("condition")),
        ))
    return SearchResult(listings)


BESTBUY_URL = "https://api.bestbuy.com/v1/products"


def _bestbuy(site: Site, query: str, ctx: AdapterContext) -> SearchResult:
    (key,) = ctx.require_env(site, "BESTBUY_API_KEY")
    terms = "&".join(f"search={quote(word, safe='')}" for word in query.split())
    response = ctx.api_request(
        site, "GET", f"{BESTBUY_URL}(({terms}))",
        params={"apiKey": key, "format": "json", "pageSize": 50,
                "show": "name,salePrice,url,onlineAvailability,condition"},
    )
    listings = []
    for product in response.json().get("products", []):
        if not product.get("url") or not product.get("name"):
            continue
        listings.append(Listing(
            site=site.name,
            url=product["url"],
            title=product["name"],
            price=product.get("salePrice"),
            condition=normalize_condition(product.get("condition")) or Condition.NEW,
            in_stock=bool(product.get("onlineAvailability", True)),
        ))
    return SearchResult(listings)


# Official APIs for adapter `api`, by site name.
API_CLIENTS: dict[str, Callable[[Site, str, AdapterContext], SearchResult]] = {
    "bestbuy": _bestbuy,
}


def _api(site: Site, query: str, ctx: AdapterContext) -> SearchResult:
    client = API_CLIENTS.get(site.name)
    if client is None:
        raise AdapterUnavailable(
            f"{site.name}: no API client for this site (known: {', '.join(sorted(API_CLIENTS))})"
        )
    return client(site, query, ctx)


_ADAPTERS: dict[AdapterType, Callable[[Site, str, AdapterContext], SearchResult]] = {
    AdapterType.LLM: _llm,
    AdapterType.CSS: _css,
    AdapterType.RSS: _rss,
    AdapterType.EBAY_API: _ebay_api,
    AdapterType.API: _api,
}
