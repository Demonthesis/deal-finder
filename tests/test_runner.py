import json
from datetime import UTC, datetime

import httpx
import pytest
from typer.testing import CliRunner

from dealfinder import db
from dealfinder.adapters import AdapterContext
from dealfinder.cli import app
from dealfinder.config import parse_config
from dealfinder.extract import ExtractionOutput, Extractor, TokenUsage
from dealfinder.fetch import Fetcher
from dealfinder.runner import Pipeline

SHOP_HTML = """<html><body><main><ul>
<li class="card"><a href="/item/1"><h3>Sony WH-1000XM6 Headphones</h3></a><span class="p">$279.00</span></li>
<li class="card"><a href="/item/2"><h3>Case for Sony WH-1000XM6</h3></a><span class="p">$19.00</span></li>
<li class="card"><a href="/item/3"><h3>Patagonia Better Sweater 1/4 Zip M</h3></a><span class="p">$40.00</span></li>
</ul></main></body></html>"""

FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>d</title>
<item><title>Sony WH-1000XM6 $260 shipped</title><link>https://feed.example/d/1</link></item>
</channel></rss>"""

CONFIG = {
    "site_groups": [{
        "name": "retail",
        "interval_minutes": 30,
        "sites": [
            {"name": "shop", "url": "https://shop.example", "adapter": "css",
             "search_url": "https://shop.example/s?q={query}",
             "selectors": {"listing": "li.card", "title": "h3", "price": ".p"},
             "rate_limit": {"requests": 100, "per_seconds": 1}},
            {"name": "feed", "url": "https://feed.example/rss", "adapter": "rss",
             "rate_limit": {"requests": 100, "per_seconds": 1}},
            {"name": "broken", "url": "https://broken.example", "adapter": "css",
             "search_url": "https://broken.example/s?q={query}",
             "selectors": {"listing": "li", "title": "h3", "price": ".p"},
             "rate_limit": {"requests": 100, "per_seconds": 1}},
            {"name": "ebay", "url": "https://www.ebay.com", "adapter": "ebay_api",
             "rate_limit": {"requests": 100, "per_seconds": 1}},
            {"name": "ai", "url": "https://ai.example", "adapter": "llm",
             "search_url": "https://ai.example/s?q={query}",
             "rate_limit": {"requests": 100, "per_seconds": 1}},
        ],
    }],
    "watchlists": [{
        "name": "Buy soon", "site_group": "retail",
        "items": [
            {"name": "Sony WH-1000XM6", "keywords": ["sony", "wh-1000xm6"],
             "exclude": ["case"], "max_price": 330},
            {"name": "Better Sweater", "keywords": ["patagonia", "better sweater"],
             "max_price": 45},
        ],
    }],
    "alerts": {"channel": "console", "timezone": "UTC"},
}


class Recorder:
    channel = "console"

    def __init__(self):
        self.sent = []

    def send(self, title, body):
        self.sent.append(title)


class Server:
    def __init__(self):
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if path == "/robots.txt":
            return httpx.Response(404)
        if host == "shop.example":
            return httpx.Response(200, text=SHOP_HTML, headers={"Content-Type": "text/html"})
        if host == "feed.example":
            return httpx.Response(200, text=FEED, headers={"Content-Type": "application/rss+xml"})
        if host == "ai.example":
            return httpx.Response(200, text=SHOP_HTML, headers={"Content-Type": "text/html"})
        return httpx.Response(500)


class FakeBackend:
    provider, model = "fake", "claude-haiku-5-5"

    def __init__(self):
        self.calls = 0

    def parse(self, system, user, output):
        self.calls += 1
        return output.model_validate({"listings": [{
            "title": "Sony WH-1000XM6 (AI)", "price": 250, "currency": "USD",
            "url": "/ai/1", "condition": "Open box", "in_stock": True}]}), TokenUsage(800, 60)


@pytest.fixture
def server():
    return Server()


def make_pipeline(server, data=CONFIG, budget=0):
    config = parse_config(data)
    conn = db.connect(":memory:")
    db.migrate(conn)
    db.sync_sites(conn, config)
    fetcher = Fetcher.from_config(config, transport=httpx.MockTransport(server),
                                  sleep=lambda s: None, max_retries=0)
    backend = FakeBackend()
    ctx = AdapterContext(fetcher=fetcher, extractor=Extractor(backend, conn,
                                                              daily_token_budget=budget),
                         http=httpx.Client(transport=httpx.MockTransport(server)), env={})
    notifier = Recorder()
    pipeline = Pipeline(config, conn, ctx=ctx, notifier=notifier,
                        clock=lambda: datetime(2026, 10, 1, 12, tzinfo=UTC))
    return pipeline, notifier, backend


def test_run_finds_deals_isolates_errors_and_logs_the_run(server):
    pipeline, notifier, backend = make_pipeline(server)
    report = pipeline.run_group("retail")

    assert sorted(notifier.sent) == sorted([
        "Sony WH-1000XM6: $279.00", "Better Sweater: $40.00",  # shop
        "Sony WH-1000XM6: $260.00",  # feed
        "Sony WH-1000XM6: $250.00",  # ai (LLM)
    ])
    assert report.deals == 4
    assert report.sites_checked == 5
    assert report.sites_failed == 2  # broken (500s) and ebay (no credentials)
    assert any("EBAY_CLIENT_ID" in e for e in report.errors)
    assert any(e.startswith("broken / ") for e in report.errors)
    assert report.usage == TokenUsage(1600, 120)  # one LLM page per item

    row = pipeline.conn.execute("SELECT * FROM runs").fetchone()
    assert row["deals"] == 4 and row["input_tokens"] == 1600 and row["finished_at"]
    assert len(json.loads(row["errors"])) == len(report.errors)
    usage_runs = {r[0] for r in pipeline.conn.execute("SELECT run_id FROM llm_usage")}
    assert usage_runs == {row["id"]}


def test_second_run_sends_nothing_new_and_reuses_llm_cache(server):
    pipeline, notifier, backend = make_pipeline(server)
    pipeline.run_group("retail")
    sent = len(notifier.sent)
    report = pipeline.run_group("retail")
    assert report.deals == 0 and len(notifier.sent) == sent
    assert backend.calls == 2  # same pages -> cached
    assert report.usage == TokenUsage()


def test_feed_without_search_url_is_fetched_once_per_run(server):
    pipeline, _, _ = make_pipeline(server)
    pipeline.run_group("retail")
    feed_fetches = [r for r in server.requests if r.url.host == "feed.example"
                    and r.url.path != "/robots.txt"]
    assert len(feed_fetches) == 1


def test_unavailable_adapter_is_tried_once_per_run(server):
    pipeline, _, _ = make_pipeline(server)
    report = pipeline.run_group("retail")
    assert sum("EBAY_CLIENT_ID" in e for e in report.errors) == 1


def test_llm_sites_are_skipped_when_budget_is_used_up(server):
    pipeline, notifier, backend = make_pipeline(server, budget=100)
    pipeline.conn.execute("INSERT INTO llm_usage (purpose, model, input_tokens, output_tokens)"
                          " VALUES ('extract', 'm', 100, 0)")
    report = pipeline.run_group("retail")
    assert backend.calls == 0
    assert any("budget" in e for e in report.errors)
    assert report.deals == 3  # the other sites still ran


def test_build_without_keys_disables_llm_and_falls_back_to_console(tmp_path, caplog):
    data = json.loads(json.dumps(CONFIG))
    data["alerts"]["channel"] = "discord"
    pipeline = Pipeline.build(parse_config(data), tmp_path / "d.db", env={},
                              cache_dir=tmp_path / "cache")
    try:
        assert pipeline.ctx.extractor is None
        assert pipeline.alerts.notifier.channel == "console"
        assert "ANTHROPIC_API_KEY is not set" in caplog.text
        assert "DISCORD_WEBHOOK_URL" in caplog.text
    finally:
        pipeline.close()


def test_cli_run_once_and_test_site_report_errors(tmp_path, write_config):
    data = json.loads(json.dumps(CONFIG))
    data["site_groups"][0]["sites"] = [data["site_groups"][0]["sites"][3]]  # ebay only
    path = write_config(data)
    runner = CliRunner()

    result = runner.invoke(app, ["run-once", "-c", str(path), "--db", str(tmp_path / "d.db")],
                           env={"EBAY_CLIENT_ID": "", "ANTHROPIC_API_KEY": ""})
    assert result.exit_code == 2  # every site failed
    assert "EBAY_CLIENT_ID" in result.output

    result = runner.invoke(app, ["run-once", "-c", str(path), "-g", "nope"])
    assert result.exit_code == 1 and "Unknown site group" in result.output

    result = runner.invoke(app, ["test-site", "ebay", "sony", "-c", str(path),
                                 "--db", str(tmp_path / "d.db")], env={"EBAY_CLIENT_ID": ""})
    assert result.exit_code == 1 and "EBAY_CLIENT_ID" in result.output


def test_unexpected_matching_error_only_fails_that_site(server, monkeypatch):
    pipeline, notifier, _ = make_pipeline(server)
    real = pipeline.matcher.process

    def process(watchlist, item, listings):
        if listings and listings[0].site == "shop":
            raise RuntimeError("bug")
        return real(watchlist, item, listings)

    monkeypatch.setattr(pipeline.matcher, "process", process)
    report = pipeline.run_group("retail")
    assert any("shop: unexpected RuntimeError" in e for e in report.errors)
    assert report.deals == 2  # feed and ai still ran


def test_old_extraction_cache_is_pruned(server):
    pipeline, _, _ = make_pipeline(server)
    pipeline.conn.execute("INSERT INTO extraction_cache (content_hash, extractor, listings_json,"
                          " created_at) VALUES ('h', 'e', '{}', '2026-01-01T00:00:00Z')")
    pipeline.run_group("retail")
    assert pipeline.conn.execute(
        "SELECT COUNT(*) FROM extraction_cache WHERE content_hash = 'h'").fetchone()[0] == 0
