# Which LLM should extract deals? GLM-5.3-Flash vs Claude Haiku 5.5

_Researched 2026-10-11._

## What the LLM actually does in Deal Finder

The LLM should **not** be the scraper. Fetching pages, rate limiting, and APIs (eBay Browse API, RSS)
are plain code. The LLM gets used for three narrow jobs:

1. **Extraction**: turning a cleaned page (markdown/HTML) into a list of `{title, price, size, condition, url}`
   for sites with no API or feed.
2. **Match checking**: "Is this listing really a men's M Patagonia Better Sweater, or an accessory / wrong size?"
3. **Coupon search** (product-hunt mode only): run a web search for current codes for one retailer and
   summarise which ones look valid.

That means the model mostly needs **reliable structured JSON**, **resistance to prompt injection** (listing text is
untrusted), and **a low cost per page**. It doesn't need deep reasoning.

## GLM-5.3-Flash at a glance

| | GLM-5.3-Flash (Z.ai / Zhipu) |
|---|---|
| Released | Aug 2026 (it was the anonymous "Ox Alpha" model); MIT-licensed open weights |
| Architecture | MoE, 320B total / 18B active, natively multimodal (can read screenshots) |
| Context / max output | 1M / 128K |
| Price (list, USD) | **$0.15 in / $0.03 cached in / $0.50 out** per 1M tokens (the launch half-price promo ended Sep 9) |
| Web search tool | $0.01 per use (Z.ai-hosted) |
| Tool calling | Supported; one third-party test of GLM 5.3 got 24/24 valid calls |
| Structured output | Listed as supported, but sources **disagree on strict JSON-schema enforcement** (some say JSON mode only, schema not enforced) |
| Independent quality | Artificial Analysis Intelligence Index 57 (#10 overall, #3 open-weight); one small hands-on review passed strict-JSON, retrieval and prompt-injection tests on the first try |
| Self-hosting | Not realistic at home: about 328 GB of weights, needs 90–740 GB of RAM/VRAM |
| Weak spots | Reliability is unmeasured beyond single runs; a 4K default output cap caused truncated tool calls in one test of GLM 5.3; early feedback was mixed |

## Head-to-head for this workload

| | GLM-5.3-Flash | Claude Haiku 5.5 |
|---|---|---|
| Input $/1M | $0.15 | **$0.10** |
| Output $/1M | $0.50 | $0.50 |
| Strict schema-enforced output | Unclear (validate yourself) | Yes (`strict` tools / structured outputs) |
| Vision (screenshots) | Yes | Yes |
| Est. cost per page (~8K in / 800 out) | ~$0.0016 | ~$0.0012 |
| Est. cost at 2,000 pages/day | ~$3.30/day | ~$2.40/day |

Page extraction is input-heavy, so **Haiku 5.5 is actually slightly cheaper per page** and guarantees schema-valid
output. GLM-5.3-Flash's real advantages are open weights and the MIT license, which only matter if you self-host,
and that isn't practical on a home Proxmox box.

## Verdict

**GLM-5.3-Flash is good enough for this job, but not the best default.** It's cheap and seems to handle
extraction and injection well. Still, it isn't cheaper than Haiku 5.5 for page extraction, its strict-schema
support is uncertain, and its reliability evidence is thin.

**Recommendation:** make the LLM provider pluggable (one `Extractor` interface with Anthropic and
OpenAI-compatible backends; Z.ai exposes an OpenAI-compatible endpoint), default to Haiku 5.5, and run a
**bake-off on 30–50 real saved pages from your sites**. Measure field accuracy, JSON validity rate, and cost per
*successful* page, then pick the winner with data instead of benchmarks.

Bigger savings come from not calling an LLM at all: use the eBay API, RSS feeds, and CSS selectors where possible,
and cache by page hash so unchanged pages are never re-extracted.

## Sources
- https://technode.com/2026/08/27/zhipu-identifies-ox-alpha-as-glm-5-3-flash-and-releases-model-weights/
- https://docs.z.ai/guides/vlm/glm-5.3-flash
- https://kingy.ai/blog/glm-5-3-flash-review-tests-pricing/
- https://www.mindstudio.ai/blog/glm-5-3-flash-local-benchmarks
- https://mcpplaygroundonline.com/blog/glm-5-3-mcp-servers
- https://modelcompare.dev/models/zai/glm-5-3-flash
- Anthropic model pricing (Claude API docs, cached 2026-10-06)
