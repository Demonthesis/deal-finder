"""Deal alerts.

New deals are queued in the `alerts` table (one row per listing, item, channel and price, so
nothing is sent twice) and `AlertQueue.flush` sends what is due:
- instant alerts, unless the item is in quiet hours (they wait until the quiet hours end);
- digest alerts (alerts.digest, or `digest: true` on an item), once a day at digest_time,
  combined into one message.
A failed send is retried on the next flush, up to MAX_ATTEMPTS times.

Channels and the env vars they read:
  console   -
  discord   DISCORD_WEBHOOK_URL
  ntfy      NTFY_TOPIC_URL, optional NTFY_TOKEN
  telegram  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  email     SMTP_HOST, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_TO, optional SMTP_PORT (587),
            SMTP_FROM
"""

from __future__ import annotations

import smtplib
import sqlite3
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Protocol, TextIO
from zoneinfo import ZoneInfo

import httpx

from dealfinder import db
from dealfinder.config import AlertChannel, AlertSettings, Config, QuietHours, WatchItem
from dealfinder.match import Deal

MAX_ATTEMPTS = 5


class NotifyError(Exception):
    """An alert could not be delivered (will be retried)."""


class NotifyConfigError(Exception):
    """The alert channel is missing settings."""


class Notifier(Protocol):
    channel: str

    def send(self, title: str, body: str) -> None: ...


class ConsoleNotifier:
    channel = AlertChannel.CONSOLE.value

    def __init__(self, stream: TextIO | None = None):
        self.stream = stream

    def send(self, title: str, body: str) -> None:
        stream = self.stream or sys.stdout
        print(f"=== {title} ===\n{body}\n", file=stream, flush=True)


def _post(http: httpx.Client, url: str, what: str, **kwargs) -> httpx.Response:
    try:
        response = http.post(url, **kwargs)
    except httpx.HTTPError as exc:
        raise NotifyError(f"{what}: {exc}") from exc
    if response.status_code >= 400:
        raise NotifyError(f"{what} answered {response.status_code}: {response.text[:200]}")
    return response


class DiscordNotifier:
    channel = AlertChannel.DISCORD.value
    LIMIT = 2000

    def __init__(self, webhook_url: str, http: httpx.Client):
        self.webhook_url = webhook_url
        self.http = http

    def send(self, title: str, body: str) -> None:
        content = f"**{title}**\n{body}"
        if len(content) > self.LIMIT:
            content = content[: self.LIMIT - 20] + "\n… (more in the app)"
        _post(self.http, self.webhook_url, "Discord", json={"content": content})


