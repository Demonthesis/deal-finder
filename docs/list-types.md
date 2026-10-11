# List types (hunt modes)

Every list has a **mode**. The mode decides which site group is searched, what counts as a match, and what
counts as a "good deal". Lists live in `config/lists/*.yaml` (and later in the web UI).

## 1. `wardrobe`: thrift and used clothing, filtered by your sizes

A standing list of clothing you want, checked continuously on resale sites.

```yaml
name: Wardrobe upgrades
mode: wardrobe
site_group: resale-clothing
profile:            # your sizes, applied to every item unless the item overrides them
  tops: [M]
  pants: {waist: [32], inseam: [30, 32]}
  shoes: {us: [10, 10.5]}
  fit_notes: "slim, no cropped"
items:
  - want: Patagonia Better Sweater 1/4 zip
    category: tops
    colors: [navy, grey]
    max_price: 45
    condition: [new_with_tags, excellent, good]
    exclude: [womens, kids, vest]
  - want: Red Wing Iron Ranger
    category: shoes
    max_price: 160
```

**Sites (resale-clothing group):** eBay (official Browse API, with size and condition filters), Poshmark,
Mercari, Depop, ThredUp, Grailed, Vinted, The RealReal.
Only eBay has a usable public API. The others need careful scraping of search-result pages, and some use
bot protection. Per the project rules, any site that actively blocks bots is marked *unsupported* instead of
being worked around, and the per-site status goes in `docs/sites.md`.

**Matching:** brand plus item keywords, a size from your profile (sizes are normalised: "Medium" means "M", and
"32x30" means waist 32 and inseam 30), condition, and excluded words. An LLM check is used only when the size or
model is ambiguous.
**Deal score:** the price against your `max_price`, plus the price against what this item usually sells for
(sold-listing history from eBay where available).

## 2. `product_hunt`: one specific product, best price anywhere (new or used), plus coupons

For when you're ready to buy one exact thing.

```yaml
name: Buy soon
mode: product_hunt
site_group: general-retail
items:
  - want: Sony WH-1000XM6
    identifiers: {upc: "...", mpn: "..."}   # optional; makes matching exact
    accept_conditions: [new, open_box, refurbished, used_excellent]
    max_price: 330
    find_coupons: true
    buy_by: 2026-11-30
```

**Sites (general-retail group):** eBay (API), Amazon (the Creators API replaced PA-API in 2026 and needs an Amazon
Associates account; otherwise Amazon is unsupported because it blocks scraping), Best Buy (open product API),
Walmart, Target, B&H, Newegg, Back Market, Swappa, plus manufacturer refurb stores.

**Output:** a one-shot comparison table (total price including shipping and tax estimate, condition, seller
rating, return policy), and the item is then watched until `buy_by` for price drops.

**Coupons:** only when `find_coupons: true`. For the top 2–3 retailers, the agent does a web search for current
codes and cashback offers, then reports them ranked by how fresh and how believable they are. It never auto-applies
or auto-buys. You check out yourself.

## Shared pipeline

```
list → site group → fetch (API / RSS / CSS / LLM extractor) → normalise → match (mode rules)
     → score → dedupe → alert / digest / comparison table
```
