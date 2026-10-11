"""Listings as every adapter returns them."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from dealfinder.config import Condition

# Words sites use for each condition, checked in order (most specific first).
_CONDITION_WORDS: list[tuple[Condition, re.Pattern[str]]] = [
    (Condition.NEW_WITH_TAGS, re.compile(r"\b(nwt|new with tags?|brand new with tags?)\b")),
    (Condition.OPEN_BOX, re.compile(r"\bopen[- ]box\b")),
    (Condition.REFURBISHED, re.compile(r"\b(refurb(ished)?|renewed|reconditioned)\b")),
    (Condition.EXCELLENT, re.compile(r"\b(excellent|like new|mint|nwot|new without tags?)\b")),
    (Condition.GOOD, re.compile(r"\b(good|very good|gently used)\b")),
    (Condition.FAIR, re.compile(r"\b(fair|acceptable|worn|for parts)\b")),
    (Condition.NEW, re.compile(r"\b(new|brand new|sealed|bnib)\b")),
    (Condition.USED, re.compile(r"\b(used|pre-?owned|second ?hand)\b")),
]


def normalize_condition(text: str | None) -> Condition | None:
    """Map a site's condition wording ("Pre-owned", "NWT", "Like New") to a Condition."""
    if not text:
        return None
    text = text.strip().lower().replace("_", " ")
    try:
        return Condition(text.replace(" ", "_"))
    except ValueError:
        pass
    for condition, pattern in _CONDITION_WORDS:
        if pattern.search(text):
            return condition
    return None


_NUMBER = r"(\d{1,3}(?:,\d{3})+|\d+)(?:[.,](\d{1,2}))?(?!\d)"
_PRICE_RE = re.compile(_NUMBER)
_CURRENCY_PRICE_RE = re.compile(
    rf"(?:[$£€¥]\s?{_NUMBER})|(?:\b{_NUMBER}\s?(?:USD|EUR|GBP|CAD|AUD)\b)"
)


def parse_price(text: str | None, *, require_currency: bool = False) -> float | None:
    """First price in a string: "$1,299.99" -> 1299.99, "From 45 USD" -> 45.0.

    With `require_currency`, only a number next to a currency sign or code counts, so a
    title like "Sony WH-1000XM6 $298" gives 298, not 1000.
    """
    if not text:
        return None
    match = _CURRENCY_PRICE_RE.search(text)
    if match:
        groups = [g for g in match.groups()]
        whole, cents = (groups[0], groups[1]) if groups[0] else (groups[2], groups[3])
    elif require_currency:
        return None
    else:
        match = _PRICE_RE.search(text)
        if not match:
            return None
        whole, cents = match.groups()
    return float(f"{whole.replace(',', '')}.{cents or 0}")


class Listing(BaseModel):
    """One item for sale on a site."""

    site: str
    url: str
    title: str = Field(min_length=1)
    price: float | None = Field(default=None, ge=0)
    currency: str = "USD"
    condition: Condition | None = None
    in_stock: bool = True

    @field_validator("title")
    @classmethod
    def _collapse_whitespace(cls, value: str) -> str:
        return " ".join(value.split())

    @field_validator("currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.strip().upper() or "USD"
