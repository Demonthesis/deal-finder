import json

import httpx
import pytest

from dealfinder import adapters, db
from dealfinder.adapters import AdapterContext, AdapterError, AdapterUnavailable, search
from dealfinder.config import Condition, CssSelectors, RateLimit, Site
from dealfinder.extract import ExtractionOutput, Extractor, TokenUsage
from dealfinder.fetch import Fetcher

SEARCH_HTML = """
<html><body><main>
  <ul class="results">
    <li class="card"><a class="link" href="/item/1"><h3>Patagonia Better Sweater M</h3></a>
        <span class="price">$39.99</span><span class="cond">Pre-owned</span></li>
    <li class="card"><a class="link" href="https://shop.example/item/2"><h3>Better Sweater Vest</h3></a>
        <span class="price">$1,050.00</span></li>
    <li class="card"><span>sponsored, no link</span></li>
    <li class="card"><a class="link" href="/item/1"><h3>duplicate</h3></a></li>
  </ul>
</main></body></html>
"""

FEED = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Deals</title>
<item><title>Sony WH-1000XM6 Headphones $298 at Amazon</title>
      <link>https://slickdeals.net/f/1</link></item>
<item><title>Refurbished Sony WH-1000XM6 $219.99 + Free Shipping</title>
      <link>https://slickdeals.net/f/2</link></item>
