"""The pipeline: fetch -> adapter -> match -> score -> alert, one site group at a time.

Errors are isolated per site: a site that fails is logged in the run and the others carry
on. A site whose adapter cannot run at all (missing API key, no LLM) is skipped for the
rest of the run. When the daily LLM token budget is used up, llm sites are skipped until
the next day; cached pages are still matched.

`serve` runs each site group every `interval_minutes` (with a little random jitter so
sites aren't all hit at once) and flushes waiting alerts every few minutes. Jobs run one
at a time on a single worker thread, which owns the database connection and the browser.
"""

from __future__ import annotations

import json
import logging
import os
import random
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dealfinder import adapters, db
from dealfinder.adapters import AdapterContext, AdapterError, AdapterUnavailable, SearchResult
from dealfinder.config import AdapterType, Config, FetcherType, Site
from dealfinder.extract import (
    BudgetExhausted,
    ExtractionError,
    Extractor,
    LLMConfigError,
    TokenUsage,
    backend_from_settings,
)
from dealfinder.fetch import DEFAULT_CACHE_DIR, FetchError, Fetcher, FileCache
from dealfinder.match import Matcher, MatchChecker
from dealfinder.notify import (
    AlertQueue,
    ConsoleNotifier,
    FlushResult,
    Notifier,
    NotifyConfigError,
    notifier_from_settings,
)

log = logging.getLogger("dealfinder")

FLUSH_MINUTES = 5


@dataclass
class RunReport:
    site_group: str
    run_id: int = 0
    sites_checked: int = 0
    sites_failed: int = 0
    listings: int = 0
    deals: int = 0
    usage: TokenUsage = field(default_factory=TokenUsage)
    errors: list[str] = field(default_factory=list)
    alerts: FlushResult = field(default_factory=FlushResult)

    def summary(self) -> str:
        return (
            f"{self.site_group}: {self.sites_checked} site(s) checked, {self.sites_failed} failed, "
            f"{self.listings} listing(s), {self.deals} new deal(s), "
            f"{self.usage.total:,} LLM tokens, {self.alerts.sent} alert(s) sent"
        )


