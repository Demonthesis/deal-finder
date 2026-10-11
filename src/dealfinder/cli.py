"""Command-line interface."""

from __future__ import annotations

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


if __name__ == "__main__":
    app()
