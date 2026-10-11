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
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Check each site group on its interval until stopped."""
    from dealfinder.runner import serve

    _setup_logging(verbose)
    cfg = _load_or_exit(config)
    serve(cfg, db_path)


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


if __name__ == "__main__":
    app()
