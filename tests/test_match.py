from datetime import UTC, datetime, timedelta

import pytest

from dealfinder import db
from dealfinder.config import Condition, WatchItem, Watchlist, parse_config
from dealfinder.extract import TokenUsage, tokens_used_today
from dealfinder.match import (
    MatchChecker,
    Matcher,
    Verdict,
    contains_term,
    keyword_verdict,
    score_listing,
)
from dealfinder.models import Listing

HEADPHONES = WatchItem(
    name="Sony WH-1000XM6", keywords=["sony", "wh-1000xm6"], exclude=["case", "ear pads"],
    max_price=330, condition=["new", "open_box", "refurbished", "excellent"],
)
WATCHLIST = Watchlist(name="Buy soon", site_group="general-retail", items=[HEADPHONES])


def make(title="Sony WH-1000XM6 Wireless Headphones", price=300.0, url="https://ebay.example/1",
         site="ebay", **kw) -> Listing:
    return Listing(site=site, url=url, title=title, price=price, **kw)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 1, 12, tzinfo=UTC)

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def conn(example_data):
    conn = db.connect(":memory:")
    db.migrate(conn)
    db.sync_sites(conn, parse_config(example_data))
    yield conn
    conn.close()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def matcher(conn, clock):
    return Matcher(conn, clock=clock)


@pytest.mark.parametrize("title, term, expected", [
    ("Sony WH1000XM6 headphones", "wh-1000xm6", True),
    ("SONY WH-1000XM6", "wh-1000xm6", True),
    ("Patagonia Better Sweater 1/4-Zip", "1/4 zip", True),
    ("Patagonia harvest jacket", "vest", False),
    ("Patagonia Better Sweater Vest", "vest", True),
    ("Sony WH-1000XM5", "wh-1000xm6", False),
])
def test_contains_term(title, term, expected):
    assert contains_term(title, term) is expected


@pytest.mark.parametrize("title, price, verdict", [
    ("Sony WH-1000XM6 Noise Cancelling", 300, Verdict.MATCH),
    ("Hard case for Sony WH-1000XM6", 25, Verdict.NO),  # excluded word
    ("Sony WH-1000XM6 replacement cushions", 30, Verdict.UNCLEAR),  # suspiciously cheap
    ("WH-1000XM6 headphones black", 280, Verdict.UNCLEAR),  # half the keywords
    ("Bose QC Ultra", 280, Verdict.NO),
])
def test_keyword_verdict(title, price, verdict):
    assert keyword_verdict(HEADPHONES, make(title=title, price=price)) is verdict


def test_score_rewards_price_under_max_and_median_and_condition():
    cheap = score_listing(HEADPHONES, make(price=200, condition=Condition.NEW), None)
    pricey = score_listing(HEADPHONES, make(price=320, condition=Condition.NEW), None)
    used = score_listing(HEADPHONES, make(price=200, condition=Condition.FAIR), None)
    with_median = score_listing(HEADPHONES, make(price=200, condition=Condition.NEW), median=300)
    assert cheap > pricey and cheap > used
    assert 0 <= pricey <= 100 and 0 <= with_median <= 100
    assert score_listing(HEADPHONES, make(price=None), None) == 0


def test_new_match_is_a_deal_once(matcher):
    [deal] = matcher.process(WATCHLIST, HEADPHONES, [make(condition=Condition.NEW)])
    assert deal.reason == "new" and deal.listing.price == 300
    assert matcher.process(WATCHLIST, HEADPHONES, [make(condition=Condition.NEW)]) == []


def test_price_drop_flags_again_but_rise_does_not(matcher, conn):
    matcher.process(WATCHLIST, HEADPHONES, [make(price=300)])
    assert matcher.process(WATCHLIST, HEADPHONES, [make(price=310)]) == []
    assert matcher.process(WATCHLIST, HEADPHONES, [make(price=305)]) == []  # still above 300
    [deal] = matcher.process(WATCHLIST, HEADPHONES, [make(price=280)])
    assert deal.reason == "price drop" and deal.previous_price == 300
    history = [r[0] for r in conn.execute("SELECT price FROM price_history ORDER BY id")]
    assert history == [300, 310, 305, 280]


@pytest.mark.parametrize("listing", [
    make(price=340),  # over max_price
    make(condition=Condition.FAIR),  # condition not accepted
    make(in_stock=False),
    make(currency="EUR"),
    make(price=None),
])
def test_ineligible_matches_are_stored_but_not_deals(matcher, conn, listing):
    assert matcher.process(WATCHLIST, HEADPHONES, [listing]) == []
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 1


