"""Schema and loader for config/watchlists.yaml."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import (
    AfterValidator,
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


class Site(StrictModel):
    name: Name
    url: HttpUrl
    adapter: AdapterType
    rate_limit: RateLimit
    fetcher: FetcherType = FetcherType.HTTP


class SiteGroup(StrictModel):
    name: Name
    sites: list[Site] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_site_names(self) -> SiteGroup:
        _ensure_unique([s.name for s in self.sites], "site name")
        return self


class WatchItem(StrictModel):
    name: Name | None = None
    keywords: list[Term] = Field(min_length=1)
    exclude: list[Term] = Field(default_factory=list)
    max_price: float = Field(gt=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    condition: list[Condition] = Field(
        default_factory=list, description="Accepted conditions; empty means any."
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


class Watchlist(StrictModel):
    name: Name
    site_group: Name
    items: list[WatchItem] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_item_labels(self) -> Watchlist:
        _ensure_unique([i.label for i in self.items], "item name")
        return self


class Config(StrictModel):
    site_groups: list[SiteGroup] = Field(min_length=1)
    watchlists: list[Watchlist] = Field(default_factory=list)

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
