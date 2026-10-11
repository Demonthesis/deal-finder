import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic
import httpx
import pytest

from dealfinder import db
from dealfinder.config import Condition
from dealfinder.extract import (
    AnthropicBackend,
    BudgetExhausted,
    ExtractedListing,
    ExtractionError,
    ExtractionOutput,
    Extractor,
    LLMConfigError,
    OpenAICompatibleBackend,
    SYSTEM_PROMPT,
    TokenUsage,
    _user_message,
    to_listings,
    tokens_used_today,
)
from dealfinder.models import normalize_condition, parse_price

PAGE_URL = "https://shop.example/search?q=sweater"


def listing(**kw) -> dict:
    return {"title": "Patagonia Better Sweater", "price": 40.0, "currency": "USD",
            "url": "/item/1", "condition": "Pre-owned", "in_stock": True} | kw


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    db.migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def fake_anthropic():
    """A local server speaking the Messages API; `replies` is a list of (status, body)."""
    state = {"requests": [], "replies": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            state["requests"].append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            status, body = state["replies"].pop(0)
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    client = anthropic.Anthropic(
        api_key="test", base_url=f"http://127.0.0.1:{httpd.server_port}", max_retries=0
    )
    yield client, state
    httpd.shutdown()
    httpd.server_close()


def message(listings: list[dict], stop_reason="end_turn") -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": json.dumps({"listings": listings})}],
        "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 80},
    }


def test_anthropic_backend_uses_structured_output(fake_anthropic):
    client, state = fake_anthropic
    state["replies"].append((200, message([listing()])))
    output, usage = AnthropicBackend(client=client).parse(SYSTEM_PROMPT, _user_message("# Results", PAGE_URL), ExtractionOutput)

    assert output.listings[0].title == "Patagonia Better Sweater"
    assert usage == TokenUsage(1200, 80)
    request = state["requests"][0]
    assert request["model"] == "claude-haiku-5-5"
    assert request["output_config"]["effort"] == "low"
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert "untrusted" in request["system"]
    assert "<page>\n# Results\n</page>" in request["messages"][0]["content"]


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_anthropic_backend_rejects_incomplete_replies(fake_anthropic, stop_reason):
    client, state = fake_anthropic
    state["replies"].append((200, message([], stop_reason=stop_reason)))
    with pytest.raises(ExtractionError):
        AnthropicBackend(client=client).parse("s", "page", ExtractionOutput)


def test_anthropic_backend_bad_key_is_a_config_error(fake_anthropic):
    client, state = fake_anthropic
    state["replies"].append((401, {"type": "error", "error": {
        "type": "authentication_error", "message": "invalid x-api-key"}}))
    with pytest.raises(LLMConfigError):
        AnthropicBackend(client=client).parse("s", "page", ExtractionOutput)


def openai_backend(handler) -> OpenAICompatibleBackend:
    return OpenAICompatibleBackend(
        "glm-5.3-flash", base_url="https://llm.example/v4", api_key="k",
        client=httpx.Client(transport=httpx.MockTransport(handler)), max_attempts=1,
    )


def chat_reply(content: str, finish_reason="stop") -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 50},
    })


def test_openai_backend_validates_and_retries_once():
    requests = []
    replies = [chat_reply("not json at all"),
               chat_reply("```json\n" + json.dumps({"listings": [listing()]}) + "\n```")]

    def handler(request):
        requests.append(json.loads(request.content))
        return replies.pop(0)

    output, usage = openai_backend(handler).parse(SYSTEM_PROMPT, "page", ExtractionOutput)
    assert len(output.listings) == 1
    assert usage == TokenUsage(2000, 100)
    assert requests[0]["max_tokens"] >= 16000
    assert requests[1]["messages"][-1]["role"] == "user"  # the correction request
    assert "schema" in requests[0]["messages"][0]["content"]


def test_openai_backend_gives_up_after_second_invalid_reply():
    with pytest.raises(ExtractionError, match="schema"):
        openai_backend(lambda r: chat_reply('{"listings": "nope"}')).parse("s", "page", ExtractionOutput)


def test_openai_backend_truncated_output_is_an_error():
    with pytest.raises(ExtractionError, match="cut off"):
        openai_backend(lambda r: chat_reply('{"listings": [', "length")).parse("s", "p", ExtractionOutput)