class Pipeline:
    def __init__(
        self, config: Config, conn: sqlite3.Connection, *, ctx: AdapterContext,
        notifier: Notifier, checker: MatchChecker | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self.config = config
        self.conn = conn
        self.ctx = ctx
        self.checker = checker
        self.clock = clock
        self.matcher = Matcher(conn, checker=checker, clock=clock)
        self.alerts = AlertQueue(conn, config, notifier, clock=clock)

    @classmethod
    def build(cls, config: Config, db_path: Path | str = db.DEFAULT_DB_PATH, *,
              env: Mapping[str, str] | None = None, cache_dir: Path = DEFAULT_CACHE_DIR) -> Pipeline:
        """Wire up the real database, fetcher, LLM and alert channel."""
        env = dict(os.environ if env is None else env)
        conn = db.connect(db_path, check_same_thread=False)
        db.migrate(conn)
        db.sync_sites(conn, config)

        renderer = None
        if any(s.fetcher == FetcherType.BROWSER for s in config.sites().values()):
            from dealfinder.fetch import PlaywrightRenderer

            renderer = PlaywrightRenderer()
        fetcher = Fetcher.from_config(config, cache=FileCache(cache_dir), renderer=renderer)

        extractor = checker = None
        try:
            backend = backend_from_settings(config.llm, env)
        except LLMConfigError as exc:
            log.warning("LLM disabled: %s (llm sites will be skipped)", exc)
        else:
            budget = config.llm.daily_token_budget
            extractor = Extractor(backend, conn, daily_token_budget=budget)
            checker = MatchChecker(backend, conn, daily_token_budget=budget)

        try:
            notifier = notifier_from_settings(config.alerts, env)
        except NotifyConfigError as exc:
            log.warning("%s; printing alerts to the console instead", exc)
            notifier = ConsoleNotifier()

        ctx = AdapterContext(fetcher=fetcher, extractor=extractor, env=env)
        return cls(config, conn, ctx=ctx, notifier=notifier, checker=checker)

    def close(self) -> None:
        try:
            self.ctx.fetcher.close()
        except Exception:  # Playwright objects can only be closed from their own thread
            log.debug("fetcher close failed", exc_info=True)
        self.ctx.http.close()
        self.conn.close()

    # one run

    def _set_run_id(self, run_id: int | None) -> None:
        if self.ctx.extractor is not None:
            self.ctx.extractor.run_id = run_id
        if self.checker is not None:
            self.checker.run_id = run_id

    def _search(self, site: Site, query: str, pages: dict[str, SearchResult]) -> SearchResult:
        """Search a site; a site without search_url serves one page for every item."""
        if site.search_url is None and site.page_url(query) in pages:
            listings = pages[site.page_url(query)].listings
            return SearchResult(listings, cached=True)
        result = adapters.search(site, query, self.ctx)
        if site.search_url is None:
            pages[site.page_url(query)] = result
        return result

    def run_group(self, group_name: str) -> RunReport:
        group = self.config.site_group(group_name)
        watchlists = [w for w in self.config.watchlists if w.site_group == group_name]
        report = RunReport(group_name)
        report.run_id = self.conn.execute(
            "INSERT INTO runs (site_group, started_at) VALUES (?, ?)",
            (group_name, self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")),
        ).lastrowid
        self._set_run_id(report.run_id)
        pages: dict[str, SearchResult] = {}
        try:
            for site in group.sites:
                self._run_site(site, watchlists, report, pages)
            report.alerts = self.alerts.flush()
        finally:
            self._set_run_id(None)
            self.conn.execute(
                "UPDATE runs SET finished_at = ?, sites_checked = ?, sites_failed = ?,"
                " listings = ?, deals = ?, input_tokens = ?, output_tokens = ?, errors = ?"
                " WHERE id = ?",
                (self.clock().strftime("%Y-%m-%dT%H:%M:%SZ"), report.sites_checked,
                 report.sites_failed, report.listings, report.deals, report.usage.input_tokens,
                 report.usage.output_tokens, json.dumps(report.errors), report.run_id),
            )
        log.info(report.summary())
        for error in report.errors:
            log.warning("  %s", error)
        return report

    def _run_site(self, site: Site, watchlists, report: RunReport,
                  pages: dict[str, SearchResult]) -> None:
        if site.adapter == AdapterType.LLM and self.ctx.extractor is not None:
            if self.ctx.extractor.budget_left() == 0:
                report.errors.append(f"{site.name}: skipped, daily LLM token budget used up")
                return
        report.sites_checked += 1
        failed = False
        for watchlist in watchlists:
            for item in watchlist.items:
                try:
                    result = self._search(site, item.search_query, pages)
                except AdapterUnavailable as exc:
                    report.errors.append(str(exc))
                    report.sites_failed += 1
                    return
                except BudgetExhausted as exc:
                    report.errors.append(f"{site.name}: {exc}; skipping the rest of this site")
                    return
                except (AdapterError, FetchError, ExtractionError) as exc:
                    report.errors.append(f"{site.name} / {item.label}: {exc}")
                    failed = True
                    continue
                except Exception as exc:  # a bug in one adapter must not stop the others
                    log.exception("%s failed", site.name)
                    report.errors.append(f"{site.name}: unexpected {type(exc).__name__}: {exc}")
                    report.sites_failed += 1
                    return
                report.usage += result.usage
                if not result.cached:
                    report.listings += len(result.listings)
                for deal in self.matcher.process(watchlist, item, result.listings):
                    report.deals += 1
                    self.alerts.queue(deal)
        report.sites_failed += failed

    def run_all(self) -> list[RunReport]:
        return [self.run_group(g.name) for g in self.config.site_groups]

    def flush_alerts(self) -> FlushResult:
        return self.alerts.flush()


def serve(config_path: Path, db_path: Path | str = db.DEFAULT_DB_PATH, *,
          extra: Callable[[], None] | None = None) -> None:
    """Run every site group on its interval until interrupted.

    The config file is re-read before each job when it has changed (the web app edits it);
    the pipeline is rebuilt and the group schedule updated. A broken edit is logged and the
    previous config kept. `extra`, if given, runs in the main thread (the web server) and
    the scheduler stops when it returns.
    """
    import threading

    from apscheduler.executors.pool import ThreadPoolExecutor
    from apscheduler.schedulers.background import BackgroundScheduler

    from dealfinder.config import ConfigError, load_config

    config_path = Path(config_path)
    state: dict = {"config": load_config(config_path), "mtime": config_path.stat().st_mtime}
    scheduler = BackgroundScheduler(executors={"default": ThreadPoolExecutor(1)}, timezone=UTC)

    def schedule_groups(config: Config, first: bool = False) -> None:
        wanted = {f"group:{g.name}": g for g in config.site_groups}
        for job in scheduler.get_jobs():
            if job.id.startswith("group:") and job.id not in wanted:
                job.remove()
        now = datetime.now(UTC)
        for i, (job_id, group) in enumerate(wanted.items()):
            seconds = group.interval_minutes * 60
            existing = scheduler.get_job(job_id)
            if existing is not None and existing.trigger.interval.total_seconds() == seconds:
                continue
            scheduler.add_job(
                run_group, "interval", args=[group.name], seconds=seconds,
                jitter=min(120, seconds // 10), coalesce=True, max_instances=1,
                misfire_grace_time=seconds, id=job_id, replace_existing=True,
                next_run_time=now + timedelta(seconds=(10 + 30 * i if first else 5)
                                              + random.uniform(0, 20)),
            )
            log.info("site group %s: every %d min", group.name, group.interval_minutes)

    def pipeline() -> Pipeline:
        # Runs in the single worker thread, which owns the pipeline.
        mtime = config_path.stat().st_mtime
        if mtime != state["mtime"]:
            state["mtime"] = mtime
            try:
                config = load_config(config_path)
            except ConfigError as exc:
                log.error("config change ignored, %s", exc)
            else:
                log.info("config changed; reloading")
                state["config"] = config
                if "p" in state:
                    state.pop("p").close()
                schedule_groups(config)
        if "p" not in state:
            state["p"] = Pipeline.build(state["config"], db_path)
        return state["p"]

    def run_group(name: str) -> None:
        try:
            p = pipeline()
            if name in {g.name for g in p.config.site_groups}:
                p.run_group(name)
        except Exception:
            log.exception("run of %s failed", name)

    def flush() -> None:
        try:
            pipeline().flush_alerts()
        except Exception:
            log.exception("alert flush failed")

    schedule_groups(state["config"], first=True)
    scheduler.add_job(flush, "interval", minutes=FLUSH_MINUTES, coalesce=True, max_instances=1,
                      id="flush-alerts")
    scheduler.start()
    try:
        if extra is not None:
            extra()
        else:
            threading.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        scheduler.shutdown(wait=True)
        if "p" in state:
            state["p"].close()
