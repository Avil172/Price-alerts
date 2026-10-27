# Price alerts (India)

A GitHub Actions job (`.github/workflows/price-alert.yml`) checks every product in
`config.json` (now: RTX 5090 and RTX 4090) across Indian retailers every 30 minutes,
including out-of-stock listings, and opens a GitHub issue that @mentions you (so
GitHub emails it to you) when:

- an out-of-stock listing comes back in stock (**BACK IN STOCK**, with its price), or
- an in-stock listing drops below the last in-stock price seen for it (**PRICE DROP**).

Nothing is sent otherwise. Each product gets its own issue/email.
It runs on GitHub's servers: no PC, no email password, no Claude usage.

## Add a product

Add an entry to `products` in `config.json`, e.g.:

```json
{"id": "rtx5080", "name": "RTX 5080", "enabled": true,
 "search": "rtx 5080", "long_search": "nvidia geforce rtx 5080 graphics card",
 "match_regex": "rtx\\s*-?\\s*5080(?!\\s*(ti|super))", "min_price": 80000, "max_price": 500000}
```

- `search`: short term used on store search boxes; `long_search`: used on Amazon/Flipkart.
- `match_regex`: listing titles must match it (keeps e.g. a 5080 Super out of 5080 alerts).
- `min_price` / `max_price`: listings outside this range are ignored as placeholders.
- Optional `shopify_collections`: a store's collection handle when its search is poor.
- Set `"enabled": false` to pause a product. Its first run is a silent baseline.

## Stores

| Store | How it's read | Reliability |
|---|---|---|
| Aashirwad Computers (SP Road, Bangalore, walk-in) | WooCommerce product API (JSON) | Good |
| Online King (Bangalore) | Store's own product API (JSON) | Good |
| NVIDIA Founders Edition (official India store) | WooCommerce product API (JSON) | Good |
| PrimeABGB | WooCommerce product API (JSON) | Blocks GitHub's servers at times |
| PC Studio | WooCommerce product API (JSON) | Blocks GitHub's servers at times |
| EZPZ Solutions | WooCommerce product API (JSON) | Good |
| EliteHubs | Shopify collection (JSON) | Good |
| Vishal Peripherals | Shopify collection (JSON) | Good |
| Vedant Computers | Search page HTML | Good |
| Computech Store | Search page HTML | Good |
| Flipkart | Search page HTML | OK, layout changes can break it |
| Amazon.in | Search page HTML | Best effort, often blocks cloud servers |

Not included because they block automated requests: MD Computers, ServerBasket,
GameLoot. Checked but no RTX listings: TheITDepot, Clarion, PC Planet, TLG Gaming.
Laptops, prebuilt PCs and accessories are ignored (`exclude_words` in
`config.json`), and each store can be switched off there.

To add another shop: if it runs WooCommerce or Shopify, it's one line in
`STORES` in `tracker.py` using the `woocommerce()` or `shopify()` helper.

## Setup

Nothing to configure. Alerts arrive from notifications@github.com at the email
set under GitHub → Settings → Notifications → Default notifications email, as
long as email is ticked for "Participating, @mentions and custom".
Close the issues as you read them.

Optional repo variables (**Settings → Secrets and variables → Actions → Variables**):
- `ALERT_MENTION`: GitHub username to @mention, if not the repo owner.

To test, open **Actions → Price alerts → Run workflow** and tick the
test box: it opens an issue per product with the current price list.

## Run locally

```
pip install -r requirements.txt
SEND_SUMMARY=1 python tracker.py
```
