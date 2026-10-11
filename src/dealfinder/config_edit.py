"""Edit config/watchlists.yaml from the web app, keeping its comments and layout.

Every change is applied to a round-trip copy of the YAML, validated with the normal config
schema, and only then written (atomically, keeping the previous file as `.bak`). A change
that would make the config invalid raises ConfigError and leaves the file untouched.
"""

from __future__ import annotations

import io
import os
import shutil
import threading
from collections.abc import Callable
from pathlib import Path

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from dealfinder.config import Config, ConfigError, parse_config

_lock = threading.Lock()


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 100
    return y


def _flow(values: list) -> CommentedSeq:
    seq = CommentedSeq(values)
    seq.fa.set_flow_style()
    return seq


def _clean(data: dict) -> CommentedMap:
    """Drop empty optional values and write short lists inline, like the hand-written file."""
    out = CommentedMap()
    for key, value in data.items():
        if value is None or value == [] or value == "":
            continue
        if isinstance(value, list) and all(isinstance(v, str | int | float) for v in value):
            value = _flow(value)
        elif isinstance(value, dict):
            value = _clean(value)
            value.fa.set_flow_style()
        out[key] = value
    return out


def edit_config(path: Path, change: Callable[[CommentedMap], None]) -> Config:
    """Apply `change` to the YAML document at `path`, validate, and save. Returns the new Config."""
    path = Path(path)
    with _lock:
        y = _yaml()
        doc = y.load(path.read_text(encoding="utf-8"))
        change(doc)
        buffer = io.StringIO()
        y.dump(doc, buffer)
        text = buffer.getvalue()
        config = parse_config(yaml.safe_load(text), path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        os.replace(tmp, path)
        return config


def _find(seq, key: str, value: str, what: str) -> int:
    for i, entry in enumerate(seq or []):
        if entry.get(key) == value:
            return i
    raise ConfigError(Path("<edit>"), [f"no {what} named {value!r}"])


def _item_label(item) -> str:
    return item.get("name") or " ".join(item.get("keywords", []))


def _find_item(watchlist, label: str) -> int:
    for i, item in enumerate(watchlist["items"]):
        if _item_label(item) == label:
            return i
    raise ConfigError(Path("<edit>"), [f"no item named {label!r} in {watchlist['name']!r}"])


def upsert_item(path: Path, watchlist: str, item: dict, *, replace: str | None = None) -> Config:
    """Add an item to a watchlist, or replace the item labelled `replace`."""
    def change(doc):
        wl = doc["watchlists"][_find(doc.get("watchlists"), "name", watchlist, "watchlist")]
        if replace is None:
            wl["items"].append(_clean(item))
        else:
            wl["items"][_find_item(wl, replace)] = _clean(item)
    return edit_config(path, change)


def delete_item(path: Path, watchlist: str, label: str) -> Config:
    def change(doc):
        wl = doc["watchlists"][_find(doc.get("watchlists"), "name", watchlist, "watchlist")]
        del wl["items"][_find_item(wl, label)]
    return edit_config(path, change)


def add_watchlist(path: Path, name: str, site_group: str, first_item: dict) -> Config:
    def change(doc):
        if doc.get("watchlists") is None:
            doc["watchlists"] = CommentedSeq()
        doc["watchlists"].append(CommentedMap(
            name=name, site_group=site_group, items=CommentedSeq([_clean(first_item)])))
    return edit_config(path, change)


def delete_watchlist(path: Path, name: str) -> Config:
    def change(doc):
        del doc["watchlists"][_find(doc.get("watchlists"), "name", name, "watchlist")]
    return edit_config(path, change)


def upsert_site(path: Path, group: str, site: dict, *, replace: str | None = None) -> Config:
    """Add a site to a group, or replace the site named `replace` (in every group that has
    it, since a shared site must be defined the same way everywhere)."""
    def change(doc):
        g = doc["site_groups"][_find(doc["site_groups"], "name", group, "site group")]
        if replace is None:
            g["sites"].append(_clean(site))
            return
        _find(g["sites"], "name", replace, "site")
        for other in doc["site_groups"]:
            for i, entry in enumerate(other["sites"]):
                if entry.get("name") == replace:
                    other["sites"][i] = _clean(site)
    return edit_config(path, change)


def delete_site(path: Path, group: str, name: str) -> Config:
    def change(doc):
        g = doc["site_groups"][_find(doc["site_groups"], "name", group, "site group")]
        del g["sites"][_find(g["sites"], "name", name, "site")]
    return edit_config(path, change)