<item><title>No link here</title></item>
</channel></rss>"""


def make_site(adapter, **kw) -> Site:
    return Site(name=kw.pop("name", "shop"), url=kw.pop("url", "https://shop.example"),
                adapter=adapter, rate_limit=RateLimit(requests=100, per_seconds=1), **kw)


SELECTORS = CssSelectors(listing="li.card", title="h3", url="a.link@href",
                         price=".price", condition=".cond")


class Server:
    """httpx MockTransport routing by (host, path)."""

    def __init__(self):
        self.routes = {}
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get((request.url.host, request.url.path))
        if handler is None:
            return httpx.Response(404)
        return handler(request) if callable(handler) else handler


@pytest.fixture
def server():
    return Server()


@pytest.fixture
def ctx(server):
    fetcher = Fetcher(transport=httpx.MockTransport(server), sleep=lambda s: None)
    context = AdapterContext(
        fetcher=fetcher, http=httpx.Client(transport=httpx.MockTransport(server)), env={},
    )
    yield context
    fetcher.close()


def test_css_adapter_reads_listings_with_selectors(server, ctx):
    server.routes[("shop.example", "/search")] = httpx.Response(
        200, text=SEARCH_HTML, headers={"Content-Type": "text/html"})
    site = make_site("css", search_url="https://shop.example/search?q={query}", selectors=SELECTORS)

    result = search(site, "better sweater", ctx)

    assert [(l.url, l.title, l.price, l.condition) for l in result.listings] == [
        ("https://shop.example/item/1", "Patagonia Better Sweater M", 39.99, Condition.USED),
        ("https://shop.example/item/2", "Better Sweater Vest", 1050.0, None),
    ]
    page_request = next(r for r in server.requests if r.url.path == "/search")
    assert page_request.url.params["q"] == "better sweater"


def test_rss_adapter_reads_price_from_titles(server, ctx):
    server.routes[("slickdeals.net", "/newsearch.php")] = httpx.Response(
        200, text=FEED, headers={"Content-Type": "application/rss+xml"})
    site = make_site("rss", name="slickdeals", url="https://slickdeals.net",
                     search_url="https://slickdeals.net/newsearch.php?q={query}&rss=1")

    listings = search(site, "sony", ctx).listings

    assert [(l.url, l.price, l.condition) for l in listings] == [
        ("https://slickdeals.net/f/1", 298.0, None),
        ("https://slickdeals.net/f/2", 219.99, Condition.REFURBISHED),
    ]


def test_rss_adapter_rejects_non_feeds():
    with pytest.raises(AdapterError, match="not an RSS"):
        adapters.parse_feed("<html><body>Access denied</body></html>", "slickdeals")


def test_robots_disallow_is_respected_by_scraping_adapters(server, ctx):
    server.routes[("shop.example", "/robots.txt")] = httpx.Response(
        200, text="User-agent: *\nDisallow: /search\n")
    site = make_site("css", search_url="https://shop.example/search?q={query}", selectors=SELECTORS)
    with pytest.raises(Exception, match="robots"):
        search(site, "x", ctx)
    assert not any(r.url.path == "/search" for r in server.requests)


class FakeBackend:
    provider, model = "fake", "claude-haiku-5-5"

    def parse(self, system, user, output):
        assert "Patagonia Better Sweater" in user and "<li" not in user  # cleaned markdown
        return output.model_validate({"listings": [{
            "title": "Patagonia Better Sweater", "price": 40, "currency": "USD",
            "url": "/item/1", "condition": "NWT", "in_stock": True}]}), TokenUsage(500, 50)


def test_llm_adapter_sends_cleaned_page_to_extractor(server, ctx):
    server.routes[("shop.example", "/search")] = httpx.Response(
        200, text=SEARCH_HTML, headers={"Content-Type": "text/html"})
    conn = db.connect(":memory:")
    db.migrate(conn)
    ctx.extractor = Extractor(FakeBackend(), conn)
    site = make_site("llm", search_url="https://shop.example/search?q={query}")

    result = search(site, "sweater", ctx)
    again = search(site, "sweater", ctx)

    assert result.listings[0].url == "https://shop.example/item/1"
    assert result.listings[0].condition is Condition.NEW_WITH_TAGS
    assert result.usage == TokenUsage(500, 50)
    assert again.cached and again.usage == TokenUsage()


def test_llm_adapter_without_llm_is_unavailable(ctx):
    with pytest.raises(AdapterUnavailable):
        search(make_site("llm"), "x", ctx)


def test_ebay_api_adapter(server, ctx):
    token_calls = []

    def token(request):
        token_calls.append(request)
        assert request.headers["Authorization"].startswith("Basic ")
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 7200})

    def results(request):
        assert request.headers["Authorization"] == "Bearer tok"
        assert request.url.params["q"] == "red wing iron ranger"
        return httpx.Response(200, json={"itemSummaries": [
            {"title": "Red Wing Iron Ranger 8111 10D", "itemWebUrl": "https://www.ebay.com/itm/1?hash=x",
             "price": {"value": "149.99", "currency": "USD"}, "condition": "Pre-owned"},
            {"title": "no url"},
        ]})

    server.routes[("api.ebay.com", "/identity/v1/oauth2/token")] = token
    server.routes[("api.ebay.com", "/buy/browse/v1/item_summary/search")] = results
    ctx.env = {"EBAY_CLIENT_ID": "id", "EBAY_CLIENT_SECRET": "secret"}
    site = make_site("ebay_api", name="ebay", url="https://www.ebay.com")

    listings = search(site, "red wing iron ranger", ctx).listings
    search(site, "red wing iron ranger", ctx)

    assert [(l.url, l.price, l.condition) for l in listings] == [
        ("https://www.ebay.com/itm/1", 149.99, Condition.USED)]
    assert len(token_calls) == 1  # token reused


def test_api_adapters_need_credentials(ctx):
    with pytest.raises(AdapterUnavailable, match="EBAY_CLIENT_ID"):
        search(make_site("ebay_api", name="ebay"), "x", ctx)
    with pytest.raises(AdapterUnavailable, match="BESTBUY_API_KEY"):
        search(make_site("api", name="bestbuy"), "x", ctx)
    with pytest.raises(AdapterUnavailable, match="no API client"):
        search(make_site("api", name="walmart"), "x", ctx)


def test_bestbuy_api_adapter(server, ctx):
    def products(request):
        assert "search=sony&search=wh-1000xm6" in request.url.path
        assert request.url.params["apiKey"] == "k"
        return httpx.Response(200, json={"products": [
            {"name": "Sony WH-1000XM6", "salePrice": 329.99, "url": "https://www.bestbuy.com/site/1",
             "onlineAvailability": False, "condition": "New"},
        ]})

    server.routes[("api.bestbuy.com", "/v1/products((search=sony&search=wh-1000xm6))")] = products
    ctx.env = {"BESTBUY_API_KEY": "k"}

    [listing] = search(make_site("api", name="bestbuy"), "sony wh-1000xm6", ctx).listings
    assert (listing.price, listing.condition, listing.in_stock) == (329.99, Condition.NEW, False)


def test_api_rejected_credentials_are_unavailable(server, ctx):
    server.routes[("api.bestbuy.com", "/v1/products((search=x))")] = httpx.Response(403)
    ctx.env = {"BESTBUY_API_KEY": "bad"}
    with pytest.raises(AdapterUnavailable):
        search(make_site("api", name="bestbuy"), "x", ctx)


def test_search_url_must_contain_query_placeholder():
    with pytest.raises(ValueError, match="query"):
        make_site("llm", search_url="https://shop.example/search")
    assert json.dumps(make_site("llm", search_url="https://s.example/?q={query}")
                      .page_url("a b&c")) == '"https://s.example/?q=a+b%26c"'


def test_scraped_listings_must_link_back_to_the_site(server, ctx):
    html = SEARCH_HTML.replace('href="/item/1"', 'href="https://evil.example/phish"')
    html = html.replace('href="https://shop.example/item/2"', 'href="https://m.shop.example/item/2"')
    server.routes[("shop.example", "/search")] = httpx.Response(
        200, text=html, headers={"Content-Type": "text/html"})
    site = make_site("css", search_url="https://shop.example/search?q={query}", selectors=SELECTORS)
    assert [l.url for l in search(site, "x", ctx).listings] == ["https://m.shop.example/item/2"]
