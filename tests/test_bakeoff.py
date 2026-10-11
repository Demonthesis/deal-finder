import json

from typer.testing import CliRunner

from dealfinder.bakeoff import format_report, load_fixtures, run_bakeoff
from dealfinder.cli import app
from dealfinder.extract import ExtractionError, TokenUsage


def write_fixture(root, name, expected):
    d = root / name
    d.mkdir()
    (d / "page.md").write_text("# results")
    (d / "meta.json").write_text(json.dumps(
        {"url": "https://shop.example/s", "site": "shop", "expected": expected}))


class Backend:
    provider = "fake"

    def __init__(self, model, listings, fail_on=()):
        self.model, self.listings, self.fail_on = model, listings, fail_on
        self.n = 0

    def parse(self, system, user, output):
        self.n += 1
        if self.n in self.fail_on:
            raise ExtractionError("bad json")
        return output.model_validate({"listings": self.listings}), TokenUsage(10_000, 1_000)


def test_bakeoff_scores_accuracy_validity_and_cost(tmp_path):
    expected = [
        {"url": "https://shop.example/1", "title": "Sweater M", "price": 40,
         "condition": "used", "in_stock": True},
        {"url": "https://shop.example/2", "title": "Sweater L", "price": 50,
         "condition": "new", "in_stock": True},
    ]
    write_fixture(tmp_path, "a", expected)
    write_fixture(tmp_path, "b", expected)
    good = Backend("claude-haiku-5-5", [
        {"title": "Sweater M", "price": 40, "currency": "USD", "url": "/1",
         "condition": "Pre-owned", "in_stock": True},
        {"title": "Sweater L", "price": 55, "currency": "USD", "url": "/2",
         "condition": "New", "in_stock": True},
    ])
    flaky = Backend("glm-5.3-flash", [
        {"title": "Sweater M", "price": 40, "currency": "USD", "url": "/1",
         "condition": None, "in_stock": True},
        {"title": "Ad", "price": 1, "currency": "USD", "url": "/ad", "condition": None,
         "in_stock": True},
    ], fail_on=(2,))

    haiku, glm = run_bakeoff([good, flaky], load_fixtures(tmp_path), clock=iter(range(100)).__next__)

    assert (haiku.valid, haiku.recall, haiku.precision) == (2, 1.0, 1.0)
    assert haiku.accuracy("price") == 0.5 and haiku.accuracy("condition") == 1.0
    assert (glm.valid, glm.recall, glm.precision) == (1, 0.5, 0.5)
    assert glm.errors == ["b: bad json"]
    assert haiku.cost == 2 * (10_000 * 0.10 + 1_000 * 0.50) / 1e6
    assert glm.cost_per_valid_page == glm.cost
    report = format_report([haiku, glm])
    assert "| claude-haiku-5-5 | 2/2 | 100% |" in report and "glm-5.3-flash: b: bad json" in report


def test_bakeoff_cli_needs_keys_and_fixtures(tmp_path):
    runner = CliRunner()
    result = runner.invoke(app, ["bakeoff", "-m", "anthropic:claude-haiku-5-5"],
                           env={"ANTHROPIC_API_KEY": ""})
    assert result.exit_code == 1 and "ANTHROPIC_API_KEY" in result.output
    result = runner.invoke(app, ["bakeoff", "-m", "openai:glm-5.3-flash@https://api.z.ai/api/paas/v4",
                                 "--fixtures", str(tmp_path)], env={"LLM_API_KEY": "k"})
    assert result.exit_code == 1 and "No fixtures" in result.output
