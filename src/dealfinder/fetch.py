"""Polite page fetcher for the scraping adapters (css, llm).

Every request checks robots.txt, waits for the per-domain rate limit, retries transient
failures with exponential backoff, and revalidates cached pages with ETag/Last-Modified.
Sites whose pages are built with JavaScript can set `fetcher: browser` in the config to be
rendered in headless Chromium (needs the optional `browser` extra and
`playwright install chromium`).

Results carry the raw HTML plus a cleaned markdown version of the main content, which is
what should be sent to an LLM extractor.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import random
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup, Comment
from markdownify import markdownify

from dealfinder.config import Config, FetcherType, RateLimit, Site

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36"
)
# Token matched against `User-agent:` lines in robots.txt (falls back to `*` rules).
ROBOTS_AGENT = "DealFinder"
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
DEFAULT_RATE_LIMIT = RateLimit(requests=1, per_seconds=2)
DEFAULT_CACHE_DIR = Path("data/http_cache")
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
ROBOTS_TTL = 24 * 3600.0
ROBOTS_ERROR_TTL = 3600.0  # retry an unreachable robots.txt sooner


class FetchError(Exception):
    """A page could not be fetched; `status_code` is set when the server answered."""

    def __init__(self, url: str, message: str, status_code: int | None = None):
        self.url = url
        self.status_code = status_code
        super().__init__(f"{url}: {message}")


class RobotsDisallowed(FetchError):
    """robots.txt does not allow this URL for us."""


class BrowserUnavailable(RuntimeError):
    """`fetcher: browser` was configured but Playwright or Chromium is not installed."""


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status_code: int
    html: str
    text: str  # cleaned markdown of the main content
    fetcher: FetcherType
    from_cache: bool = False


def domain_key(url: str) -> str:
    """Rate-limit/site key for a URL: the lowercased host without a leading `www.`."""
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


# --- rate limiting -------------------------------------------------------------------


class RateLimiter:
    """Sliding-window limiter per domain: at most N requests in any `per_seconds` window,
    plus an optional minimum gap between requests (from robots.txt Crawl-delay)."""

    def __init__(
        self,
        default: RateLimit = DEFAULT_RATE_LIMIT,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.default = default
        self._clock = clock
        self._sleep = sleep
        self._limits: dict[str, RateLimit] = {}
        self._min_gap: dict[str, float] = {}
        self._history: dict[str, deque[float]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def set_limit(self, domain: str, limit: RateLimit) -> None:
        self._limits[domain] = limit

    def set_min_gap(self, domain: str, seconds: float) -> None:
        self._min_gap[domain] = seconds

    def limit_for(self, domain: str) -> RateLimit:
        return self._limits.get(domain, self.default)

    def wait(self, domain: str) -> float:
        """Block until a request to `domain` is allowed, record it, return seconds waited."""
        with self._guard:
            lock = self._locks.setdefault(domain, threading.Lock())
            history = self._history.setdefault(domain, deque())
        waited = 0.0
        with lock:
            limit = self.limit_for(domain)
            gap = self._min_gap.get(domain, 0.0)
            while True:
                now = self._clock()
                while history and history[0] <= now - limit.per_seconds:
                    history.popleft()
                delay = 0.0
                if len(history) >= limit.requests:
                    delay = history[0] + limit.per_seconds - now
                if history and gap:
                    delay = max(delay, history[-1] + gap - now)
                if delay <= 0:
                    history.append(now)
                    return waited
                self._sleep(delay)
                waited += delay


# --- response cache ------------------------------------------------------------------


@dataclass
class CachedResponse:
    final_url: str
    status_code: int
    html: str
    etag: str | None = None
    last_modified: str | None = None


class ResponseCache(Protocol):
    def get(self, url: str) -> CachedResponse | None: ...
    def set(self, url: str, entry: CachedResponse) -> None: ...


class MemoryCache:
    def __init__(self) -> None:
        self._entries: dict[str, CachedResponse] = {}

    def get(self, url: str) -> CachedResponse | None:
        return self._entries.get(url)

    def set(self, url: str, entry: CachedResponse) -> None:
        self._entries[url] = entry


class FileCache:
    """One JSON file per URL under `directory`."""

    def __init__(self, directory: Path = DEFAULT_CACHE_DIR):
        self.directory = Path(directory)

    def _path(self, url: str) -> Path:
        return self.directory / f"{hashlib.sha256(url.encode()).hexdigest()}.json"

    def get(self, url: str) -> CachedResponse | None:
        try:
            data = json.loads(self._path(url).read_text(encoding="utf-8"))
            return CachedResponse(**data)
        except (FileNotFoundError, ValueError, TypeError):
            return None

    def set(self, url: str, entry: CachedResponse) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self._path(url)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(entry)), encoding="utf-8")
        tmp.replace(path)


# --- headless browser ----------------------------------------------------------------


@dataclass(frozen=True)
class RenderedPage:
    final_url: str
    status_code: int
    html: str


class Renderer(Protocol):
    def render(
        self, url: str, *, user_agent: str, timeout: float,
        before_request: Callable[[str], None],
    ) -> RenderedPage: ...
    def close(self) -> None: ...


class PlaywrightRenderer:
    """Renders pages in headless Chromium. The browser starts on first use.

    Images, fonts, media and secondary windows are not downloaded: they are not needed
    for the HTML. `before_request` must approve and reserve a slot for each request.
    """

    BLOCKED_RESOURCES = frozenset({"image", "font", "media"})

    def __init__(self, *, settle_ms: int = 3000):
        self.settle_ms = settle_ms
        self._playwright = None
        self._browser = None

    def _ensure_browser(self):
        if self._browser is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                raise BrowserUnavailable(
                    "fetcher: browser needs Playwright: uv sync --extra browser && "
                    "uv run playwright install chromium"
                ) from exc
            self._playwright = sync_playwright().start()
            try:
                self._browser = self._playwright.chromium.launch(headless=True)
            except Exception:
                # A started but unstopped Playwright leaves its event loop on this thread,
                # which breaks every later sync_playwright() call here.
                self._playwright.stop()
                self._playwright = None
                raise
        return self._browser

    def render(
        self, url: str, *, user_agent: str, timeout: float,
        before_request: Callable[[str], None],
    ) -> RenderedPage:
        from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout

        context = self._ensure_browser().new_context(
            user_agent=user_agent, locale="en-US", service_workers="block",
        )
        try:
            def intercept(page):
                # Playwright routes only intercept the first URL in a redirect chain.
                # Chromium's Fetch domain pauses every hop before it is sent.
                session = context.new_cdp_session(page)

                def handle_request(event):
                    request_id = {"requestId": event["requestId"]}
                    if not event.get("redirectedRequestId"):
                        session.send("Fetch.continueRequest", request_id)
                        return
                    try:
                        before_request(event["request"]["url"])
                    except FetchError:
                        session.send("Fetch.failRequest", {
                            **request_id, "errorReason": "BlockedByClient",
                        })
                        return
                    session.send("Fetch.continueRequest", request_id)

                session.on("Fetch.requestPaused", handle_request)
                session.send("Fetch.enable", {
                    "patterns": [{"urlPattern": "*", "requestStage": "Request"}],
                })

            def handle_route(route):
                if route.request.resource_type in self.BLOCKED_RESOURCES:
                    route.abort()
                    return
                # Popup navigations can precede creation of their Page/Frame. Keep
                # rendering confined to the page whose redirects we intercept.
                try:
                    if route.request.frame.page != page:
                        route.abort()
                        return
                except PlaywrightError:
                    route.abort()
                    return
                try:
                    before_request(route.request.url)
                except FetchError:
                    route.abort()
                    return
                route.continue_()

            context.route("**/*", handle_route)
            page = context.new_page()
            intercept(page)
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=self.settle_ms)
            except PlaywrightTimeout:
                pass  # pages with long polling never go idle; use what has rendered
            return RenderedPage(
                final_url=page.url,
                status_code=response.status if response else 200,
                html=page.content(),
            )
        finally:
            context.close()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._browser = self._playwright = None


# --- robots.txt ----------------------------------------------------------------------


def _robots_path(value: str) -> str:
    """Normalize UTF-8 and escapes, keeping encoded reserved characters distinct."""
    value = quote(value, safe="/%!$&'()*+,-.:;=?@_~")

    def normalize_escape(match: re.Match[str]) -> str:
        char = chr(int(match[0][1:], 16))
        if char in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~":
            return char
        return match[0].upper()

    return re.sub(r"%[0-9a-fA-F]{2}", normalize_escape, value)


class _RobotsParser(RobotFileParser):
    """Keep stdlib group/delay parsing, but match patterns by specificity."""

    def parse(self, lines: Iterable[str]) -> None:
        # The stdlib unquotes rules and drops a trailing '?'. Parse groups
        # with placeholders, then restore the original pattern values.
        protected = []
        patterns = {}
        for line in lines:
            key, sep, value = line.partition(":")
            if key.strip().lower() in ("allow", "disallow"):
                placeholder = f"/dealfinder-rule-{len(patterns)}"
                patterns[placeholder] = quote(value.split("#", 1)[0].strip())
                value = placeholder
            protected.append(key + sep + value)
        super().parse(protected)
        entries = self.entries + ([self.default_entry] if self.default_entry else [])
        for entry in entries:
            for rule in entry.rulelines:
                rule.path = patterns[rule.path]

    def can_fetch(self, useragent: str, url: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        if not self.last_checked:
            return False
        entry = next((entry for entry in self.entries if entry.applies_to(useragent)),
                     self.default_entry)
        if entry is None:
            return True
        parts = urlsplit(url)
        path = _robots_path((parts.path or "/") + ("?" + parts.query if parts.query else ""))
        matches = []
        for rule in entry.rulelines:
            pattern = _robots_path(unquote(rule.path))
            if not pattern:
                continue
            anchored = pattern.endswith("$")
            pattern = pattern[:-1] if anchored else pattern
            expression = ".*".join(re.escape(part) for part in pattern.split("*"))
            if re.match(expression + (r"\Z" if anchored else ""), path):
                # Wildcards and the end anchor do not add literal specificity.
                specificity = len(unquote(pattern.replace("*", "")).encode("utf-8"))
                matches.append((specificity, rule.allowance))
        return max(matches)[1] if matches else True


# --- fetcher -------------------------------------------------------------------------


class Fetcher:
    def __init__(
        self,
        sites: Iterable[Site] = (),
        *,
        transport: httpx.BaseTransport | None = None,
        cache: ResponseCache | None = None,
        renderer: Renderer | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
        robots_agent: str = ROBOTS_AGENT,
        default_rate_limit: RateLimit = DEFAULT_RATE_LIMIT,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        max_backoff: float = 120.0,
        timeout: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        self.user_agent = user_agent
        self.robots_agent = robots_agent
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self.timeout = timeout
        self.cache: ResponseCache = cache if cache is not None else MemoryCache()
        self._renderer = renderer
        self._clock = clock
        self._sleep = sleep
        self._jitter = jitter
        self.limiter = RateLimiter(default_rate_limit, clock=clock, sleep=sleep)
        self._sites: dict[str, Site] = {}
        for site in sites:
            key = domain_key(str(site.url))
            self._sites[key] = site
            self.limiter.set_limit(key, site.rate_limit)
        self._robots: dict[str, tuple[RobotFileParser, float]] = {}
        self._client = httpx.Client(
            transport=transport,
            headers={"User-Agent": user_agent, **DEFAULT_HEADERS},
            timeout=timeout,
            follow_redirects=False,
        )

    @classmethod
    def from_config(cls, config: Config, **kwargs) -> Fetcher:
        return cls(config.sites().values(), **kwargs)

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()
        if self._renderer is not None:
            self._renderer.close()

    def site_for(self, url: str) -> Site | None:
        return self._sites.get(domain_key(url))

    # robots.txt

    def allowed(self, url: str) -> bool:
        return self._robots_for(url).can_fetch(self.robots_agent, url)

    def _robots_for(self, url: str) -> RobotFileParser:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = self._robots.get(origin)
        if cached and cached[1] > self._clock():
            return cached[0]
        parser = _RobotsParser()
        ttl = ROBOTS_TTL
        try:
            response = self._send(f"{origin}/robots.txt", {}, check_robots=False)
        except FetchError:
            response = None
        if response is None or response.status_code >= 500:
            parser.disallow_all = True  # unreachable: assume nothing is allowed for now
            ttl = ROBOTS_ERROR_TTL
        elif response.status_code in (401, 403):
            parser.disallow_all = True
        elif response.status_code >= 400:
            parser.allow_all = True  # no robots.txt
        else:
            parser.parse(response.text.splitlines())
            self._apply_crawl_delay(domain_key(url), parser)
        self._robots[origin] = (parser, self._clock() + ttl)
        return parser

    def _apply_crawl_delay(self, domain: str, parser: RobotFileParser) -> None:
        gaps = []
        if (delay := parser.crawl_delay(self.robots_agent)) is not None:
            gaps.append(float(delay))
        if (rate := parser.request_rate(self.robots_agent)) is not None and rate.requests:
            gaps.append(rate.seconds / rate.requests)
        if gaps:
            self.limiter.set_min_gap(domain, max(gaps))

    # fetching

    def fetch(self, url: str, *, fetcher: FetcherType | None = None) -> FetchResult:
        """Fetch a page. `fetcher` overrides the site's configured fetcher."""
        if urlsplit(url).scheme not in ("http", "https"):
            raise FetchError(url, "only http(s) URLs can be fetched")
        if not self.allowed(url):
            raise RobotsDisallowed(url, f"disallowed by robots.txt for {self.robots_agent}")
        site = self.site_for(url)
        mode = fetcher or (site.fetcher if site else FetcherType.HTTP)
        if mode is FetcherType.BROWSER:
            return self._fetch_browser(url)
        return self._fetch_http(url)

    def _fetch_http(self, url: str) -> FetchResult:
        cached = self.cache.get(url)
        headers = {}
        if cached and cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached and cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified
        response = self._send(url, headers)
        if response.status_code == 304 and cached:
            return self._result(url, cached.final_url, cached.status_code, cached.html,
                                FetcherType.HTTP, from_cache=True)
        if not response.is_success:
            raise FetchError(url, f"HTTP {response.status_code}", response.status_code)
        html = response.text
        etag = response.headers.get("ETag")
        last_modified = response.headers.get("Last-Modified")
        if etag or last_modified:
            self.cache.set(url, CachedResponse(str(response.url), response.status_code, html,
                                               etag, last_modified))
        return self._result(url, str(response.url), response.status_code, html,
                            FetcherType.HTTP)

    def _fetch_browser(self, url: str) -> FetchResult:
        if self._renderer is None:
            self._renderer = PlaywrightRenderer()

        def before_request(destination: str) -> None:
            if urlsplit(destination).scheme not in ("http", "https"):
                raise FetchError(destination, "only http(s) URLs can be fetched")
            if not self.allowed(destination):
                raise RobotsDisallowed(
                    destination, f"disallowed by robots.txt for {self.robots_agent}"
                )
            self.limiter.wait(domain_key(destination))

        try:
            page = self._renderer.render(
                url, user_agent=self.user_agent, timeout=self.timeout,
                before_request=before_request,
            )
        except BrowserUnavailable:
            raise
        except Exception as exc:
            raise FetchError(url, f"browser render failed: {exc}") from exc
        if page.status_code >= 400:
            raise FetchError(url, f"HTTP {page.status_code}", page.status_code)
        return self._result(url, page.final_url, page.status_code, page.html,
                            FetcherType.BROWSER)

    def _result(self, url, final_url, status, html, fetcher, from_cache=False) -> FetchResult:
        return FetchResult(url, final_url, status, html, clean_html(html, final_url),
                           fetcher, from_cache)

    def _send(
        self, url: str, headers: dict[str, str], *, check_robots: bool = True
    ) -> httpx.Response:
        """Follow redirects explicitly so every hop obeys robots and rate limits."""
        request = self._client.build_request("GET", url, headers=headers)
        for hop in range(self._client.max_redirects + 1):
            destination = str(request.url)
            if check_robots and not self.allowed(destination):
                raise RobotsDisallowed(
                    destination, f"disallowed by robots.txt for {self.robots_agent}"
                )
            response = self._send_request(request)
            if response.next_request is None:
                return response
            if hop == self._client.max_redirects:
                raise FetchError(url, "too many redirects")
            request = response.next_request
        raise AssertionError("unreachable")

    def _send_request(self, request: httpx.Request) -> httpx.Response:
        """Send one hop with rate limiting and retries, returning its last response."""
        url = str(request.url)
        domain = domain_key(url)
        for attempt in range(self.max_retries + 1):
            self.limiter.wait(domain)
            last_attempt = attempt == self.max_retries
            try:
                response = self._client.send(request, follow_redirects=False)
            except httpx.TransportError as exc:
                if last_attempt:
                    raise FetchError(url, f"{type(exc).__name__}: {exc}") from exc
                self._sleep(self._backoff(attempt))
                continue
            if response.status_code not in RETRY_STATUSES or last_attempt:
                return response
            delay = _retry_after(response)
            delay = self._backoff(attempt) if delay is None else delay
            if delay > self.max_backoff:
                return response  # server wants us gone for longer than we are willing to wait
            self._sleep(delay)
        raise AssertionError("unreachable")

    def _backoff(self, attempt: int) -> float:
        return min(self.max_backoff, self.backoff_base * 2**attempt * (1 + self._jitter()))


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


