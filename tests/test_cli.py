import sqlite3

import yaml
from typer.testing import CliRunner

from dealfinder import db
from dealfinder.cli import app
from tests.conftest import EXAMPLE_CONFIG

runner = CliRunner()


def test_validate_example_config():
    result = runner.invoke(app, ["validate-config", "--config", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 0, result.output
    assert "OK:" in result.output
    assert "2 site group(s), 4 site(s), 2 watchlist(s), 3 item(s)" in result.output


def test_validate_invalid_config(example_data, write_config):
    example_data["watchlists"][0]["items"][0]["max_price"] = "cheap"
    example_data["watchlists"][1]["site_group"] = "ghost"
    path = write_config(example_data)
    result = runner.invoke(app, ["validate-config", "-c", str(path)])
    assert result.exit_code == 1
    assert "Invalid config" in result.output
    assert "watchlists[0].items[0].max_price: Input should be a valid number" in result.output


def test_validate_reports_cross_reference_error(example_data, write_config):
    example_data["watchlists"][1]["site_group"] = "ghost"
    result = runner.invoke(app, ["validate-config", "-c", str(write_config(example_data))])
    assert result.exit_code == 1
    assert "unknown site_group 'ghost'" in result.output


def test_validate_missing_file(tmp_path):
    result = runner.invoke(app, ["validate-config", "-c", str(tmp_path / "nope.yaml")])
    assert result.exit_code == 1
    assert "file not found" in result.output


def test_init_db(tmp_path):
    db_path = tmp_path / "sub" / "deals.db"
    result = runner.invoke(app, ["init-db", "--db", str(db_path), "-c", str(EXAMPLE_CONFIG)])
    assert result.exit_code == 0, result.output
    assert f"Applied {len(db.load_migrations())} migration(s)" in result.output
    assert "Synced 4 site(s)" in result.output

    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT count(*) FROM sites").fetchone()[0] == 4

    again = runner.invoke(app, ["init-db", "--db", str(db_path)])
    assert again.exit_code == 0
    assert "Applied" not in again.output


def test_init_db_rejects_bad_config(tmp_path, write_config):
    path = write_config(yaml.safe_dump({"site_groups": []}))
    db_path = tmp_path / "deals.db"
    result = runner.invoke(app, ["init-db", "--db", str(db_path), "-c", str(path)])
    assert result.exit_code == 1
    assert not db_path.exists()
