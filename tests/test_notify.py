import io
import json
import smtplib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from dealfinder import db
from dealfinder.config import AlertChannel, AlertSettings, Condition, parse_config
from dealfinder.match import Matcher
from dealfinder.models import Listing
from dealfinder.notify import (
    AlertQueue,
    ConsoleNotifier,
    DiscordNotifier,
    EmailNotifier,
    NotifyConfigError,
    NotifyError,
    NtfyNotifier,
    TelegramNotifier,
    format_deal,
    notifier_from_settings,
)


class Clock:
    def __init__(self, when: datetime):
        self.now = when

    def __call__(self):
        return self.now


class Recorder:
    channel = "console"

    def __init__(self):
        self.sent = []
        self.fail = False

    def send(self, title, body):
        if self.fail:
            raise NotifyError("down")
        self.sent.append((title, body))


@pytest.fixture
def config(example_data):
    example_data["alerts"] = {
        "channel": "console", "timezone": "America/New_York",
        "quiet_hours": {"start": "22:00", "end": "07:00"}, "digest_time": "08:00",
    }
    # Red Wing Iron Ranger is digest-only in the example config
    return parse_config(example_data)


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 1, 16, 0, tzinfo=UTC))  # 12:00 in New York


@pytest.fixture
def setup(config, clock):
    conn = db.connect(":memory:")
    db.migrate(conn)
    db.sync_sites(conn, config)
    matcher = Matcher(conn, clock=clock)
    notifier = Recorder()
    queue = AlertQueue(conn, config, notifier, clock=clock)
    yield conn, matcher, queue, notifier, config
    conn.close()


def deal_for(matcher, config, item_index=0, wl_index=1, **listing):
    watchlist = config.watchlists[wl_index]
    item = watchlist.items[item_index]
    fields = {"site": "ebay", "url": "https://www.ebay.com/itm/1",
              "title": "Sony WH-1000XM6 headphones", "price": 280.0,
              "condition": Condition.OPEN_BOX} | listing
    [deal] = matcher.process(watchlist, item, [Listing(**fields)])
    return deal


def test_format_deal_has_everything_the_owner_needs(setup):
    _, matcher, _, _, config = setup
    title, body = format_deal(deal_for(matcher, config))
    assert title == "Sony WH-1000XM6: $280.00"
    assert "target $330.00" in body and "ebay" in body and "open box" in body
    assert body.splitlines()[-1] == "https://www.ebay.com/itm/1"


def test_instant_alert_is_sent_once(setup):
    _, matcher, queue, notifier, config = setup
    deal = deal_for(matcher, config)
    assert queue.queue(deal)
    assert not queue.queue(deal)  # same listing, item, price
    assert queue.flush().sent == 1
    assert queue.flush().sent == 0
    assert len(notifier.sent) == 1


def test_price_drop_sends_a_new_alert(setup):
    _, matcher, queue, notifier, config = setup
    queue.queue(deal_for(matcher, config, price=300))
    queue.flush()
    queue.queue(deal_for(matcher, config, price=250))
    queue.flush()
    assert [t for t, _ in notifier.sent] == ["Sony WH-1000XM6: $300.00", "Sony WH-1000XM6: $250.00"]
    assert "down from $300.00" in notifier.sent[1][1]


def test_quiet_hours_hold_alerts_until_they_end(setup, clock):
    _, matcher, queue, notifier, config = setup
    clock.now = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)  # 23:00 in New York
    queue.queue(deal_for(matcher, config, price=300))
    queue.queue(deal_for(matcher, config, price=290))  # replaces the waiting alert
    assert queue.flush().held == 1
    clock.now += timedelta(hours=8)  # 07:00
    assert queue.flush().sent == 1
    assert notifier.sent[0][0] == "Sony WH-1000XM6: $290.00"


def test_per_item_quiet_hours_override(example_data, clock):
    example_data["alerts"] = {"channel": "console", "timezone": "UTC"}
    example_data["watchlists"][1]["items"][0]["quiet_hours"] = {"start": "15:00", "end": "17:00"}
    config = parse_config(example_data)
    conn = db.connect(":memory:")
    db.migrate(conn)
    db.sync_sites(conn, config)
    queue = AlertQueue(conn, config, Recorder(), clock=clock)  # 16:00 UTC
    queue.queue(deal_for(Matcher(conn, clock=clock), config))
    assert queue.flush().held == 1


def test_digest_items_are_sent_together_once_a_day(setup, clock):
    _, matcher, queue, notifier, config = setup
    clock.now = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)  # 07:00 local, before digest_time
    boots = config.watchlists[0].items[1]
    for i, price in enumerate([150, 140]):
        [deal] = matcher.process(config.watchlists[0], boots, [Listing(
            site="ebay", url=f"https://www.ebay.com/itm/b{i}", title=f"Red Wing Iron Ranger {i}",
            price=price)])
        queue.queue(deal)
    assert queue.flush().held == 2 and notifier.sent == []

    clock.now += timedelta(hours=1, minutes=5)  # 08:05 local
    assert queue.flush().sent == 2
    [(title, body)] = notifier.sent
    assert title == "Deal digest: 2 deals"
    assert "Red Wing Iron Ranger 0" in body and "Red Wing Iron Ranger 1" in body

    [deal] = matcher.process(config.watchlists[0], boots, [Listing(
        site="ebay", url="https://www.ebay.com/itm/b9", title="Red Wing Iron Ranger 9", price=99)])
    queue.queue(deal)
    clock.now += timedelta(hours=2)
    assert queue.flush().held == 1  # already sent today's digest
    clock.now += timedelta(days=1)
    assert queue.flush().sent == 1