# --- cleaning ------------------------------------------------------------------------

_DROP_TAGS = [
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "object",
    "embed", "link", "meta", "head", "nav", "footer", "header", "aside", "form",
    "button", "input", "select", "textarea", "dialog",
]
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_MIN_MAIN_CHARS = 200


def clean_html(html: str, base_url: str | None = None) -> str:
    """Reduce a page to compact markdown of its main content.

    Drops scripts, styles, navigation, forms and hidden elements, prefers <main> (or
    role=main / a single <article>), makes links absolute, and replaces images with their
    alt text. Links are kept because listing URLs are what extractors need.
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_DROP_TAGS):
        tag.decompose()
    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    for tag in soup.find_all(True):
        if tag.decomposed:
            continue
        attrs = tag.attrs or {}
        if ("hidden" in attrs or attrs.get("aria-hidden") == "true"
                or _HIDDEN_STYLE.search(attrs.get("style", ""))):
            tag.decompose()

    root = soup.body or soup
    articles = root.find_all("article")
    candidate = (root.find("main") or root.find(attrs={"role": "main"})
                 or (articles[0] if len(articles) == 1 else None))
    if candidate is not None and len(candidate.get_text(strip=True)) >= _MIN_MAIN_CHARS:
        root = candidate

    for img in root.find_all("img"):
        alt = (img.get("alt") or "").strip()
        if alt:
            img.replace_with(f" {alt} ")
        else:
            img.decompose()
    for a in root.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            a.unwrap()
            continue
        a.attrs = {"href": urljoin(base_url, href) if base_url else href}

    text = markdownify(str(root), heading_style="ATX", bullets="-")
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)  # links with no visible text
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