def test_unknown_condition_is_accepted(matcher):
    assert len(matcher.process(WATCHLIST, HEADPHONES, [make(condition=None)])) == 1


def test_non_matches_are_stored_as_listings_only(matcher, conn):
    assert matcher.process(WATCHLIST, HEADPHONES, [make(title="Bose QC Ultra")]) == []
    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM matches").fetchone()[0] == 0


def test_cross_site_duplicate_is_not_flagged_twice(matcher, conn):
    matcher.process(WATCHLIST, HEADPHONES, [make(title="Sony WH-1000XM6 Black", price=299.5)])
    repost = make(title="sony wh-1000xm6 black", price=299.99, site="bestbuy",
                  url="https://bestbuy.example/9")
    assert matcher.process(WATCHLIST, HEADPHONES, [repost]) == []
    row = conn.execute("SELECT duplicate_of FROM matches WHERE listing_id = 2").fetchone()
    assert row[0] == 1


def test_duplicate_window_expires(matcher, clock):
    matcher.process(WATCHLIST, HEADPHONES, [make()])
    clock.advance(days=15)
    relist = make(url="https://ebay.example/2")
    assert len(matcher.process(WATCHLIST, HEADPHONES, [relist])) == 1


def test_median_uses_latest_price_of_other_listings_in_30_days(matcher, clock):
    for i, price in enumerate([320, 330, 340, 500]):
        matcher.process(WATCHLIST, HEADPHONES, [make(url=f"https://e.example/{i}", price=price,
                                                     title=f"Sony WH-1000XM6 lot {i}")])
    # listing 3 dropped to 325; its latest price is what counts
    matcher.process(WATCHLIST, HEADPHONES, [make(url="https://e.example/3", price=325,
                                                 title="Sony WH-1000XM6 lot 3")])
    [deal] = matcher.process(WATCHLIST, HEADPHONES, [make(url="https://e.example/new", price=250)])
    assert deal.median == 327.5  # median of 320, 330, 340, 325
    assert deal.pct_below_median == pytest.approx(23.7, abs=0.1)

    clock.advance(days=31)
    [later] = matcher.process(WATCHLIST, HEADPHONES, [make(url="https://e.example/x", price=240)])
    assert later.median is None


def test_too_few_prices_means_no_median(matcher):
    [deal] = matcher.process(WATCHLIST, HEADPHONES, [make()])
    assert deal.median is None and deal.pct_below_median is None


class FakeBackend:
    provider, model = "fake", "claude-haiku-5-5"

    def __init__(self, answer: bool):
        self.answer = answer
        self.calls = 0

    def parse(self, system, user, output):
        self.calls += 1
        assert "<listing>" in user and "untrusted" in system
        return output(is_match=self.answer, reason="test"), TokenUsage(300, 20)


def test_unclear_matches_go_to_llm_check_and_are_cached(conn, clock):
    backend = FakeBackend(answer=True)
    matcher = Matcher(conn, checker=MatchChecker(backend, conn), clock=clock)
    unclear = make(title="WH-1000XM6 headphones black", price=280)

    assert len(matcher.process(WATCHLIST, HEADPHONES, [unclear])) == 1
    matcher.process(WATCHLIST, HEADPHONES, [unclear])
    assert backend.calls == 1
    assert tokens_used_today(conn, now=datetime.now(UTC)) == 320


def test_llm_can_reject_unclear_matches(conn, clock):
    matcher = Matcher(conn, checker=MatchChecker(FakeBackend(answer=False), conn), clock=clock)
    accessory = make(title="Sony WH-1000XM6 replacement cushions", price=30)
    assert matcher.process(WATCHLIST, HEADPHONES, [accessory]) == []


def test_unclear_matches_are_dropped_without_llm_or_budget(conn, clock):
    unclear = make(title="WH-1000XM6 headphones black", price=280)
    assert Matcher(conn, clock=clock).process(WATCHLIST, HEADPHONES, [unclear]) == []

    backend = FakeBackend(answer=True)
    checker = MatchChecker(backend, conn, daily_token_budget=1)
    conn.execute("INSERT INTO llm_usage (purpose, model, input_tokens, output_tokens)"
                 " VALUES ('extract', 'm', 5, 5)")
    matcher = Matcher(conn, checker=checker, clock=clock)
    assert matcher.process(WATCHLIST, HEADPHONES, [unclear]) == []
    assert backend.calls == 0 and matcher.llm_checks_skipped == 1
