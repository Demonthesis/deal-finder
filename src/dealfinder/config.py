"""Schema and loader for config/watchlists.yaml."""

from __future__ import annotations

from datetime import time
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    AfterValidator,
    BeforeValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    model_validator,
)

DEFAULT_CONFIG_PATH = Path("config/watchlists.yaml")


def _strip_nonempty(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("must not be blank")
    return value


Name = Annotated[str, AfterValidator(_strip_nonempty)]
Term = Annotated[str, AfterValidator(_strip_nonempty)]


class AdapterType(StrEnum):
    """How listings are fetched from a site."""

    EBAY_API = "ebay_api"  # official eBay Browse API
    API = "api"  # another official/public JSON API
    RSS = "rss"  # RSS / Atom feed
    CSS = "css"  # search-result page parsed with CSS selectors
    LLM = "llm"  # cleaned page handed to an LLM extractor


class FetcherType(StrEnum):
    """How pages are downloaded for scraping adapters (css, llm)."""

    HTTP = "http"  # plain httpx request
    BROWSER = "browser"  # headless Chromium via Playwright, for pages built with JavaScript


class Condition(StrEnum):
    NEW = "new"
    NEW_WITH_TAGS = "new_with_tags"
    OPEN_BOX = "open_box"
    REFURBISHED = "refurbished"
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    USED = "used"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RateLimit(StrictModel):
    """At most `requests` requests every `per_seconds` seconds."""

    requests: int = Field(gt=0)
    per_seconds: float = Field(gt=0)

    @property
    def min_interval(self) -> float:
        return self.per_seconds / self.requests


class CssSelectors(StrictModel):
    """CSS selectors for adapter css. `listing` selects one element per listing; the
    others are relative to it. Each may end in `@attr` to read an attribute instead of text."""

    listing: Term
    title: Term
    url: Term = "a@href"
    price: Term
    condition: Term | None = None


class Site(StrictModel):
    name: Name
    url: HttpUrl
    adapter: AdapterType
    rate_limit: RateLimit
    fetcher: FetcherType = FetcherType.HTTP
    search_url: str | None = Field(
        default=None,
        description="Search page for one item; {query} is replaced by the URL-encoded query. "
        "Without it the site's url is fetched as is (a feed or category page).",
    )
    selectors: CssSelectors | None = None

    @model_validator(mode="after")
    def _check_adapter_settings(self) -> Site:
        if self.search_url is not None:
            if "{query}" not in self.search_url:
                raise ValueError("search_url must contain {query}")
            HttpUrl(self.search_url.replace("{query}", "q"))
        if self.adapter == AdapterType.CSS and self.selectors is None:
            raise ValueError("adapter css needs selectors")
        if self.adapter != AdapterType.CSS and self.selectors is not None:
            raise ValueError("selectors are only used by adapter css")
        return self

    def page_url(self, query: str) -> str:
        """The page to fetch for `query`."""
        if self.search_url is None:
            return str(self.url)
        return self.search_url.replace("{query}", quote_plus(query))


class SiteGroup(StrictModel):
    name: Name
    sites: list[Site] = Field(min_length=1)
    interval_minutes: int = Field(default=60, ge=5, description="How often to check.")

    @model_validator(mode="after")
    def _unique_site_names(self) -> SiteGroup:
        _ensure_unique([s.name for s in self.sites], "site name")
        return self


def _parse_clock(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{value!r} is not a time like 07:30") from None


ClockTime = Annotated[time, BeforeValidator(lambda v: _parse_clock(v) if isinstance(v, str) else v)]


class QuietHours(StrictModel):
    """No instant alerts between start and end (local time); they are sent when it ends."""

    start: ClockTime
    end: ClockTime

    def contains(self, now: time) -> bool:
        if self.start <= self.end:
            return self.start <= now < self.end
        return now >= self.start or now < self.end  # spans midnight


class WatchItem(StrictModel):
    name: Name | None = None
    keywords: list[Term] = Field(min_length=1)
    query: Term | None = Field(
        default=None, description="Search text sent to sites; defaults to the keywords."
    )
    exclude: list[Term] = Field(default_factory=list)
    max_price: float = Field(gt=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    condition: list[Condition] = Field(
        default_factory=list, description="Accepted conditions; empty means any."
    )
    quiet_hours: QuietHours | None = Field(
        default=None, description="Overrides alerts.quiet_hours for this item."
    )
    digest: bool | None = Field(
        default=None, description="Overrides alerts.digest: true sends this item's deals "
        "only in the daily digest."
    )

    @model_validator(mode="after")
    def _exclude_does_not_clash(self) -> WatchItem:
        clash = {k.lower() for k in self.keywords} & {e.lower() for e in self.exclude}
        if clash:
            raise ValueError(
                f"terms appear in both keywords and exclude: {', '.join(sorted(clash))}"
            )
        return self

    @property
    def label(self) -> str:
        return self.name or " ".join(self.keywords)

    @property
    def search_query(self) -> str:
        return self.query or " ".join(self.keywords)


class Watchlist(StrictModel):
    name: Name
    site_group: Name
    items: list[WatchItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_item_labels(self) -> Watchlist:
        _ensure_unique([i.label for i in self.items], "item name")
        return self


class LLMProvider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI_COMPATIBLE = "openai_compatible"  # e.g. Z.ai GLM via its OpenAI-style endpoint


class LLMSettings(StrictModel):
    """The model used for adapter llm. API keys come from environment variables."""

    provider: LLMProvider = LLMProvider.ANTHROPIC
    model: Name = "claude-haiku-5-5"
    base_url: HttpUrl | None = Field(
        default=None, description="Endpoint for openai_compatible, e.g. https://api.z.ai/api/paas/v4"
    )
    api_key_env: Name | None = Field(
        default=None,
        description="Env var holding the key; defaults to ANTHROPIC_API_KEY or LLM_API_KEY.",
    )
    daily_token_budget: int = Field(
        default=2_000_000, ge=0, description="Input + output tokens per day; 0 means no limit."
    )

    @model_validator(mode="after")
    def _base_url_for_openai(self) -> LLMSettings:
        if self.provider == LLMProvider.OPENAI_COMPATIBLE and self.base_url is None:
            raise ValueError("provider openai_compatible needs base_url")
        return self

    @property
    def key_env(self) -> str:
        if self.api_key_env:
            return self.api_key_env
        return "ANTHROPIC_API_KEY" if self.provider == LLMProvider.ANTHROPIC else "LLM_API_KEY"


class AlertChannel(StrEnum):
    CONSOLE = "console"  # print to the log; for testing
    DISCORD = "discord"  # DISCORD_WEBHOOK_URL
    NTFY = "ntfy"  # NTFY_TOPIC_URL (optional NTFY_TOKEN)
    TELEGRAM = "telegram"  # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    EMAIL = "email"  # SMTP_HOST, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO (opt. SMTP_PORT)


class AlertSettings(StrictModel):
    channel: AlertChannel = AlertChannel.CONSOLE
    timezone: str = "UTC"
    quiet_hours: QuietHours | None = None
    digest: bool = Field(
        default=False, description="Send deals once a day in one message instead of instantly."
    )
    digest_time: ClockTime = time(8, 0)

    @model_validator(mode="after")
    def _known_timezone(self) -> AlertSettings:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {self.timezone!r}") from None
        return self


class Config(StrictModel):
    site_groups: list[SiteGroup] = Field(min_length=1)
    watchlists: list[Watchlist] = Field(default_factory=list)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    alerts: AlertSettings = Field(default_factory=AlertSettings)

    @model_validator(mode="after")
    def _check_references(self) -> Config:
        _ensure_unique([g.name for g in self.site_groups], "site group name")
        _ensure_unique([w.name for w in self.watchlists], "watchlist name")
        by_name: dict[str, Site] = {}
        for group in self.site_groups:
            for site in group.sites:
                first = by_name.setdefault(site.name, site)
                if first != site:
                    raise ValueError(
                        f"site {site.name!r} is defined differently in group {group.name!r}; "
                        "a site shared between groups must have the same url, adapter, "
                        "rate_limit and fetcher everywhere"
                    )
        known = {g.name for g in self.site_groups}
        for wl in self.watchlists:
            if wl.site_group not in known:
                raise ValueError(
                    f"watchlist {wl.name!r} uses unknown site_group {wl.site_group!r} "
                    f"(defined: {', '.join(sorted(known))})"
                )
        return self

    def site_group(self, name: str) -> SiteGroup:
        return next(g for g in self.site_groups if g.name == name)

    def sites(self) -> dict[str, Site]:
        """Every distinct site, keyed by name."""
        return {s.name: s for g in self.site_groups for s in g.sites}


def _ensure_unique(names: list[str], what: str) -> None:
    seen: set[str] = set()
    dupes = sorted({n for n in names if n in seen or seen.add(n)})
    if dupes:
        raise ValueError(f"duplicate {what}: {', '.join(dupes)}")


class ConfigError(Exception):
    """Config could not be loaded; `errors` holds one readable line per problem."""

    def __init__(self, path: Path, errors: list[str]):
        self.path = path
        self.errors = errors
        super().__init__(f"{path}: " + "; ".join(errors))


def _format_loc(loc: tuple[int | str, ...]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out or "(root)"


def format_validation_error(exc: ValidationError) -> list[str]:
    lines = []
    for err in exc.errors():
        msg = err["msg"].removeprefix("Value error, ")
        lines.append(f"{_format_loc(err['loc'])}: {msg}")
    return lines


def parse_config(data: object, path: Path = Path("<memory>")) -> Config:
    if data is None:
        raise ConfigError(path, ["file is empty"])
    if not isinstance(data, dict):
        raise ConfigError(path, ["top level must be a mapping"])
    try:
        return Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(path, format_validation_error(exc)) from exc


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(path, ["file not found"]) from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(path, [f"invalid YAML: {exc}"]) from exc
    return parse_config(data, path)
