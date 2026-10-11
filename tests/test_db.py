import sqlite3

import pytest

from dealfinder import db
from dealfinder.config import load_config
from tests.conftest import EXAMPLE_CONFIG


@pytest.fixture
def conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


def table_names(conn) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {r["name"] for r in rows}


def columns(conn, table) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def test_migrate_creates_schema(conn):
    applied = db.migrate(conn)
    assert applied == [m.name for m in db.load_migrations()]
    assert db.schema_version(conn) == len(db.load_migrations())
    assert {"sites", "site_group_sites", "listings", "price_history", "alerts", "matches", "runs"} <= (
        table_names(conn)
    )
    assert {"url", "title", "price", "currency", "seen_at"} <= columns(conn, "listings")


def test_migrate_is_idempotent(conn):
    db.migrate(conn)
    assert db.migrate(conn) == []


def test_refuses_newer_database(conn):
    conn.execute("PRAGMA user_version = 999")
    with pytest.raises(RuntimeError, match="newer than this code"):
        db.migrate(conn)


def test_failed_migration_rolls_back(conn, monkeypatch):
    bad = db.Migration(1, "0001_bad.sql", "CREATE TABLE ok (x);\nCREATE TABLE broken (;\n")
    monkeypatch.setattr(db, "load_migrations", lambda: [bad])
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn)
    assert db.schema_version(conn) == 0
    assert "ok" not in table_names(conn)


def test_sync_sites_and_relations(conn):
    db.migrate(conn)
    cfg = load_config(EXAMPLE_CONFIG)
    assert db.sync_sites(conn, cfg) == 5
    assert db.sync_sites(conn, cfg) == 5  # rerun is an upsert

    groups = conn.execute(
        """SELECT g.site_group FROM site_group_sites g JOIN sites s ON s.id = g.site_id
           WHERE s.name = 'ebay' ORDER BY 1"""
    ).fetchall()
    assert [r[0] for r in groups] == ["general-retail", "resale-clothing"]

    site_id = conn.execute("SELECT id FROM sites WHERE name = 'ebay'").fetchone()[0]
    listing_id = conn.execute(
        "INSERT INTO listings (site_id, url, title, price) VALUES (?, ?, ?, ?)",
        (site_id, "https://www.ebay.com/itm/1", "Better Sweater M", 39.0),
    ).lastrowid
    conn.execute(
        "INSERT INTO price_history (listing_id, price, currency) VALUES (?, 39.0, 'USD')",
        (listing_id,),
    )
    alert = (listing_id, "Wardrobe upgrades", "Patagonia Better Sweater", 39.0)
    sql = ("INSERT INTO alerts (listing_id, watchlist, item, price, channel, title, body,"
           " created_at) VALUES (?, ?, ?, ?, 'console', 't', 'b', '2026-10-01T00:00:00Z')")
    conn.execute(sql, alert)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, alert)  # one alert per listing/watchlist/item/channel/price
    conn.execute(sql, alert[:3] + (35.0,))  # a lower price is a new alert

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO listings (site_id, url, title) VALUES (999, 'https://x', 't')"
        )  # foreign keys are enforced
