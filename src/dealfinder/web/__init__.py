"""Small web app: deals feed, watchlist and site editing, site testing, run history.

Server-rendered (Jinja) with htmx for the "test this site" button. Single user: every page
needs the password in DEALFINDER_PASSWORD, unless it is empty and the app only listens on
localhost (e.g. behind a tunnel that does its own login). Edits go straight to the YAML config (see
config_edit), which `serve` reloads before its next run.
"""

from __future__ import annotations

import hmac
import json
import secrets
import sqlite3
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from dealfinder import adapters, config_edit, db
from dealfinder.config import (
    AdapterType,
    Condition,
    Config,
    ConfigError,
    FetcherType,
    load_config,
)
from dealfinder.extract import tokens_used_today

TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
TEMPLATES.env.filters["from_json"] = json.loads
PUBLIC_PATHS = {"/login", "/healthz"}


class ConfigHolder:
    """The config file, reloaded when it changes on disk."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._mtime = 0.0
        self._config: Config | None = None

    def get(self) -> Config:
        mtime = self.path.stat().st_mtime
        if self._config is None or mtime != self._mtime:
            self._config = load_config(self.path)
            self._mtime = mtime
        return self._config


def _split(text: str) -> list[str]:
    return [t.strip() for t in text.split(",") if t.strip()]


def _item_from_form(form: Mapping) -> dict:
    digest = form.get("digest", "")
    return {
        "name": form.get("name", "").strip() or None,
        "keywords": _split(form.get("keywords", "")),
        "exclude": _split(form.get("exclude", "")),
        "query": form.get("query", "").strip() or None,
        "max_price": float(form["max_price"]) if form.get("max_price") else None,
        "currency": form.get("currency", "").strip().upper() or None,
        "condition": form.getlist("condition") if hasattr(form, "getlist") else [],
        "digest": {"yes": True, "no": False}.get(digest),
    }


def _site_from_form(form: Mapping) -> dict:
    site = {
        "name": form.get("name", "").strip(),
        "url": form.get("url", "").strip(),
        "search_url": form.get("search_url", "").strip() or None,
        "adapter": form.get("adapter", ""),
        "rate_limit": {"requests": int(form.get("requests") or 1),
                       "per_seconds": float(form.get("per_seconds") or 10)},
    }
    if form.get("fetcher") == FetcherType.BROWSER.value:
        site["fetcher"] = FetcherType.BROWSER.value
    if site["adapter"] == AdapterType.CSS.value:
        site["selectors"] = {k: form.get(f"sel_{k}", "").strip() or None
                             for k in ("listing", "title", "url", "price", "condition")}
    return site


def create_app(config_path: Path, db_path: Path | str = db.DEFAULT_DB_PATH, *,
               password: str | None, env: Mapping[str, str] | None = None,
               session_secret: str | None = None) -> FastAPI:
    """`password=None` disables the login (only for binding to localhost)."""
    import os

    env = dict(os.environ if env is None else env)
    holder = ConfigHolder(config_path)
    holder.get()  # fail fast on a broken config
    app = FastAPI(title="Deal Finder", docs_url=None, redoc_url=None, openapi_url=None)

    def conn() -> sqlite3.Connection:
        c = db.connect(db_path)
        db.migrate(c)
        return c

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if password is not None and request.url.path not in PUBLIC_PATHS:
            if not request.session.get("ok"):
                return RedirectResponse(f"/login?next={quote(request.url.path)}", status_code=303)
        return await call_next(request)

    # Added last so it runs first (outermost) and the session is ready for require_login.
    app.add_middleware(SessionMiddleware, secret_key=session_secret or secrets.token_hex(32),
                       same_site="strict", https_only=False, max_age=30 * 24 * 3600)

    def render(request: Request, name: str, status: int = 200, **context) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(request, name, {"auth": password is not None, **context},
                                          status_code=status)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        return render(request, "login.html", next=next, error=None)

    @app.post("/login")
    def login(request: Request, password_field: str = Form(alias="password"), next: str = Form("/")):
        if password is None or hmac.compare_digest(password_field.encode(), password.encode()):
            request.session["ok"] = True
            target = next if next.startswith("/") and not next.startswith("//") else "/"
            return RedirectResponse(target, status_code=303)
        time.sleep(1)  # slow down guessing
        return render(request, "login.html", status=401, next=next, error="Wrong password.")

    @app.post("/logout")
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # deals

    @app.get("/", response_class=HTMLResponse)
    def deals(request: Request, watchlist: str = "", site: str = "", min_score: float = 0,
              days: int = 14, sort: str = "new"):
        config = holder.get()
        since = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sql = """
            SELECT m.watchlist, m.item, m.score, m.price, m.median_price, m.flagged_at,
                   m.flagged_price, l.title, l.url, l.currency, l.condition, l.in_stock,
                   s.name AS site
            FROM matches m JOIN listings l ON l.id = m.listing_id JOIN sites s ON s.id = l.site_id
            WHERE m.flagged_at >= ? AND m.score >= ? AND m.duplicate_of IS NULL
        """
        params: list = [since, min_score]
        if watchlist:
            sql += " AND m.watchlist = ?"
            params.append(watchlist)
        if site:
            sql += " AND s.name = ?"
            params.append(site)
        sql += " ORDER BY " + ("m.score DESC" if sort == "score" else "m.flagged_at DESC")
        sql += " LIMIT 300"
        c = conn()
        try:
            rows = c.execute(sql, params).fetchall()
        finally:
            c.close()
        return render(request, "deals.html", rows=rows, config=config,
                      filters={"watchlist": watchlist, "site": site, "min_score": min_score,
                               "days": days, "sort": sort})

    # watchlists

    def watchlists_page(request: Request, errors: list[str] | None = None, status: int = 200):
        return render(request, "watchlists.html", status=status, config=holder.get(),
                      conditions=[c.value for c in Condition], errors=errors or [])

    @app.get("/watchlists", response_class=HTMLResponse)
    def watchlists(request: Request):
        return watchlists_page(request)

    def apply(request: Request, page, action):
        """Run a config edit; on success go back to the page, else show the problems."""
        try:
            action()
        except ConfigError as exc:
            return page(request, errors=exc.errors, status=400)
        except (ValueError, KeyError) as exc:
            return page(request, errors=[str(exc)], status=400)
        return RedirectResponse("/" + request.url.path.split("/")[1], status_code=303)

    @app.post("/watchlists")
    async def new_watchlist(request: Request):
        form = await request.form()
        return apply(request, watchlists_page, lambda: config_edit.add_watchlist(
            holder.path, form.get("watchlist_name", "").strip(), form.get("site_group", ""),
            _item_from_form(form)))

    @app.post("/watchlists/{wl}/delete")
    def remove_watchlist(request: Request, wl: str):
        return apply(request, watchlists_page, lambda: config_edit.delete_watchlist(holder.path, wl))

    @app.post("/watchlists/{wl}/items")
    async def add_item(request: Request, wl: str):
        form = await request.form()
        replace = form.get("replace") or None
        return apply(request, watchlists_page, lambda: config_edit.upsert_item(
            holder.path, wl, _item_from_form(form), replace=replace))

    @app.post("/watchlists/{wl}/items/delete")
    async def remove_item(request: Request, wl: str):
        form = await request.form()
        return apply(request, watchlists_page,
                     lambda: config_edit.delete_item(holder.path, wl, form.get("label", "")))

    # sites

    def sites_page(request: Request, errors: list[str] | None = None, status: int = 200):
        return render(request, "sites.html", status=status, config=holder.get(),
                      adapters=[a.value for a in AdapterType], errors=errors or [])

    @app.get("/sites", response_class=HTMLResponse)
    def sites(request: Request):
        return sites_page(request)

    @app.post("/sites")
    async def add_site(request: Request):
        form = await request.form()
        replace = form.get("replace") or None
        return apply(request, sites_page, lambda: config_edit.upsert_site(
            holder.path, form.get("group", ""), _site_from_form(form), replace=replace))

    @app.post("/sites/{group}/{name}/delete")
    def remove_site(request: Request, group: str, name: str):
        return apply(request, sites_page,
                     lambda: config_edit.delete_site(holder.path, group, name))

    @app.post("/sites/{name}/test", response_class=HTMLResponse)
    def test_site(request: Request, name: str, query: str = Form("")):
        from dealfinder.runner import Pipeline

        config = holder.get()
        site = config.sites().get(name)
        if site is None:
            return Response(f"Unknown site {name}", status_code=404)
        pipeline = Pipeline.build(config, db_path, env=env)
        try:
            result = adapters.search(site, query or "test", pipeline.ctx)
            error = None
        except Exception as exc:
            result, error = None, f"{type(exc).__name__}: {exc}"
        finally:
            pipeline.close()
        return render(request, "_test_result.html", site=site, result=result, error=error)

    # runs

    @app.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        config = holder.get()
        c = conn()
        try:
            rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 100").fetchall()
            daily = c.execute(
                "SELECT substr(created_at, 1, 10) AS day, model, SUM(input_tokens) AS input,"
                " SUM(output_tokens) AS output, COUNT(*) AS calls FROM llm_usage"
                " GROUP BY day, model ORDER BY day DESC LIMIT 60"
            ).fetchall()
            today = tokens_used_today(c)
        finally:
            c.close()
        return render(request, "runs.html", rows=rows, daily=daily, today=today,
                      budget=config.llm.daily_token_budget)

    return app
