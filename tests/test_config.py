import pytest

from dealfinder.config import AdapterType, Condition, ConfigError, load_config, parse_config
from tests.conftest import EXAMPLE_CONFIG


def errors_for(data) -> list[str]:
    with pytest.raises(ConfigError) as exc_info:
        parse_config(data)
    return exc_info.value.errors


def test_example_config_is_valid():
    cfg = load_config(EXAMPLE_CONFIG)
    assert {g.name for g in cfg.site_groups} == {"resale-clothing", "general-retail"}
    sweater = cfg.watchlists[0].items[0]
    assert sweater.max_price == 45
    assert sweater.currency == "USD"
    assert Condition.NEW_WITH_TAGS in sweater.condition
    assert cfg.site_group("resale-clothing").sites[0].adapter is AdapterType.EBAY_API
    assert "ebay" in cfg.sites()


def test_rate_limit_interval(example_data):
    cfg = parse_config(example_data)
    grailed = cfg.sites()["grailed"]
    assert grailed.rate_limit.min_interval == 10


def test_unknown_site_group(example_data):
    example_data["watchlists"][0]["site_group"] = "nope"
    [err] = errors_for(example_data)
    assert "unknown site_group 'nope'" in err


def test_negative_max_price_points_at_field(example_data):
    example_data["watchlists"][0]["items"][1]["max_price"] = -5
    [err] = errors_for(example_data)
    assert err.startswith("watchlists[0].items[1].max_price:")
    assert "greater than 0" in err


def test_reports_every_error(example_data):
    site = example_data["site_groups"][0]["sites"][0]
    site["adapter"] = "telepathy"
    site["url"] = "not a url"
    del site["rate_limit"]
    errs = errors_for(example_data)
    locs = {e.split(":")[0] for e in errs}
    assert locs == {
        "site_groups[0].sites[0].adapter",
        "site_groups[0].sites[0].url",
        "site_groups[0].sites[0].rate_limit",
    }


def test_unknown_key_rejected(example_data):
    example_data["watchlists"][0]["items"][0]["max_prise"] = 10
    errs = errors_for(example_data)
    assert any("max_prise" in e and "Extra inputs" in e for e in errs)


def test_bad_condition(example_data):
    example_data["watchlists"][0]["items"][0]["condition"] = ["mint"]
    [err] = errors_for(example_data)
    assert err.startswith("watchlists[0].items[0].condition[0]:")


@pytest.mark.parametrize("keywords", [[], ["  "]])
def test_empty_keywords(example_data, keywords):
    example_data["watchlists"][0]["items"][0]["keywords"] = keywords
    errs = errors_for(example_data)
    assert errs and errs[0].startswith("watchlists[0].items[0].keywords")


def test_keyword_excluded_too(example_data):
    example_data["watchlists"][0]["items"][0]["exclude"] = ["Patagonia"]
    [err] = errors_for(example_data)
    assert "both keywords and exclude: patagonia" in err


def test_duplicate_names(example_data):
    example_data["site_groups"][1]["name"] = "resale-clothing"
    [err] = errors_for(example_data)
    assert "duplicate site group name: resale-clothing" in err


def test_shared_site_must_match(example_data):
    example_data["site_groups"][1]["sites"][0]["rate_limit"]["requests"] = 99
    [err] = errors_for(example_data)
    assert "site 'ebay' is defined differently" in err


def test_bad_currency(example_data):
    example_data["watchlists"][1]["items"][0]["currency"] = "usd"
    [err] = errors_for(example_data)
    assert err.startswith("watchlists[1].items[0].currency:")


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("", "file is empty"),
        ("- just\n- a list\n", "top level must be a mapping"),
        ("site_groups: [\n", "invalid YAML"),
    ],
)
def test_file_level_errors(write_config, content, expected):
    with pytest.raises(ConfigError) as exc_info:
        load_config(write_config(content))
    assert expected in exc_info.value.errors[0]


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError) as exc_info:
        load_config(tmp_path / "missing.yaml")
    assert exc_info.value.errors == ["file not found"]
