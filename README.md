# Price alerts (India)

GitHub Actions checks the enabled products in config.json (RTX 5090 and RTX 4090)
every 30 minutes at minutes 7 and 37. GitHub may delay scheduled runs.
No ChatGPT/Claude/API calls or subscriptions are used by this repository.

A GitHub issue mentions the repository owner when a previously observed sold-out
listing returns in stock, or an in-stock listing falls below its last observed
in-stock selling price. The first observation for a new listing is a silent
baseline. Unchanged prices and unsuccessful checks do not send routine updates.

## Retailer coverage

| Stores | Adapter |
|---|---|
| PrimeABGB, PCStudio, EZPZ, Aashirwad, NVIDIA official India store | WooCommerce Store API, then storefront HTML |
| MDComputers, Vedant, TLG Gaming, NationalPC | OpenCart/storefront search with product-card price and stock parsing |
| EliteHubs, Vishal Peripherals, TechCure India, Hardware Corpus | Shopify collections or predictive search |
| Clarion, ITGadgetsOnline, ModxComputers, GamesnComps, SCL Gaming, Aarna Computers | WooCommerce Store API, then storefront HTML |
| Computech Store, Amazon.in, Flipkart | Site-specific search parsers; marketplace access is best effort |

A configured store is an attempted source, not a guarantee of successful coverage.
The Actions run summary lists each store's current result or error. coverage.json
is also uploaded as an artifact. Collection/search coverage is bounded; stores
can hide sold-out products, change layouts, cap results, or block cloud IPs.
Shopify collections are paginated; predictive search returns up to ten results.

## Access and stock handling

The scraper uses a persistent Chrome-compatible curl_cffi session, cookie warmup,
and bounded retries. The workflow installs Chromium for an optional Playwright
fallback. WooCommerce API failures fall back to the public search storefront.
CAPTCHAs are not solved, certificate validation is not disabled, and no proxy or
paid scraping service is used. An IP-level block may still require retailer
permission or an appropriately located runner; a browser alone cannot guarantee access.

Selling prices are extracted separately from crossed-out MRP. Explicit sold-out,
preorder, backorder, notify-me and enquiry states override generic purchase buttons.
An unknown stock state does not overwrite the previous observation or trigger an
alert. A failed/missing listing retains historical state but is excluded from the
current run's in-stock summary. A newly discovered URL is not labeled a restock.

Storefront stock/buttons and APIs are evidence from the retailer, not checkout,
seller-authorisation, warranty or delivery-PIN verification. Review the seller and
final shipping/taxes before buying. computerselection.shop is deliberately absent.

## Configuration

Enable/disable stores under stores in config.json. New adapters have corresponding
switches there. Products define id, name, enabled, search, long_search, match_regex,
min_price and max_price; shopify_collections optionally maps a store key to a full
collection handle. Matching excludes laptops, prebuilt PCs and accessories.

For example, add an RTX 5080 product with a unique id, search "rtx 5080", regex
"rtx\\s*-?\\s*5080(?!\\s*(ti|super))", and suitable price bounds. A new product starts
with a silent baseline. Set enabled to false to pause one product.

## Alerts and testing

GitHub emails the issue mention according to your GitHub notification settings.
ALERT_MENTION is an optional Actions variable overriding the repository owner.

Actions -> Price alerts -> Run workflow offers:

- dry_run (default true): fetch live data and show coverage without sending alerts
  or changing cached price history.
- send_summary (default false): send the current price list even if no price drops;
  this has an effect only when dry_run is false.

Scheduled runs remain live. Validation pushes on fix/retailer-access are dry runs.
Every run first executes the parser/history regression tests. State remains in
Actions cache and is not committed to the repository.

## Run locally

Run these from your assigned working folder:

    pip install -r requirements.txt
    python -m playwright install chromium
    python -m unittest test_tracker -v

Set ENABLE_BROWSER_FALLBACK=1 and DRY_RUN=1 before running python tracker.py for a
live diagnostic without notifications or state changes. Remove DRY_RUN for normal
monitoring. Set SEND_SUMMARY=1 only when an intentional full summary is wanted.
