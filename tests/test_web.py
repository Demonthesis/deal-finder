import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from dealfinder import config_edit, db
from dealfinder.config import ConfigError, load_config
from dealfinder.match import Matcher
from dealfinder.models import Listing
from dealfinder.web import create_app
from tests.conftest import EXAMPLE_CONFIG


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "watchlists.yaml"
    shutil.copy(EXAMPLE_CONFIG, path)
    return path


@pytest.fixture
def db_path(tmp_path, config_path):
    path = tmp_path / "d.db"
    conn = db.connect(path)
    db.migrate(conn)
    config = load_config(config_path)
    db.sync_sites(conn, config)
    wl = config.watchlists[1]
    Matcher(conn).process(wl, wl.items[0], [Listing(
        site="ebay", url="https://www.ebay.com/itm/9", title="Sony WH-1000XM6 <b>bold</b>",
        price=279.0)])
    conn.close()
    return path


@pytest.fixture
def client(config_path, db_path):
    app = create_app(config_path, db_path, password="hunter2", env={})
    with TestClient(app) as c:
        yield c


def login(client):
    r = client.post("/login", data={"password": "hunter2", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303


def test_pages_need_login(client):
    for path in ["/", "/watchlists", "/sites", "/runs"]:
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert client.post("/sites/ebay/test", data={"query": "x"},
                       follow_redirects=False).status_code == 303
    assert client.get("/healthz").json() == {"ok": True}


def test_wrong_password_is_rejected(client, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    r = client.post("/login", data={"password": "nope", "next": "/"})
    assert r.status_code == 401 and "Wrong password" in r.text


def test_login_does_not_redirect_offsite(client):
    r = client.post("/login", data={"password": "hunter2", "next": "//evil.example"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"


def test_deals_feed_lists_flagged_deals_escaped(client):
    login(client)
    r = client.get("/")
    assert r.status_code == 200
    assert "Sony WH-1000XM6 &lt;b&gt;bold&lt;/b&gt;" in r.text
    assert "279.00" in r.text
    assert "Sony" not in client.get("/?watchlist=Wardrobe+upgrades").text.split("<table>")[-1]


def test_runs_and_sites_pages_render(client):
    login(client)
    assert "No runs yet" in client.get("/runs").text
    page = client.get("/sites").text
    assert "Test this site" in page and "poshmark" in page


def test_add_edit_delete_item_writes_yaml_and_keeps_comments(client, config_path):
    login(client)
    r = client.post("/watchlists/Buy%20soon/items", data={
        "name": "Steam Deck OLED", "keywords": "steam deck, oled", "exclude": "case",
        "max_price": "450", "currency": "usd", "condition": ["new", "refurbished"],
        "digest": "yes"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    text = config_path.read_text()
    assert "# Deal Finder watchlists." in text  # comments survive
    item = load_config(config_path).watchlists[1].items[-1]
    assert (item.label, item.keywords, item.max_price, item.currency, item.digest) == (
        "Steam Deck OLED", ["steam deck", "oled"], 450, "USD", True)
    assert [c.value for c in item.condition] == ["new", "refurbished"]
    assert "keywords: [steam deck, oled]" in text

    client.post("/watchlists/Buy%20soon/items", data={
        "replace": "Steam Deck OLED", "name": "Steam Deck OLED", "keywords": "steam deck, oled",
        "max_price": "400"})
    assert load_config(config_path).watchlists[1].items[-1].max_price == 400

    client.post("/watchlists/Buy%20soon/items/delete", data={"label": "Steam Deck OLED"})
    assert [i.label for i in load_config(config_path).watchlists[1].items] == ["Sony WH-1000XM6"]
    assert config_path.with_suffix(".yaml.bak").exists()


def test_invalid_edit_shows_errors_and_leaves_file(client, config_path):
    login(client)
    before = config_path.read_text()
    r = client.post("/watchlists/Buy%20soon/items", data={
        "name": "Bad", "keywords": "x", "exclude": "x", "max_price": "10"})
    assert r.status_code == 400 and "both keywords and exclude" in r.text
    r = client.post("/watchlists/Buy%20soon/items", data={"keywords": "x", "max_price": "abc"})
    assert r.status_code == 400
    assert config_path.read_text() == before


def test_new_watchlist_and_delete(client, config_path):
    login(client)
    client.post("/watchlists", data={"watchlist_name": "Camera", "site_group": "general-retail",
                                     "keywords": "fujifilm, x100vi", "max_price": "1400"})
    assert load_config(config_path).watchlists[-1].name == "Camera"
    client.post("/watchlists/Camera/delete")
    assert [w.name for w in load_config(config_path).watchlists] == ["Wardrobe upgrades", "Buy soon"]


def test_add_css_site_and_edit_shared_site(client, config_path):
    login(client)
    r = client.post("/sites", data={
        "group": "resale-clothing", "name": "thredup", "url": "https://www.thredup.com",
        "search_url": "https://www.thredup.com/search?search_text={query}", "adapter": "css",
        "fetcher": "http", "requests": "1", "per_seconds": "20",
        "sel_listing": "div.card", "sel_title": "h3", "sel_url": "", "sel_price": ".price",
        "sel_condition": ""}, follow_redirects=False)
    assert r.status_code == 303, r.text
    site = load_config(config_path).sites()["thredup"]
    assert site.selectors.listing == "div.card" and site.selectors.url == "a@href"

    # eBay is in two groups; editing it must update both
    client.post("/sites", data={
        "group": "resale-clothing", "replace": "ebay", "name": "ebay",
        "url": "https://www.ebay.com", "adapter": "ebay_api", "requests": "2",
        "per_seconds": "1"})
    raw = yaml.safe_load(config_path.read_text())
    limits = [s["rate_limit"]["requests"] for g in raw["site_groups"] for s in g["sites"]
              if s["name"] == "ebay"]
    assert limits == [2, 2]

    client.post("/sites/resale-clothing/thredup/delete")
    assert "thredup" not in load_config(config_path).sites()


def test_test_site_button_shows_errors(client):
    login(client)
    r = client.post("/sites/ebay/test", data={"query": "sony"})
    assert r.status_code == 200 and "EBAY_CLIENT_ID" in r.text
    assert client.post("/sites/nope/test", data={"query": "x"}).status_code == 404


def test_config_edit_unknown_names(config_path):
    with pytest.raises(ConfigError, match="no watchlist"):
        config_edit.delete_item(config_path, "Nope", "x")
    with pytest.raises(ConfigError, match="no item"):
        config_edit.delete_item(config_path, "Buy soon", "x")


def test_no_password_mode(config_path, db_path):
    with TestClient(create_app(config_path, db_path, password=None, env={})) as c:
        assert c.get("/", follow_redirects=False).status_code == 200