def test_openai_backend_bad_key_is_a_config_error():
    with pytest.raises(LLMConfigError):
        openai_backend(lambda r: httpx.Response(401)).parse("s", "page", ExtractionOutput)


class FakeBackend:
    provider = "fake"
    model = "claude-haiku-5-5"

    def __init__(self, listings=None):
        self.calls = 0
        self.listings = listings if listings is not None else [listing()]

    def parse(self, system, user, output):
        self.calls += 1
        return output.model_validate({"listings": self.listings}), TokenUsage(1000, 100)


def test_extractor_caches_by_page_content(conn):
    backend = FakeBackend()
    extractor = Extractor(backend, conn)

    first = extractor.extract("page v1", PAGE_URL, site="shop")
    again = extractor.extract("page v1", PAGE_URL, site="shop")
    changed = extractor.extract("page v2", PAGE_URL, site="shop")

    assert backend.calls == 2
    assert not first.cached and again.cached and not changed.cached
    assert again.listings == first.listings
    assert again.usage == TokenUsage()
    assert tokens_used_today(conn) == 2200


def test_extractor_cache_is_per_model(conn):
    Extractor(FakeBackend(), conn).extract("page", PAGE_URL, site="shop")
    other = FakeBackend()
    other.model = "glm-5.3-flash"
    assert not Extractor(other, conn).extract("page", PAGE_URL, site="shop").cached


def test_extractor_stops_when_budget_is_used(conn):
    extractor = Extractor(FakeBackend(), conn, daily_token_budget=1500)
    extractor.extract("page 1", PAGE_URL, site="shop")  # uses 1100, 400 left
    extractor.extract("page 2", PAGE_URL, site="shop")  # allowed, goes over
    with pytest.raises(BudgetExhausted):
        extractor.extract("page 3", PAGE_URL, site="shop")
    # cached pages are still free
    assert extractor.extract("page 1", PAGE_URL, site="shop").cached


def test_extractor_refuses_huge_pages_instead_of_truncating(conn):
    with pytest.raises(ExtractionError, match="limit"):
        Extractor(FakeBackend(), conn).extract("x" * 500_000, PAGE_URL, site="shop")


def test_to_listings_normalises_model_output():
    output = ExtractionOutput(listings=[
        ExtractedListing(**listing()),
        ExtractedListing(**listing(url="https://shop.example/item/1")),  # duplicate
        ExtractedListing(**listing(url=None)),  # no link
        ExtractedListing(**listing(url="javascript:void(0)")),
        ExtractedListing(**listing(title="  ")),
        ExtractedListing(**listing(url="/item/2", price=-5, currency="dollars",
                                   condition="NWT", in_stock=None)),
        ExtractedListing(**listing(url="/item/3", in_stock=False, condition="Like new")),
    ])
    result = to_listings(output, PAGE_URL, "shop")
    assert [r.url for r in result] == [
        "https://shop.example/item/1", "https://shop.example/item/2", "https://shop.example/item/3"
    ]
    assert result[0].condition is Condition.USED
    assert result[1].price is None and result[1].currency == "USD"
    assert result[1].condition is Condition.NEW_WITH_TAGS and result[1].in_stock
    assert result[2].condition is Condition.EXCELLENT and not result[2].in_stock


@pytest.mark.parametrize("text, expected", [
    ("Pre-owned", Condition.USED),
    ("Used - Good", Condition.GOOD),
    ("Brand New", Condition.NEW),
    ("New with tags", Condition.NEW_WITH_TAGS),
    ("Like New", Condition.EXCELLENT),
    ("Open box", Condition.OPEN_BOX),
    ("Certified Refurbished", Condition.REFURBISHED),
    ("open_box", Condition.OPEN_BOX),
    ("", None),
    ("Mystery", None),
])
def test_normalize_condition(text, expected):
    assert normalize_condition(text) is expected


@pytest.mark.parametrize("text, expected", [
    ("$1,299.99", 1299.99), ("From 45 USD", 45.0), ("£12", 12.0), ("$4.5", 4.5),
    ("US $120.00 (was $150)", 120.0), ("Free", None), (None, None),
])
def test_parse_price(text, expected):
    assert parse_price(text) == expected
