import sys
import threading
import time
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from dealfinder.config import FetcherType, RateLimit, Site, parse_config
from dealfinder.fetch import (
    CachedResponse,
    FetchError,
    Fetcher,
    FileCache,
    PlaywrightRenderer,
    RenderedPage,
    RobotsDisallowed,
    clean_html,
    domain_key,
)

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClock:
    """Monotonic clock where sleeping just moves time forward."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Server:
    """Mock transport: routes by (host, path), records every request with its time."""

    def __init__(self, clock: FakeClock, robots: dict[str, str | int] | None = None):
        self.clock = clock
        self.robots = robots or {}
        self.pages: dict[tuple[str, str], list[httpx.Response] | httpx.Response] = {}
        self.requests: list[tuple[float, httpx.Request]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((self.clock(), request))
        host, path = request.url.host, request.url.path
        if path == "/robots.txt":
            robots = self.robots.get(host, 404)
            if isinstance(robots, int):
                return httpx.Response(robots)
            return httpx.Response(200, text=robots)
        page = self.pages.get((host, path))
        if page is None:
            return httpx.Response(404, text="not found")
        if isinstance(page, list):
            page = page.pop(0) if len(page) > 1 else page[0]
        if callable(page):
            return page(request)
        return page

    def times(self, host: str, path: str | None = None) -> list[float]:
        return [t for t, r in self.requests
                if r.url.host == host and (path is None or r.url.path == path)]

    def paths(self) -> list[str]:
        return [r.url.path for _, r in self.requests]


def site(name: str, url: str, requests: int = 1, per_seconds: float = 10, **kw) -> Site:
    return Site(name=name, url=url, adapter="llm",
                rate_limit=RateLimit(requests=requests, per_seconds=per_seconds), **kw)


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make(clock):
    created = []

    def _make(sites=(), robots=None, **kw):
        server = Server(clock, robots)
        fetcher = Fetcher(sites, transport=httpx.MockTransport(server.handler), clock=clock,
                          sleep=clock.sleep, jitter=lambda: 0.0, **kw)
        created.append(fetcher)
        return fetcher, server

    yield _make
    for f in created:
        f.close()


def ok(text="<html><body><main><p>hello</p></main></body></html>", **headers):
    return httpx.Response(200, text=text, headers={"Content-Type": "text/html", **headers})


# --- robots.txt ----------------------------------------------------------------------


def test_robots_disallowed_path_is_never_requested(make):
    fetcher, server = make(robots={"shop.test": "User-agent: *\nDisallow: /private\n"})
    server.pages[("shop.test", "/private/deals")] = ok()
    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("https://shop.test/private/deals")
    assert server.paths() == ["/robots.txt"]


def test_robots_allowed_path_is_fetched_and_robots_cached(make):
    fetcher, server = make(robots={"shop.test": "User-agent: *\nDisallow: /private\n"})
    server.pages[("shop.test", "/search")] = ok()
    fetcher.fetch("https://shop.test/search?q=a")
    fetcher.fetch("https://shop.test/search?q=b")
    assert server.paths() == ["/robots.txt", "/search", "/search"]


def test_robots_rules_for_our_agent_apply(make):
    robots = "User-agent: DealFinder\nDisallow: /search\n\nUser-agent: *\nAllow: /\n"
    fetcher, server = make(robots={"shop.test": robots})
    assert not fetcher.allowed("https://shop.test/search?q=x")
    assert fetcher.allowed("https://shop.test/about")


def test_robots_wildcard_query_is_never_requested(make):
    fetcher, server = make(robots={"shop.test": "User-agent: *\nDisallow: /*?\n"})
    server.pages[("shop.test", "/search")] = ok()
    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("https://shop.test/search?q=coat")
    assert server.paths() == ["/robots.txt"]
    assert fetcher.allowed("https://shop.test/search")


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    ("rules", "path", "allowed"),
    [
        (["Disallow: /", "Allow: /public"], "/public/deal", True),
        (["Allow: /", "Disallow: /private"], "/private/deal", False),
        (["Disallow: /public", "Allow: /public"], "/public/deal", True),
        (["Disallow: /*", "Allow: /public/*"], "/public/deal", True),
        (["Allow: /public/*", "Disallow: /public/private"], "/public/private/deal", False),
        (["Disallow: /*.pdf$"], "/catalog.pdf", False),
        (["Disallow: /*.pdf$"], "/catalog.pdf?download=1", True),
        (["Disallow: /*.pdf$"], "/catalog.pdf/preview", True),
        (["Disallow: /*.pdf$"], "/catalogXpdf", True),
        (["Disallow: /private$"], "/private", False),
        (["Disallow: /private$"], "/private/deal", True),
        (["Disallow: /items/*/sale$"], "/items/coat/sale", False),
        (["Disallow: /items/*/sale$"], "/items//sale", False),
        (["Disallow: /items/*/sale$"], "/items/coat/sale/today", True),
        (["Disallow:", "Disallow: /private"], "/private", False),
        (["Disallow: /file%2A$"], "/file%2A", False),
        (["Disallow: /file%2A$"], "/file-other", True),
        (["Disallow: /file%24"], "/file%24/deal", False),
        (["Disallow: /file%24"], "/file", True),
        (["Disallow: /café$"], "/caf%C3%A9", False),
        (["Disallow: /private"], "/%70rivate", False),
        (["Disallow: /private$"], "/private#deal", False),
        (["Disallow: /Private"], "/private", True),
        (["Disallow: /public******", "Allow: /public/deal"], "/public/deal", True),
        (["Disallow: /public/*", "Allow: /public/"], "/public/deal", True),
    ],
)
def test_robots_patterns_and_specificity(make, reverse, rules, path, allowed):
    rules = list(reversed(rules)) if reverse else rules
    fetcher, server = make(robots={"shop.test": "User-agent: *\n" + "\n".join(rules)})
    assert fetcher.allowed("https://shop.test" + path) is allowed
    assert server.paths() == ["/robots.txt"]


def test_robots_public_exception_is_fetched(make):
    fetcher, server = make(robots={"shop.test": "User-agent: *\nDisallow: /\nAllow: /public\n"})
    server.pages[("shop.test", "/public/deal")] = ok()
    assert fetcher.fetch("https://shop.test/public/deal").status_code == 200
    assert server.paths() == ["/robots.txt", "/public/deal"]


def test_robots_patterns_respect_agent_group(make):
    robots = (
        "User-agent: OtherBot\nDisallow: /\n\n"
        "User-agent: DealFinder\nDisallow: /*?\nAllow: /public*\n\n"
        "User-agent: *\nDisallow: /\n"
    )
    fetcher, _ = make(robots={"shop.test": robots})
    assert not fetcher.allowed("https://shop.test/search?q=coat")
    assert fetcher.allowed("https://shop.test/public?q=coat")
    assert fetcher.allowed("https://shop.test/search")


@pytest.mark.parametrize(
    ("robots_status", "allowed"),
    [(404, True), (410, True), (401, False), (403, False), (500, False), (503, False)],
)
def test_robots_status_handling(make, robots_status, allowed):
    fetcher, _ = make(robots={"shop.test": robots_status}, max_retries=0)
    assert fetcher.allowed("https://shop.test/anything") is allowed


def test_robots_unreachable_means_disallowed(clock):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    fetcher = Fetcher(transport=httpx.MockTransport(boom), clock=clock, sleep=clock.sleep,
                      max_retries=1)
    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("https://down.test/page")


def test_non_http_urls_are_rejected(make):
    fetcher, server = make()
    with pytest.raises(FetchError, match="only http"):
        fetcher.fetch("file:///etc/passwd")
    assert server.requests == []


# --- redirects -----------------------------------------------------------------------


@pytest.mark.parametrize("destination_host", ["shop.test", "other.test"])
def test_redirect_to_disallowed_path_is_never_requested(make, destination_host):
    fetcher, server = make(robots={destination_host: "User-agent: *\nDisallow: /private\n"})
    destination = f"https://{destination_host}/private"
    server.pages[("shop.test", "/start")] = httpx.Response(
        302, headers={"Location": destination}
    )
    server.pages[(destination_host, "/private")] = ok()

    with pytest.raises(RobotsDisallowed) as exc_info:
        fetcher.fetch("https://shop.test/start")

    assert exc_info.value.url == destination
    assert "/private" not in server.paths()


def test_each_redirect_hop_reserves_its_domain_limit(make):
    fetcher, server = make([site("other", "https://other.test", per_seconds=5)])
    server.pages[("shop.test", "/start")] = httpx.Response(
        302, headers={"Location": "/next"}
    )
    server.pages[("shop.test", "/next")] = httpx.Response(
        307, headers={"Location": "https://other.test/final"}
    )
    server.pages[("other.test", "/final")] = ok()

    result = fetcher.fetch("https://shop.test/start")

    assert result.final_url == "https://other.test/final"
    assert server.times("shop.test") == [1000.0, 1002.0, 1004.0]
    assert server.times("other.test") == [1004.0, 1009.0]


def test_redirect_destination_retries_are_rate_limited(make):
    fetcher, server = make()
    server.pages[("shop.test", "/start")] = httpx.Response(
        302, headers={"Location": "/final"}
    )
    server.pages[("shop.test", "/final")] = [httpx.Response(503), ok()]

    assert fetcher.fetch("https://shop.test/start").status_code == 200
    assert server.times("shop.test", "/start") == [1002.0]
    assert server.times("shop.test", "/final") == [1004.0, 1006.0]


def test_redirect_loop_is_bounded(make):
    fetcher, server = make()
    server.pages[("shop.test", "/loop")] = httpx.Response(
        302, headers={"Location": "/loop"}
    )

    with pytest.raises(FetchError, match="too many redirects"):
        fetcher.fetch("https://shop.test/loop")

    times = server.times("shop.test", "/loop")
    assert len(times) == fetcher._client.max_redirects + 1
    assert all(b - a == 2 for a, b in zip(times, times[1:]))


# --- rate limits ---------------------------------------------------------------------


def test_per_domain_rate_limit_spaces_requests(make):
    fetcher, server = make([site("slow", "https://www.slow.test", requests=1, per_seconds=10)])
    server.pages[("www.slow.test", "/s")] = ok()
    for _ in range(3):
        fetcher.fetch("https://www.slow.test/s")
    times = server.times("www.slow.test")  # robots.txt counts against the limit too
    assert len(times) == 4
    assert all(b - a >= 10 for a, b in zip(times, times[1:]))


def test_rate_limit_allows_bursts_up_to_window(make):
    fetcher, server = make([site("fast", "https://fast.test", requests=5, per_seconds=1)])
    server.pages[("fast.test", "/s")] = ok()
    for _ in range(9):  # + robots.txt = 10 requests
        fetcher.fetch("https://fast.test/s")
    times = server.times("fast.test")
    assert times[:5] == [times[0]] * 5
    assert times[5] >= times[0] + 1
    for i in range(5, len(times)):
        assert times[i] - times[i - 5] >= 1  # never more than 5 in any 1s window


def test_domains_are_limited_independently(make, clock):
    fetcher, server = make([site("a", "https://a.test"), site("b", "https://b.test")])
    server.pages[("a.test", "/")] = ok()
    server.pages[("b.test", "/")] = ok()
    fetcher.fetch("https://a.test/")
    start = clock()
    fetcher.fetch("https://b.test/")
    # b only waits for its own robots.txt request, not for a's limit
    assert server.times("b.test", "/") == [start + 10]


def test_www_and_bare_host_share_a_limit():
    assert domain_key("https://www.ebay.com/sch") == domain_key("https://EBAY.com/") == "ebay.com"


def test_unknown_domain_uses_default_limit(make):
    fetcher, server = make(default_rate_limit=RateLimit(requests=1, per_seconds=3))
    server.pages[("other.test", "/")] = ok()
    fetcher.fetch("https://other.test/")
    robots_t, page_t = server.times("other.test")
    assert page_t - robots_t == 3


def test_robots_crawl_delay_slows_down_a_fast_site(make):
    fetcher, server = make([site("fast", "https://fast.test", requests=10, per_seconds=1)],
                           robots={"fast.test": "User-agent: *\nCrawl-delay: 4\n"})
    server.pages[("fast.test", "/s")] = ok()
    fetcher.fetch("https://fast.test/s")
    fetcher.fetch("https://fast.test/s")
    times = server.times("fast.test", "/s")
    assert times[1] - times[0] == 4


# --- retries -------------------------------------------------------------------------


def test_retries_server_errors_with_exponential_backoff(make, clock):
    # generous rate limit so the only sleeps are the backoff ones
    fetcher, server = make([site("shop", "https://shop.test", requests=100, per_seconds=1)],
                           backoff_base=1.0)
    server.pages[("shop.test", "/s")] = [httpx.Response(503), httpx.Response(502), ok()]
    result = fetcher.fetch("https://shop.test/s")
    assert result.status_code == 200
    assert server.paths().count("/s") == 3
    assert clock.sleeps == [1.0, 2.0]


def test_honours_retry_after(make, clock):
    fetcher, server = make()
    server.pages[("shop.test", "/s")] = [httpx.Response(429, headers={"Retry-After": "7"}),
                                         ok()]
    fetcher.fetch("https://shop.test/s")
    assert 7.0 in clock.sleeps


def test_gives_up_after_max_retries(make):
    fetcher, server = make(max_retries=2)
    server.pages[("shop.test", "/s")] = [httpx.Response(500)]
    with pytest.raises(FetchError) as exc_info:
        fetcher.fetch("https://shop.test/s")
    assert exc_info.value.status_code == 500
    assert server.paths().count("/s") == 3


def test_does_not_retry_client_errors(make):
    fetcher, server = make()
    server.pages[("shop.test", "/s")] = [httpx.Response(404)]
    with pytest.raises(FetchError):
        fetcher.fetch("https://shop.test/s")
    assert server.paths().count("/s") == 1


def test_retries_transport_errors(make):
    fetcher, server = make()
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return ok()

    server.pages[("shop.test", "/s")] = flaky
    assert fetcher.fetch("https://shop.test/s").status_code == 200
    assert calls["n"] == 2


# --- headers and caching -------------------------------------------------------------


def test_sends_realistic_browser_headers(make):
    fetcher, server = make()
    server.pages[("shop.test", "/s")] = ok()
    fetcher.fetch("https://shop.test/s")
    headers = server.requests[-1][1].headers
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert "Chrome/" in headers["User-Agent"]
    assert "text/html" in headers["Accept"]
    assert headers["Accept-Language"].startswith("en")


def test_etag_revalidation_uses_cache(make):
    fetcher, server = make()
    body = "<html><body><main><p>listing</p></main></body></html>"

    def respond(request):
        if request.headers.get("If-None-Match") == '"v1"':
            return httpx.Response(304)
        return ok(body, ETag='"v1"')

    server.pages[("shop.test", "/s")] = respond
    first = fetcher.fetch("https://shop.test/s")
    second = fetcher.fetch("https://shop.test/s")
    assert not first.from_cache
    assert second.from_cache
    assert second.html == body
    assert second.text == "listing"


def test_last_modified_revalidation(make):
    fetcher, server = make()
    stamp = "Wed, 07 Oct 2026 10:00:00 GMT"
    seen = []

    def respond(request):
        seen.append(request.headers.get("If-Modified-Since"))
        return httpx.Response(304) if seen[-1] == stamp else ok(**{"Last-Modified": stamp})

    server.pages[("shop.test", "/s")] = respond
    fetcher.fetch("https://shop.test/s")
    assert fetcher.fetch("https://shop.test/s").from_cache
    assert seen == [None, stamp]


def test_responses_without_validators_are_not_cached(make):
    fetcher, server = make()
    server.pages[("shop.test", "/s")] = ok()
    fetcher.fetch("https://shop.test/s")
    fetcher.fetch("https://shop.test/s")
    assert all("If-None-Match" not in r.headers for _, r in server.requests)


def test_file_cache_round_trip(tmp_path):
    cache = FileCache(tmp_path / "cache")
    assert cache.get("https://a.test/") is None
    entry = CachedResponse("https://a.test/", 200, "<p>x</p>", '"e"', None)
    cache.set("https://a.test/", entry)
    assert FileCache(tmp_path / "cache").get("https://a.test/") == entry


# --- browser fallback ----------------------------------------------------------------


class FakeRenderer:
    def __init__(self, clock=None):
        self.clock = clock
        self.rendered: list[str] = []
        self.times: list[float] = []
        self.closed = False

    def render(self, url, *, user_agent, timeout, before_request):
        before_request(url)
        self.rendered.append(url)
        if self.clock:
            self.times.append(self.clock())
        return RenderedPage(url, 200, "<html><body><main><p>rendered by js</p></main></body></html>")

    def close(self):
        self.closed = True


def test_site_can_switch_to_browser_in_config(make, example_data):
    example_data["site_groups"][0]["sites"][2]["fetcher"] = "browser"  # poshmark
    example_data["site_groups"][0]["sites"][1]["fetcher"] = "http"  # grailed
    cfg = parse_config(example_data)
    assert cfg.sites()["poshmark"].fetcher is FetcherType.BROWSER
    assert cfg.sites()["grailed"].fetcher is FetcherType.HTTP

    renderer = FakeRenderer()
    fetcher, server = make(cfg.sites().values(), renderer=renderer)
    server.pages[("www.grailed.com", "/shop")] = ok()

    posh = fetcher.fetch("https://poshmark.com/search?query=patagonia")
    assert posh.fetcher is FetcherType.BROWSER
    assert posh.text == "rendered by js"
    assert renderer.rendered == ["https://poshmark.com/search?query=patagonia"]
    # robots.txt is still checked over plain HTTP; the page itself is not
    assert [r.url.path for _, r in server.requests if r.url.host == "poshmark.com"] == [
        "/robots.txt"
    ]

    grailed = fetcher.fetch("https://www.grailed.com/shop")
    assert grailed.fetcher is FetcherType.HTTP
    assert renderer.rendered == ["https://poshmark.com/search?query=patagonia"]
    fetcher.close()
    assert renderer.closed


def test_browser_fetch_respects_robots_and_rate_limit(make, clock):
    renderer = FakeRenderer(clock)
    fetcher, server = make([site("js", "https://js.test", fetcher="browser")],
                           robots={"js.test": "User-agent: *\nDisallow: /cart\n"},
                           renderer=renderer)
    with pytest.raises(RobotsDisallowed):
        fetcher.fetch("https://js.test/cart")
    fetcher.fetch("https://js.test/a")
    fetcher.fetch("https://js.test/b")
    assert renderer.rendered == ["https://js.test/a", "https://js.test/b"]
    [robots_t] = server.times("js.test", "/robots.txt")
    assert renderer.times == [robots_t + 10, robots_t + 20]


def test_browser_interception_checks_every_destination(make, clock, monkeypatch):
    # Exercise the production interceptor without requiring Playwright or Chromium.
    monkeypatch.setitem(sys.modules, "playwright", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "playwright.sync_api", SimpleNamespace(
        TimeoutError=TimeoutError, Error=RuntimeError,
    ))
    destinations = [
        ("https://shop.test/start", "Document"),
        ("https://shop.test/private", "Document"),
        ("https://shop.test/private/api", "Fetch"),
        ("https://shop.test/private/api?page=2", "Fetch"),
        ("https://shop.test/script.js", "Script"),
        ("https://shop.test/style.css", "Stylesheet"),
        ("https://shop.test/api", "Fetch"),
        ("https://shop.test/api?page=2", "XHR"),
        ("https://other.test/private", "Document"),
        ("https://other.test/api", "Fetch"),
        ("https://other.test/api?page=2", "Fetch"),
        ("https://shop.test/image.png", "Image"),
    ]
    decisions = []
    handlers = {}

    class Session:
        def on(self, event, callback):
            assert event == "Fetch.requestPaused"
            self.callback = callback

        def send(self, method, params):
            if method == "Fetch.enable":
                assert params == {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}
                return
            if params["requestId"] != "initial":
                decisions.append((params["requestId"], method, clock()))

    session = Session()

    class Page:
        url = "https://shop.test/start"

        def goto(self, url, **kwargs):
            # The CDP interceptor delegates original requests to context routes.
            session.callback({"requestId": "initial"})
            for index, (destination, resource_type) in enumerate(destinations):
                if index in (1, 8):
                    session.callback({
                        "requestId": str(index), "request": {"url": destination},
                        "resourceType": resource_type, "redirectedRequestId": "initial",
                    })
                else:
                    route = SimpleNamespace(
                        request=SimpleNamespace(
                            url=destination, resource_type=resource_type.lower(),
                            frame=SimpleNamespace(page=self),
                        ),
                        abort=lambda: decisions.append((str(index), "Fetch.failRequest", clock())),
                        continue_=lambda: decisions.append((str(index), "Fetch.continueRequest", clock())),
                    )
                    handlers["route"](route)
            return SimpleNamespace(status=200)

        def wait_for_load_state(self, *args, **kwargs):
            pass

        def content(self):
            return "<main>rendered</main>"

    context = SimpleNamespace(
        new_page=Page, new_cdp_session=lambda page: session,
        route=lambda pattern, callback: handlers.update(route=callback), close=lambda: None,
    )

    def new_context(**kwargs):
        assert kwargs["service_workers"] == "block"
        return context

    renderer = PlaywrightRenderer()
    monkeypatch.setattr(renderer, "_ensure_browser", lambda: SimpleNamespace(new_context=new_context))
    fetcher, server = make(
        [site("other", "https://other.test", per_seconds=5)],
        robots={host: "User-agent: *\nDisallow: /private\n"
                for host in ("shop.test", "other.test")},
        renderer=renderer,
    )

    assert fetcher.fetch("https://shop.test/start", fetcher=FetcherType.BROWSER).text == "rendered"
    assert [index for index, method, _ in decisions if method == "Fetch.failRequest"] == [
        "1", "2", "3", "8", "11",
    ]
    assert [(index, when) for index, method, when in decisions
            if method == "Fetch.continueRequest"] == [
        ("0", 1002), ("4", 1004), ("5", 1006), ("6", 1008), ("7", 1010),
        ("9", 1015), ("10", 1020),
    ]
    assert server.times("shop.test") == [1000]
    assert server.times("other.test") == [1010]


def test_invalid_fetcher_value_is_rejected(example_data):
    from dealfinder.config import ConfigError

    example_data["site_groups"][0]["sites"][0]["fetcher"] = "selenium"
    with pytest.raises(ConfigError) as exc_info:
        parse_config(example_data)
    assert "fetcher" in exc_info.value.errors[0]


# --- cleaning ------------------------------------------------------------------------


def test_cleaned_sample_page_is_much_smaller_and_keeps_listings():
    html = (FIXTURES / "sample_search_page.html").read_text(encoding="utf-8")
    text = clean_html(html, "https://www.example-resale.com/search?q=patagonia")
    assert len(text) * 10 < len(html), (len(html), len(text))
    assert "Results for" in text
    assert "](https://www.example-resale.com/listing/10000000?src=search)" in text
    assert text.count("Condition:") == 24
    assert "$" in text
    for junk in ("gtag", "__NEXT_DATA__", "<svg", "display:flex", "Category 3",
                 "Help topic", "cookies", "rendered by edge"):
        assert junk not in text


def test_clean_html_falls_back_to_body_without_main():
    html = "<html><body><nav>menu</nav><div><h2>Deal</h2><p>Price $5</p></div></body></html>"
    assert clean_html(html) == "## Deal\n\nPrice $5"


def test_clean_html_drops_hidden_and_keeps_image_alt():
    html = ('<body><p hidden>secret</p><p aria-hidden="true">icon</p>'
            '<p><img src="x.jpg" alt="Navy sweater"> <a href="#top">top</a></p></body>')
    assert clean_html(html) == "Navy sweater top"


# --- real browser (skipped unless Playwright and Chromium are installed) -------------


@pytest.fixture
def browser_renderer():
    pytest.importorskip("playwright.sync_api")
    from dealfinder.fetch import PlaywrightRenderer

    renderer = PlaywrightRenderer(settle_ms=1000)
    try:
        renderer._ensure_browser()
    except Exception as exc:
        pytest.skip(f"Chromium unavailable: {exc}")
    try:
        yield renderer
    finally:
        renderer.close()


@pytest.fixture
def polite_browser_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((time.monotonic(), self.path))
            if self.path == "/robots.txt":
                body = b"User-agent: *\nDisallow: /private\n"
            elif self.path in (
                "/redirect", "/allowed-redirect", "/hop2", "/redirect-api", "/popup-redirect",
            ):
                self.send_response(302)
                destination = {
                    "/redirect": "/private", "/allowed-redirect": "/hop2", "/hop2": "/final",
                    "/redirect-api": "/private/api?page=3",
                    "/popup-redirect": "/private/popup-redirected",
                }[self.path]
                self.send_header("Location", destination)
                self.end_headers()
                return
            elif self.path == "/start":
                body = b"""<html><body><main>allowed</main>
                  <iframe src="http://localhost:PORT/frame"></iframe>
                  <script src="/script.js"></script><link rel="stylesheet" href="/style.css">
                  <script>
                    window.open('/private/popup');
                    window.open('/popup-redirect');
                    Promise.all(['/private/api', '/private/api?page=2', '/api', '/api?page=2',
                                 '/redirect-api']
                      .map(url => fetch(url).catch(() => {})))
                      .then(() => document.querySelector('main').textContent = 'done');
                  </script></body></html>""".replace(b"PORT", str(self.server.server_port).encode())
            elif self.path == "/frame":
                body = b"<script>fetch('/private/frame-api').catch(() => {})</script>"
            else:
                body = b""
            self.send_response(200)
            content_type = "text/plain" if self.path == "/robots.txt" else "text/html"
            if self.path.endswith(".js"):
                content_type = "application/javascript"
            elif self.path.endswith(".css"):
                content_type = "text/css"
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


@pytest.mark.parametrize("path", ["/redirect", "/allowed-redirect", "/start"])
def test_browser_requests_enforce_robots_and_rate_limits(
    browser_renderer, polite_browser_server, path, monkeypatch
):
    origin, requests = polite_browser_server
    reservations = []
    with Fetcher(
        [site("browser", origin, fetcher="browser", per_seconds=0.1),
         site("frame", origin.replace("127.0.0.1", "localhost"), per_seconds=0.1)],
        renderer=browser_renderer,
    ) as fetcher:
        wait = fetcher.limiter.wait

        def reserve(domain):
            result = wait(domain)
            reservations.append((domain, time.monotonic()))
            return result

        monkeypatch.setattr(fetcher.limiter, "wait", reserve)
        if path == "/redirect":
            with pytest.raises(FetchError):
                fetcher.fetch(origin + path)
        elif path == "/start":
            assert fetcher.fetch(origin + path).text == "done"
        else:
            assert fetcher.fetch(origin + path).final_url == origin + "/final"

    assert not any(url.startswith("/private") for _, url in requests)
    assert len(reservations) == len(requests)
    for domain in {domain for domain, _ in reservations}:
        times = [when for host, when in reservations if host == domain]
        assert all(b - a >= 0.099 for a, b in zip(times, times[1:]))
    if path == "/start":
        assert {url for _, url in requests} >= {
            "/robots.txt", "/start", "/script.js", "/style.css", "/api", "/api?page=2",
        }


@pytest.fixture
def js_server():
    page = b"""<html><body><main id="m"></main><script>
      document.getElementById('m').innerHTML = '<h1>Built by JavaScript</h1>';
    </script></body></html>"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/"
    httpd.shutdown()


def test_playwright_renderer_runs_javascript(js_server):
    pytest.importorskip("playwright.sync_api")
    from dealfinder.fetch import PlaywrightRenderer

    renderer = PlaywrightRenderer(settle_ms=500)
    try:
        page = renderer.render(
            js_server, user_agent="Mozilla/5.0 test", timeout=15,
            before_request=lambda url: None,
        )
    except Exception as exc:  # Chromium not downloaded
        pytest.skip(f"Chromium unavailable: {exc}")
    finally:
        renderer.close()
    assert "# Built by JavaScript" in clean_html(page.html)