class NtfyNotifier:
    channel = AlertChannel.NTFY.value

    def __init__(self, topic_url: str, http: httpx.Client, token: str | None = None):
        self.topic_url = topic_url
        self.http = http
        self.token = token

    def send(self, title: str, body: str) -> None:
        headers = {"Title": title.encode("ascii", "replace").decode(), "Tags": "moneybag"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        lines = body.splitlines()
        if lines and lines[-1].startswith("http"):
            headers["Click"] = lines[-1]
        _post(self.http, self.topic_url, "ntfy", content=body.encode(), headers=headers)


class TelegramNotifier:
    channel = AlertChannel.TELEGRAM.value
    LIMIT = 4096

    def __init__(self, bot_token: str, chat_id: str, http: httpx.Client):
        self.url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self.chat_id = chat_id
        self.http = http

    def send(self, title: str, body: str) -> None:
        text = f"{title}\n\n{body}"[: self.LIMIT]
        _post(self.http, self.url, "Telegram", json={"chat_id": self.chat_id, "text": text})


class EmailNotifier:
    channel = AlertChannel.EMAIL.value

    def __init__(self, host: str, port: int, user: str, password: str, to: str,
                 sender: str | None = None, smtp_factory: Callable | None = None):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.to = to
        self.sender = sender or user
        self.smtp_factory = smtp_factory or (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)

    def send(self, title: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = title
        message["From"] = self.sender
        message["To"] = self.to
        message.set_content(body)
        try:
            with self.smtp_factory(self.host, self.port, timeout=30) as smtp:
                if self.port != 465:
                    smtp.starttls()
                smtp.login(self.user, self.password)
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifyError(f"email: {exc}") from exc


def notifier_from_settings(settings: AlertSettings, env: Mapping[str, str],
                           http: httpx.Client | None = None) -> Notifier:
    def need(*names: str) -> list[str]:
        missing = [n for n in names if not env.get(n)]
        if missing:
            raise NotifyConfigError(
                f"alerts channel {settings.channel.value} needs {', '.join(missing)}"
            )
        return [env[n] for n in names]

    http = http or httpx.Client(timeout=20.0)
    match settings.channel:
        case AlertChannel.CONSOLE:
            return ConsoleNotifier()
        case AlertChannel.DISCORD:
            return DiscordNotifier(*need("DISCORD_WEBHOOK_URL"), http)
        case AlertChannel.NTFY:
            return NtfyNotifier(*need("NTFY_TOPIC_URL"), http, token=env.get("NTFY_TOKEN"))
        case AlertChannel.TELEGRAM:
            return TelegramNotifier(*need("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"), http)
        case AlertChannel.EMAIL:
            host, user, password, to = need("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD",
                                            "ALERT_EMAIL_TO")
            return EmailNotifier(host, int(env.get("SMTP_PORT", "587")), user, password, to,
                                 sender=env.get("SMTP_FROM"))
    raise NotifyConfigError(f"unknown channel {settings.channel}")


# --- message text ----------------------------------------------------------------------


def money(amount: float, currency: str) -> str:
    symbol = {"USD": "$", "CAD": "CA$", "AUD": "A$", "EUR": "€", "GBP": "£"}.get(currency)
    return f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency}"


def format_deal(deal: Deal) -> tuple[str, str]:
    """(title, body) for one deal. The body's last line is the link."""
    listing, item = deal.listing, deal.item
    price = money(listing.price or 0, listing.currency)
    title = f"{item.label}: {price}"
    facts = [price, f"target {money(item.max_price, item.currency)}"]
    if deal.previous_price is not None:
        facts.append(f"down from {money(deal.previous_price, listing.currency)}")
    if deal.pct_below_median is not None:
        facts.append(f"{deal.pct_below_median:g}% below 30-day median "
                     f"({money(deal.median, listing.currency)})")
    condition = listing.condition.value.replace("_", " ") if listing.condition else "condition ?"
    body = "\n".join([
        listing.title,
        " · ".join(facts),
        f"{listing.site} · {condition} · score {deal.score:g}",
        listing.url,
    ])
    return title, body


# --- queue -------------------------------------------------------------------------------


@dataclass
class FlushResult:
    sent: int = 0
    failed: int = 0
    held: int = 0


def _ts(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class AlertQueue:
    def __init__(self, conn: sqlite3.Connection, config: Config, notifier: Notifier, *,
                 clock: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.conn = conn
        self.settings = config.alerts
        self.notifier = notifier
        self.clock = clock
        self.tz = ZoneInfo(self.settings.timezone)
        self.items: dict[tuple[str, str], WatchItem] = {
            (wl.name, item.label): item for wl in config.watchlists for item in wl.items
        }

    def _item(self, watchlist: str, item: str) -> WatchItem | None:
        return self.items.get((watchlist, item))

    def _digest_for(self, item: WatchItem | None) -> bool:
        if item is not None and item.digest is not None:
            return item.digest
        return self.settings.digest

    def _quiet_hours_for(self, item: WatchItem | None) -> QuietHours | None:
        if item is not None and item.quiet_hours is not None:
            return item.quiet_hours
        return self.settings.quiet_hours

    def queue(self, deal: Deal) -> bool:
        """Queue a deal. Returns False if this exact alert was already queued or sent."""
        title, body = format_deal(deal)
        with db.transaction(self.conn):
            # a newer price replaces an alert for the same listing that is still waiting
            self.conn.execute(
                "DELETE FROM alerts WHERE listing_id = ? AND watchlist = ? AND item = ?"
                " AND channel = ? AND status = 'pending' AND price != ?",
                (deal.listing_id, deal.watchlist, deal.item.label, self.notifier.channel,
                 deal.listing.price),
            )
            cursor = self.conn.execute(
                "INSERT OR IGNORE INTO alerts (listing_id, watchlist, item, price, channel,"
                " digest, title, body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (deal.listing_id, deal.watchlist, deal.item.label, deal.listing.price,
                 self.notifier.channel, int(self._digest_for(deal.item)), title, body,
                 _ts(self.clock())),
            )
        return cursor.rowcount == 1

    def _mark(self, ids: list[int], error: str | None) -> None:
        marks = ",".join("?" * len(ids))
        if error is None:
            self.conn.execute(
                f"UPDATE alerts SET status = 'sent', sent_at = ?, error = NULL"
                f" WHERE id IN ({marks})",
                (_ts(self.clock()), *ids),
            )
        else:
            self.conn.execute(
                f"UPDATE alerts SET attempts = attempts + 1, error = ?,"
                f" status = CASE WHEN attempts + 1 >= {MAX_ATTEMPTS} THEN 'failed'"
                f" ELSE 'pending' END WHERE id IN ({marks})",
                (error, *ids),
            )

    def _send(self, ids: list[int], title: str, body: str, result: FlushResult) -> None:
        try:
            self.notifier.send(title, body)
        except NotifyError as exc:
            self._mark(ids, str(exc))
            result.failed += len(ids)
        else:
            self._mark(ids, None)
            result.sent += len(ids)

    def flush(self) -> FlushResult:
        result = FlushResult()
        local = self.clock().astimezone(self.tz)
        rows = self.conn.execute(
            "SELECT * FROM alerts WHERE status = 'pending' AND channel = ? ORDER BY id",
            (self.notifier.channel,),
        ).fetchall()

        digest_rows = []
        for row in rows:
            if row["digest"]:
                digest_rows.append(row)
                continue
            quiet = self._quiet_hours_for(self._item(row["watchlist"], row["item"]))
            if quiet is not None and quiet.contains(local.time().replace(tzinfo=None)):
                result.held += 1
                continue
            self._send([row["id"]], row["title"], row["body"], result)

        today = local.date().isoformat()
        last = self.conn.execute("SELECT value FROM meta WHERE key = 'last_digest'").fetchone()
        due = local.time().replace(tzinfo=None) >= self.settings.digest_time and (
            last is None or last[0] != today
        )
        if digest_rows and due:
            body = "\n\n".join(f"{r['title']}\n{r['body']}" for r in digest_rows)
            title = f"Deal digest: {len(digest_rows)} deal{'s' if len(digest_rows) != 1 else ''}"
            before = result.sent
            self._send([r["id"] for r in digest_rows], title, body, result)
            if result.sent > before:
                self.conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_digest', ?)", (today,)
                )
        elif digest_rows:
            result.held += len(digest_rows)
        return result
