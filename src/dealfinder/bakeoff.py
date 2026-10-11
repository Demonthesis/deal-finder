"""Compare LLM backends on saved, labelled pages (see docs/research-llm-choice.md).

A fixture is a directory with:
  page.md    the cleaned page text, exactly as the extractor would see it
  meta.json  {"url": ..., "site": ..., "expected": [{"url", "title", "price", "condition",
             "in_stock"}, ...]}
`dealfinder capture-page` writes these with the model-free fields empty; label `expected`
by hand (or from a trusted run, then check it) before benchmarking.

For each backend and page it measures: whether a valid result came back, listing recall and
precision (matched by absolute URL), field accuracy on matched listings, latency, tokens, and
cost. The extraction cache is not used.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from dealfinder.extract import (
    SYSTEM_PROMPT,
    ExtractionError,
    ExtractionOutput,
    LLMBackend,
    TokenUsage,
    _user_message,
    to_listings,
)
from dealfinder.match import normalize
from dealfinder.models import normalize_condition

FIELDS = ("title", "price", "condition", "in_stock")


@dataclass
class Fixture:
    name: str
    url: str
    site: str
    text: str
    expected: list[dict]

    @classmethod
    def load(cls, path: Path) -> Fixture:
        meta = json.loads((path / "meta.json").read_text())
        return cls(path.name, meta["url"], meta.get("site", "site"),
                   (path / "page.md").read_text(), meta.get("expected", []))


def load_fixtures(directory: Path) -> list[Fixture]:
    return [Fixture.load(p) for p in sorted(directory.iterdir()) if (p / "meta.json").exists()]


@dataclass
class ModelScore:
    model: str
    pages: int = 0
    valid: int = 0
    expected: int = 0
    found: int = 0
    matched: int = 0
    field_hits: dict[str, int] = field(default_factory=lambda: dict.fromkeys(FIELDS, 0))
    seconds: float = 0.0
    usage: TokenUsage = field(default_factory=TokenUsage)
    errors: list[str] = field(default_factory=list)

    @property
    def recall(self) -> float:
        return self.matched / self.expected if self.expected else 0.0

    @property
    def precision(self) -> float:
        return self.matched / self.found if self.found else 0.0

    def accuracy(self, name: str) -> float:
        return self.field_hits[name] / self.matched if self.matched else 0.0

    @property
    def cost(self) -> float | None:
        return self.usage.cost(self.model)

    @property
    def cost_per_valid_page(self) -> float | None:
        return self.cost / self.valid if self.cost is not None and self.valid else None


def _same(name: str, got, want) -> bool:
    if name == "title":
        return normalize(str(got)) == normalize(str(want))
    if name == "price":
        if got is None or want is None:
            return got is want
        return abs(float(got) - float(want)) <= max(0.01, 0.01 * float(want))
    if name == "condition":
        want_c = normalize_condition(want) if isinstance(want, str) else want
        return (got.value if got else None) == (want_c.value if want_c else None)
    return bool(got) == bool(want)


def score_page(score: ModelScore, fixture: Fixture, output: ExtractionOutput) -> None:
    got = {l.url: l for l in to_listings(output, fixture.url, fixture.site)}
    score.valid += 1
    score.found += len(got)
    score.expected += len(fixture.expected)
    for want in fixture.expected:
        listing = got.get(want["url"])
        if listing is None:
            continue
        score.matched += 1
        for name in FIELDS:
            if name in want and _same(name, getattr(listing, name), want[name]):
                score.field_hits[name] += 1


def run_bakeoff(backends: list[LLMBackend], fixtures: list[Fixture], *,
                clock: Callable[[], float] = time.monotonic) -> list[ModelScore]:
    scores = []
    for backend in backends:
        score = ModelScore(backend.model)
        for fixture in fixtures:
            score.pages += 1
            start = clock()
            try:
                output, usage = backend.parse(
                    SYSTEM_PROMPT, _user_message(fixture.text, fixture.url), ExtractionOutput
                )
            except ExtractionError as exc:
                score.errors.append(f"{fixture.name}: {exc}")
                continue
            finally:
                score.seconds += clock() - start
            score.usage += usage
            score_page(score, fixture, output)
        scores.append(score)
    return scores


def format_report(scores: list[ModelScore]) -> str:
    def money(v: float | None) -> str:
        return f"${v:.5f}" if v is not None else "?"

    lines = ["| model | valid | recall | precision | title | price | condition | in stock "
             "| avg s | tokens | cost | cost/valid page |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in scores:
        lines.append(
            f"| {s.model} | {s.valid}/{s.pages} | {s.recall:.0%} | {s.precision:.0%} | "
            + " | ".join(f"{s.accuracy(n):.0%}" for n in FIELDS)
            + f" | {s.seconds / max(1, s.pages):.1f} | {s.usage.total:,} | {money(s.cost)} | "
            f"{money(s.cost_per_valid_page)} |"
        )
    for s in scores:
        for error in s.errors:
            lines.append(f"- {s.model}: {error}")
    return "\n".join(lines)