def test_failed_sends_are_retried_then_given_up(setup):
    conn, matcher, queue, notifier, config = setup
    queue.queue(deal_for(matcher, config))
    notifier.fail = True
    for _ in range(4):
        assert queue.flush().failed == 1
    assert queue.flush().failed == 1
    row = conn.execute("SELECT status, attempts, error FROM alerts").fetchone()
    assert (row["status"], row["attempts"], row["error"]) == ("failed", 5, "down")
    notifier.fail = False
    assert queue.flush().sent == 0


def mock_http(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_discord_notifier_truncates_long_messages():
    seen = []
    DiscordNotifier("https://discord.example/hook", mock_http(
        lambda r: seen.append(json.loads(r.content)) or httpx.Response(204)
    )).send("t", "x" * 5000)
    assert len(seen[0]["content"]) <= 2000


def test_ntfy_notifier_sets_title_and_click():
    seen = []
    NtfyNotifier("https://ntfy.example/deals", mock_http(
        lambda r: seen.append(r) or httpx.Response(200)), token="tk",
    ).send("Sony: $280.00 €", "line\nhttps://www.ebay.com/itm/1")
    assert seen[0].headers["Click"] == "https://www.ebay.com/itm/1"
    assert seen[0].headers["Authorization"] == "Bearer tk"
    assert seen[0].headers["Title"].startswith("Sony: $280.00")


def test_telegram_notifier_and_errors():
    seen = []
    TelegramNotifier("123:abc", "42", mock_http(
        lambda r: seen.append(r) or httpx.Response(200, json={"ok": True}))).send("t", "b")
    assert seen[0].url.path == "/bot123:abc/sendMessage"
    assert json.loads(seen[0].content)["chat_id"] == "42"
    with pytest.raises(NotifyError):
        TelegramNotifier("x", "1", mock_http(lambda r: httpx.Response(400))).send("t", "b")


def test_email_notifier_uses_starttls_and_login():
    calls = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            calls.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def starttls(self):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user))

        def send_message(self, message):
            calls.append(("send", message["To"], message["Subject"]))

    EmailNotifier("smtp.example", 587, "me@example.com", "pw", "you@example.com",
                  smtp_factory=FakeSMTP).send("Deal", "body")
    assert calls == [("connect", "smtp.example", 587), ("starttls",),
                     ("login", "me@example.com"), ("send", "you@example.com", "Deal")]

    class Broken(FakeSMTP):
        def login(self, user, password):
            raise smtplib.SMTPAuthenticationError(535, b"no")

    with pytest.raises(NotifyError):
        EmailNotifier("h", 587, "u", "p", "t", smtp_factory=Broken).send("s", "b")


def test_console_notifier_prints():
    out = io.StringIO()
    ConsoleNotifier(out).send("Title", "Body")
    assert "Title" in out.getvalue() and "Body" in out.getvalue()


@pytest.mark.parametrize("channel, env, cls", [
    ("console", {}, ConsoleNotifier),
    ("discord", {"DISCORD_WEBHOOK_URL": "https://d.example/h"}, DiscordNotifier),
    ("ntfy", {"NTFY_TOPIC_URL": "https://ntfy.sh/x"}, NtfyNotifier),
    ("telegram", {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}, TelegramNotifier),
    ("email", {"SMTP_HOST": "h", "SMTP_USER": "u", "SMTP_PASSWORD": "p",
               "ALERT_EMAIL_TO": "t"}, EmailNotifier),
])
def test_notifier_from_settings(channel, env, cls):
    assert isinstance(notifier_from_settings(AlertSettings(channel=channel), env), cls)


def test_notifier_from_settings_reports_missing_env():
    with pytest.raises(NotifyConfigError, match="TELEGRAM_CHAT_ID"):
        notifier_from_settings(AlertSettings(channel=AlertChannel.TELEGRAM),
                               {"TELEGRAM_BOT_TOKEN": "t"})


def test_bad_timezone_and_quiet_hours_are_config_errors(example_data):
    from dealfinder.config import ConfigError

    example_data["alerts"] = {"timezone": "Mars/Olympus"}
    with pytest.raises(ConfigError, match="timezone"):
        parse_config(example_data)
    example_data["alerts"] = {"quiet_hours": {"start": "25:00", "end": "07:00"}}
    with pytest.raises(ConfigError):
        parse_config(example_data)


def test_telegram_errors_do_not_leak_the_token():
    def boom(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with pytest.raises(NotifyError) as exc_info:
        TelegramNotifier("123:secret", "1", mock_http(boom)).send("t", "b")
    assert "secret" not in str(exc_info.value) and "<token>" in str(exc_info.value)
