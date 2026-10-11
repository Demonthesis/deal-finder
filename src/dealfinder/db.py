"""SQLite storage with ordered SQL migrations.

Migrations are `migrations/NNNN_description.sql` files shipped in the package. They run
in order, each in its own transaction, and the applied version is tracked in
`PRAGMA user_version`. Never edit a released migration; add a new file instead.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from dealfinder.config import Config

DEFAULT_DB_PATH = Path("data/dealfinder.db")
_MIGRATION_RE = re.compile(r"^(\d{4})_[\w-]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    migrations = []
    for entry in resources.files("dealfinder.migrations").iterdir():
        match = _MIGRATION_RE.match(entry.name)
        if match:
            migrations.append(
                Migration(int(match.group(1)), entry.name, entry.read_text(encoding="utf-8"))
            )
    migrations.sort(key=lambda m: m.version)
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise RuntimeError(f"migration versions must be 1..N with no gaps, got {versions}")
    return migrations


def connect(path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, autocommit=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one transaction (connections are opened in autocommit mode)."""
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply pending migrations. Returns the names of the ones applied."""
    current = schema_version(conn)
    migrations = load_migrations()
    if current > len(migrations):
        raise RuntimeError(
            f"database schema version {current} is newer than this code ({len(migrations)})"
        )
    applied = []
    for m in migrations[current:]:
        with transaction(conn):
            for statement in _split_sql(m.sql):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {m.version}")
        applied.append(m.name)
    return applied


def _split_sql(sql: str) -> list[str]:
    statements, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                statements.append(buf.strip())
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete SQL statement: {buf.strip()[:80]}")
    return statements


def sync_sites(conn: sqlite3.Connection, config: Config) -> int:
    """Upsert every site from the config and reset group memberships.

    Sites no longer in the config are kept (their listings stay), only unlinked from groups.
    Returns the number of distinct sites.
    """
    sites = config.sites()
    with transaction(conn):
        conn.executemany(
            """
            INSERT INTO sites (name, url, adapter, rate_limit_requests, rate_limit_per_seconds)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (name) DO UPDATE SET
                url = excluded.url,
                adapter = excluded.adapter,
                rate_limit_requests = excluded.rate_limit_requests,
                rate_limit_per_seconds = excluded.rate_limit_per_seconds
            """,
            [
                (s.name, str(s.url), s.adapter.value, s.rate_limit.requests,
                 s.rate_limit.per_seconds)
                for s in sites.values()
            ],
        )
        conn.execute("DELETE FROM site_group_sites")
        conn.executemany(
            """
            INSERT INTO site_group_sites (site_group, site_id)
            SELECT ?, id FROM sites WHERE name = ?
            """,
            [(g.name, s.name) for g in config.site_groups for s in g.sites],
        )
    return len(sites)
