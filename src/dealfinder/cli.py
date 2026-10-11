"""Command-line interface."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from dealfinder import db
from dealfinder.config import DEFAULT_CONFIG_PATH, ConfigError, load_config

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Deal Finder")

ConfigOption = Annotated[
    Path, typer.Option("--config", "-c", help="Path to watchlists.yaml.")
]


def _load_or_exit(path: Path):
    try:
        return load_config(path)
    except ConfigError as exc:
        typer.secho(f"Invalid config: {exc.path}", fg=typer.colors.RED, err=True)
        for line in exc.errors:
            typer.echo(f"  - {line}", err=True)
        raise typer.Exit(code=1) from None


@app.command("validate-config")
def validate_config(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Check a watchlists config file and report every problem found."""
    cfg = _load_or_exit(config)
    n_items = sum(len(w.items) for w in cfg.watchlists)
    typer.secho(f"OK: {config}", fg=typer.colors.GREEN)
    typer.echo(
        f"  {len(cfg.site_groups)} site group(s), {len(cfg.sites())} site(s), "
        f"{len(cfg.watchlists)} watchlist(s), {n_items} item(s)"
    )


@app.command("init-db")
def init_db(
    db_path: Annotated[
        Path, typer.Option("--db", help="SQLite database file.")
    ] = db.DEFAULT_DB_PATH,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Also sync sites from this config."),
    ] = None,
) -> None:
    """Create the database (or apply pending migrations)."""
    cfg = _load_or_exit(config) if config else None
    conn = db.connect(db_path)
    try:
        applied = db.migrate(conn)
        version = db.schema_version(conn)
        if applied:
            typer.echo(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
        typer.secho(f"Database ready: {db_path} (schema v{version})", fg=typer.colors.GREEN)
        if cfg:
            n = db.sync_sites(conn, cfg)
            typer.echo(f"Synced {n} site(s) from {config}")
    finally:
        conn.close()


DbOption = Annotated[Path, typer.Option("--db", help="SQLite database file.")]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    for noisy in ("httpx", "httpcore", "apscheduler", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@app.command("run-once")
def run_once(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    db_path: DbOption = db.DEFAULT_DB_PATH,
    group: Annotated[
        list[str] | None, typer.Option("--group", "-g", help="Only this site group (repeatable).")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Check every site group once, then send due alerts."""
    from dealfinder.runner import Pipeline

    _setup_logging(verbose)
    cfg = _load_or_exit(config)
    known = {g.name for g in cfg.site_groups}
    for name in group or []:
        if name not in known:
            typer.secho(f"Unknown site group {name!r} (have: {', '.join(sorted(known))})",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
    pipeline = Pipeline.build(cfg, db_path)
    try:
        reports = [pipeline.run_group(g) for g in group] if group else pipeline.run_all()
    finally:
        pipeline.close()
    for report in reports:
        typer.echo(report.summary())
        for error in report.errors:
            typer.secho(f"  - {error}", fg=typer.colors.YELLOW)
    if reports and all(r.sites_checked and r.sites_failed == r.sites_checked for r in reports):
        raise typer.Exit(code=2)


@app.command("serve")
def serve_cmd(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    db_path: DbOption = db.DEFAULT_DB_PATH,
    host: Annotated[str, typer.Option(help="Web app address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Web app port.")] = 8080,
    web: Annotated[bool, typer.Option(help="Also serve the web app.")] = True,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Check each site group on its interval, and serve the web app, until stopped.

    The web app needs DEALFINDER_PASSWORD unless it only listens on localhost.
    """
    import os

    from dealfinder.runner import serve

    _setup_logging(verbose)
    _load_or_exit(config)
    extra = None
    if web:
        import uvicorn

        from dealfinder.web import create_app

        password = os.environ.get("DEALFINDER_PASSWORD") or None
        if password is None and host not in ("127.0.0.1", "localhost", "::1"):
            typer.secho(f"Set DEALFINDER_PASSWORD to serve the web app on {host}",
                        fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        web_app = create_app(config, db_path, password=password,
                             session_secret=os.environ.get("DEALFINDER_SECRET"))

        def extra() -> None:
            logging.getLogger("dealfinder").info("web app on http://%s:%d", host, port)
            uvicorn.run(web_app, host=host, port=port, log_level="warning")

    serve(config, db_path, extra=extra)


@app.command("test-site")
def test_site(
    site_name: Annotated[str, typer.Argument(help="Site name from the config.")],
    query: Annotated[str, typer.Argument(help="What to search for.")],
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    db_path: DbOption = db.DEFAULT_DB_PATH,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Search one site and print the listings it returns (nothing is matched or alerted)."""
    from dealfinder import adapters
    from dealfinder.runner import Pipeline

    _setup_logging(verbose)
    cfg = _load_or_exit(config)
    site = cfg.sites().get(site_name)
    if site is None:
        typer.secho(f"Unknown site {site_name!r} (have: {', '.join(sorted(cfg.sites()))})",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    pipeline = Pipeline.build(cfg, db_path)
    try:
        result = adapters.search(site, query, pipeline.ctx)
    except Exception as exc:
        typer.secho(f"{type(exc).__name__}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    finally:
        pipeline.close()
    for listing in result.listings:
        price = f"{listing.price:,.2f} {listing.currency}" if listing.price is not None else "-"
        condition = listing.condition.value if listing.condition else "-"
        typer.echo(f"{price:>14}  {condition:<13} {listing.title}\n{'':>29}{listing.url}")
    extra = " (cached)" if result.cached else ""
    typer.echo(f"{len(result.listings)} listing(s), {result.usage.total:,} LLM tokens{extra}")


@app.command("capture-page")
def capture_page(
    site_name: Annotated[str, typer.Argument(help="Site name from the config.")],
    query: Annotated[str, typer.Argument(help="What to search for.")],
    out: Annotated[Path, typer.Option("--out", help="Fixture directory.")] = Path("bakeoff"),
    config: ConfigOption = DEFAULT_CONFIG_PATH,
) -> None:
    """Save a site's cleaned search page as a bake-off fixture (label meta.json afterwards)."""
    import json
    import re

    from dealfinder.fetch import Fetcher

    cfg = _load_or_exit(config)
    site = cfg.sites().get(site_name)
    if site is None:
        typer.secho(f"Unknown site {site_name!r}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    with Fetcher.from_config(cfg) as fetcher:
        page = fetcher.fetch(site.page_url(query))
    name = re.sub(r"[^a-z0-9]+", "-", f"{site_name} {query}".lower()).strip("-")
    target = out / name
    target.mkdir(parents=True, exist_ok=True)
    (target / "page.md").write_text(page.text)
    (target / "meta.json").write_text(json.dumps(
        {"url": page.final_url, "site": site_name, "expected": []}, indent=2))
    typer.echo(f"Saved {target} ({len(page.text):,} chars). Now fill in meta.json 'expected'.")


@app.command("bakeoff")
def bakeoff_cmd(
    model: Annotated[list[str], typer.Option(
        "--model", "-m",
        help="anthropic:MODEL (ANTHROPIC_API_KEY) or openai:MODEL@BASE_URL (LLM_API_KEY). "
             "Repeatable.")],
    fixtures: Annotated[Path, typer.Option("--fixtures", help="Fixture directory.")] = Path("bakeoff"),
) -> None:
    """Compare LLM backends on labelled pages: accuracy, validity, latency and cost."""
    from dealfinder.bakeoff import format_report, load_fixtures, run_bakeoff
    from dealfinder.config import LLMSettings
    from dealfinder.extract import LLMConfigError, backend_from_settings

    backends = []
    for spec in model:
        provider, _, rest = spec.partition(":")
        name, _, base_url = rest.partition("@")
        try:
            if provider == "anthropic":
                settings = LLMSettings(provider="anthropic", model=name)
            elif provider == "openai":
                settings = LLMSettings(provider="openai_compatible", model=name, base_url=base_url)
            else:
                raise ValueError("provider must be anthropic or openai")
            backends.append(backend_from_settings(settings))
        except (ValueError, LLMConfigError) as exc:
            typer.secho(f"--model {spec}: {exc}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1) from None
    pages = load_fixtures(fixtures) if fixtures.is_dir() else []
    if not pages:
        typer.secho(f"No fixtures in {fixtures} (use capture-page)", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    typer.echo(format_report(run_bakeoff(backends, pages)))


if __name__ == "__main__":
    app()
