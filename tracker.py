"""Restock and price-drop tracker for Indian PC retailers.

For every product in config.json, checks each store, compares with the last
saved state, and writes an alert when an out-of-stock listing comes back in
stock, and after that whenever its price drops below the last in-stock price
seen. The GitHub workflow turns each product's alert into an issue that
@mentions the repo owner, so GitHub emails it to them. No email password needed.

Environment:
  SEND_SUMMARY=1      write the full price list even if nothing changed
  ALERT_DIR           where to write <product>.title / <product>.md (default: print only)
"""

import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
STATE_FILE = HERE / "state.json"
IST = timezone(timedelta(hours=5, minutes=30))

def rupees(text):
    m = re.search(r"[\d,]+(?:\.\d+)?", text.replace("₹", " "))
    return int(float(m.group().replace(",", ""))) if m else None


# --- Store adapters. Each returns a list of dicts: title, price, url, in_stock.

def _woocommerce_api(base, query):
    # WooCommerce Store API: structured JSON, no HTML scraping needed.
    out = []
    for page in range(1, 4):
        items = get(f"{base}/wp-json/wc/store/v1/products",
                    params={"search": query, "per_page": 100, "page": page}).json()
        for p in items:
            minor = p["prices"].get("currency_minor_unit", 0)
            price = int(p["prices"]["price"] or 0) // (10 ** minor)
            out.append({"title": BeautifulSoup(p["name"], "html.parser").get_text(),
                        "price": price, "url": p["permalink"], "in_stock": p.get("is_in_stock") is True and p.get("is_purchasable", True) is True and not p.get("is_on_backorder", False)})
        if len(items) < 100:
            break
    return out


def shopify(base, key, product):
    handle = product.get("shopify_collections", {}).get(key)
    if not handle:
        data = get(f"{base}/search/suggest.json", params={
            "q": product["search"].split()[-1], "resources[type]": "product", "resources[limit]": 10}).json()
        return [{"title": p["title"], "price": rupees(str(p["price"])),
                 "url": urljoin(base, p["url"].split("?")[0]), "in_stock": p.get("available") is True}
                for p in data["resources"]["results"]["products"]]
    out = []
    for page in range(1, 11):
        products = get(f"{base}/collections/{handle}/products.json", params={"limit": 250, "page": page}).json()["products"]
        for p in products:
            for v in p["variants"]:
                title = p["title"] if v["title"] == "Default Title" else f"{p['title']} ({v['title']})"
                url = f"{base}/products/{p['handle']}"
                if len(p["variants"]) > 1:
                    url += f"?variant={v['id']}"
                out.append({"title": title, "price": rupees(v["price"]), "url": url,
                            "sku": v.get("sku"), "in_stock": v.get("available") is True})
        if len(products) < 250:
            break
    return out


def onlineking(query):
    # Online King (Bangalore) runs a custom shop backed by a JSON API.
    data = get("https://api.onlineking.in/api/get-products-customer",
               params={"q": query, "limit": 100}).json()
    out = []
    for p in data["products"]:
        if not p.get("status"):
            continue
        price = p["default_price"]
        if p.get("discount_type") == "amount":
            price -= p.get("discount") or 0
        elif p.get("discount_type") == "percent":
            price = round(price * (100 - (p.get("discount") or 0)) / 100)
        out.append({"title": p["product_name"], "price": int(price),
                    "url": f"https://onlineking.in/searchlist?keyword={quote_plus(query)}#{p['id']}",
                    "in_stock": (p.get("stock") or 0) > 0})
    return out


def computech(query):
    soup = BeautifulSoup(get("https://computechstore.in/search", params={"q": query}).text, "html.parser")
    out, seen = [], set()
    for a in soup.select("a[href*='/product/']"):
        title = a.get_text(" ", strip=True)
        if query.split()[-1].lower() not in title.lower() or a["href"] in seen:
            continue
        seen.add(a["href"])
        card = a
        while card.parent is not None and "\u20b9" not in card.get_text():
            card = card.parent
        text = card.get_text(" ", strip=True)
        prices = [rupees(m) for m in re.findall(r"\u20b9\s?[\d,]+", text)]
        if prices:
            out.append({"title": title, "price": min(prices),  # MRP is listed next to the selling price
                        "url": urljoin("https://computechstore.in", a["href"]),
                        "in_stock": not re.search(r"sold out|out of stock", text, re.I)})
    return out


def flipkart(query):
    url = "https://www.flipkart.com/search?q=" + quote_plus(query)
    soup = BeautifulSoup(get(url).text, "html.parser")
    cards = soup.select("div[data-id]")
    if not cards:
        raise RuntimeError("no search results found (page layout may have changed)")
    out, seen = [], set()
    for card in cards:
        link = next((a for a in card.find_all("a", href=True) if "/p/" in a["href"]), None)
        if not link:
            continue
        href = urljoin("https://www.flipkart.com", link["href"].split("?")[0])
        if href in seen:
            continue
        seen.add(href)
        title = (card.find(attrs={"title": True}) or {}).get("title") or link.get_text(" ", strip=True)
        text = card.get_text(" ", strip=True)
        m = re.search(r"₹\s?([\d,]+)", text)
        if m:
            out.append({"title": title, "price": rupees(m.group(1)), "url": href,
                        "in_stock": not re.search(r"out of stock|coming soon|currently unavailable", text, re.I)})
    return out


def amazon(query):
    # Amazon often serves a bot check to cloud servers; treated as best effort.
    url = "https://www.amazon.in/s?k=" + quote_plus(query)
    soup = BeautifulSoup(get(url).text, "html.parser")
    if soup.select_one("form[action*='validateCaptcha']"):
        raise RuntimeError("blocked by captcha")
    cards = soup.select("div[data-component-type='s-search-result']")
    if not cards:
        raise RuntimeError("no search results returned (likely a bot check)")
    out = []
    for card in cards:
        title = card.select_one("h2")
        price = card.select_one(".a-price .a-offscreen")
        link = card.select_one("a.a-link-normal[href*='/dp/']")
        if title and price and link:
            href = urljoin("https://www.amazon.in", link["href"])
            m = re.search(r"/dp/([A-Z0-9]{10})", href)
            out.append({"title": title.get_text(" ", strip=True), "price": rupees(price.get_text()),
                        "url": f"https://www.amazon.in/dp/{m.group(1)}" if m else href,
                        "in_stock": True})
    return out


# Each store: (display name, function(product) -> listings). product["search"] is a short
# search term such as "rtx 5090"; product["long_search"] is used by the big marketplaces.
STORES = {
    "primeabgb": ("PrimeABGB", lambda p: woocommerce("https://www.primeabgb.com", p["search"])),
    "vedant": ("Vedant Computers", lambda p: vedant(p["search"])),
    "elitehubs": ("EliteHubs", lambda p: shopify("https://elitehubs.com", "elitehubs", p)),
    "vishal": ("Vishal Peripherals", lambda p: shopify("https://vishalperipherals.com", "vishal", p)),
    "aashirwad": ("Aashirwad Computers (SP Road, Bangalore)", lambda p: woocommerce("https://aashirwadcomputers.in", p["search"])),
    "onlineking": ("Online King (Bangalore)", lambda p: onlineking(p["search"].split()[-1])),
    "nvidia_fe": ("NVIDIA Founders Edition (official India store)", lambda p: woocommerce("https://nvidia.onlinestpl.co.in", p["search"].split()[-1])),
    "pcstudio": ("PC Studio", lambda p: woocommerce("https://www.pcstudio.in", p["search"].split()[-1])),
    "ezpz": ("EZPZ Solutions", lambda p: woocommerce("https://ezpzsolutions.in", p["search"].split()[-1])),
    "computech": ("Computech Store", lambda p: computech(p["search"])),
    "flipkart": ("Flipkart", lambda p: flipkart(p["long_search"])),
    "amazon": ("Amazon.in", lambda p: amazon(p["long_search"])),
}


# Browser-compatible transport and conservative public-store adapters.
# This does not solve CAPTCHAs or use proxies. A blocked site stays a reported failure.
import atexit
from urllib.parse import urlparse, urlencode
from curl_cffi import requests as browser_requests

COVERAGE = {}
SESSION = browser_requests.Session(impersonate="chrome")
_BROWSER = None
_CONTEXTS = {}
_WARMED = set()


def close_browser():
    global _BROWSER
    if _BROWSER:
        pw, browser = _BROWSER
        browser.close()
        pw.stop()
        _BROWSER = None


atexit.register(close_browser)


def blocked(text):
    soup = BeautifulSoup(text[:250000], "html.parser")
    title = soup.title.get_text(" ", strip=True).lower() if soup.title else ""
    return (any(x in title for x in ("just a moment", "attention required", "access denied", "robot check"))
            or bool(soup.select_one("form[action*='validateCaptcha'], #challenge-running, #cf-challenge-running")))


def browser_get(url):
    global _BROWSER
    if os.environ.get("ENABLE_BROWSER_FALLBACK") != "1":
        raise RuntimeError("browser fallback disabled")
    from playwright.sync_api import sync_playwright
    if _BROWSER is None:
        pw = sync_playwright().start()
        try:
            _BROWSER = (pw, pw.chromium.launch(headless=True))
        except Exception:
            pw.stop()
            raise
    host = urlparse(url).netloc
    if host not in _CONTEXTS:
        _CONTEXTS[host] = _BROWSER[1].new_context(locale="en-IN")
    page = _CONTEXTS[host].new_page()
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)
        text = page.content()
        if not response or response.status >= 400 or blocked(text):
            raise RuntimeError(f"browser HTTP {response.status if response else 'no response'} / access challenge")
        if "json" in response.headers.get("content-type", ""):
            text = page.locator("body").inner_text()
        result = requests.Response()
        result.status_code, result.url, result.encoding = 200, page.url, "utf-8"
        result._content = text.encode("utf-8")
        return result
    finally:
        page.close()


def get(url, **kw):
    params = kw.pop("params", None)
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
    host = urlparse(url).netloc
    last = "request failed"
    for attempt in range(2):
        try:
            # The session retains cookies; TLS/HTTP2 match Chrome instead of Python requests.
            r = SESSION.get(url, timeout=20, headers={"Accept-Language": "en-IN,en;q=0.9"}, **kw)
            if r.status_code == 200 and not blocked(r.text):
                return r
            last = f"HTTP {r.status_code}" + (" / access challenge" if blocked(r.text) else "")
            if r.status_code in (403, 429, 503) or blocked(r.text):
                if host not in _WARMED:
                    _WARMED.add(host)
                    SESSION.get(f"https://{host}/", timeout=15)
                else:
                    break
            elif r.status_code not in (500, 502, 504):
                break
        except Exception as e:
            last = str(e)[:160]
        if attempt == 0:
            time.sleep(1)
    try:
        return browser_get(url)
    except Exception as e:
        raise RuntimeError(f"{last}; fallback: {e}") from e


def stock_status(card):
    text = card.get_text(" ", strip=True)
    classes = " ".join(card.get("class", []))
    if re.search(r"out[\s_-]*of[\s_-]*stock|outofstock|sold[\s_-]*out|notify\s*me|pre[\s-]*order|back[\s-]*order|enquir", text + " " + classes, re.I):
        return False
    if re.search(r"\bin\s*stock\b|\binstock\b", text + " " + classes, re.I):
        return True
    for button in card.select("button, a, input[type=submit]"):
        label = " ".join([button.get_text(" ", strip=True), button.get("aria-label", ""), button.get("title", ""), button.get("value", "")])
        if re.search(r"add\s*to\s*cart|buy\s*now", label, re.I):
            if not button.has_attr("disabled") and button.get("aria-disabled") != "true" and "disabled" not in button.get("class", []):
                return True
    return None


def selling_price(card):
    for selector in (".special-price", ".price-new", ".price ins", ".price .ins", ".price-normal", "[itemprop=price]", ".price", ".product-price"):
        node = card.select_one(selector)
        if node:
            copy = BeautifulSoup(str(node), "html.parser")
            for old in copy.select("del, .del, .price-old, .previous-price, .price-tax, .woocommerce-price-suffix"):
                old.decompose()
            value = node.get("content") or copy.get_text(" ", strip=True)
            price = rupees(value)
            if price:
                return price
    return None


def parse_cards(html, base, query):
    soup = BeautifulSoup(html, "html.parser")
    for hidden in soup.select("template, script, style, noscript"):
        hidden.decompose()
    cards = soup.select(".product-thumb, .product-grid-item, li.product, .product-small.box, .product-item, .product-card")
    out = {}
    token = query.split()[-1]
    for card in cards:
        link = card.select_one(".name a, h2 a, h3 a, h4 a, a.product-name, .woocommerce-loop-product__link")
        if not link:
            continue
        title = link.get("title") or link.get_text(" ", strip=True)
        if not re.search(r"rtx\s*-?\s*" + re.escape(token), title, re.I):
            img = link.select_one("img[alt]")
            title = img.get("alt", "") if img else title
        if token not in title:
            continue
        price = selling_price(card)
        if price is None:
            continue
        url = urljoin(base, link.get("href", "")).split("?")[0]
        if urlparse(url).netloc.removeprefix("www.") != urlparse(base).netloc.removeprefix("www."):
            continue
        out[url] = {"title": title, "price": price, "url": url, "in_stock": stock_status(card)}
    return list(out.values()), soup


def html_catalog(base, query, style="woo", start_url=None):
    if style == "opencart":
        url = base + "/index.php?" + urlencode({"route": "product/search", "search": query.split()[-1], "limit": 100})
    else:
        url = base + "/?" + urlencode({"s": query.split()[-1], "post_type": "product"})
    url = start_url or url
    out, visited = {}, set()
    for _ in range(5):
        if url in visited:
            break
        visited.add(url)
        items, soup = parse_cards((browser_get(url) if style == "clarion" else get(url)).text, base, query)
        for item in items:
            out[item["url"]] = item
        if not items and not soup.select(".product-thumb, .product-grid-item, li.product, .product-item, .product-card"):
            if not re.search(r"no products|no results|did not match|there is no product|not found", soup.get_text(" ", strip=True), re.I):
                raise RuntimeError("unrecognized catalogue layout; not a verified empty result")
        link = soup.select_one("a[rel=next], a.next.page-numbers, .pagination li.active + li a")
        if not link:
            break
        url = urljoin(base, link["href"])
        if urlparse(url).netloc != urlparse(base).netloc:
            break
    return list(out.values())


def woocommerce(base, query):
    try:
        items = _woocommerce_api(base, query.split()[-1])
        # Store API stock is structured evidence; still exclude non-purchasable offers below.
        return items
    except Exception as api_error:
        print(f"    API unavailable ({str(api_error)[:100]}); checking storefront HTML")
        return html_catalog(base, query)


def vedant(query):
    return html_catalog("https://www.vedantcomputers.com", query, "opencart")


def mdcomputers(query):
    base = "https://mdcomputers.in"
    rows = html_catalog(base, query, "opencart")
    # The current store search defaults to IN stock. Its visible stock filter has
    # a separate catalogue route; poll it too or restocks could never be detected.
    out_url = base + "/index.php?" + urlencode({
        "route": "extension/ultimate_filters/module/filter", "module_id": 3188,
        "search": query.split()[-1], "stock_status": "out", "limit": 100})
    rows += html_catalog(base, query, "opencart", start_url=out_url)
    return list({row["url"]: row for row in rows}.values())


def clarion(query):
    base = "https://shop.clarioncomputers.in"
    url = base + "/products?" + urlencode({"query": query.split()[-1]})
    return html_catalog(base, query, "clarion", start_url=url)


# Extra stores start with a silent baseline. Existing false settings are respected.
EXTRA_STORES = {
    "mdcomputers": ("MDComputers", lambda p: mdcomputers(p["search"])),
    "tlggaming": ("TLG Gaming", lambda p: html_catalog("https://tlggaming.com", p["search"], "opencart")),
    "nationalpc": ("NationalPC", lambda p: html_catalog("https://nationalpc.in", p["search"], "opencart")),
    "clarion": ("Clarion Computers", lambda p: clarion(p["search"])),
    "itgadgetsonline": ("ITGadgetsOnline", lambda p: woocommerce("https://itgadgetsonline.com", p["search"])),
    "modxcomputers": ("ModxComputers", lambda p: woocommerce("https://modxcomputers.com", p["search"])),
    "gamesncomps": ("GamesnComps", lambda p: woocommerce("https://gamesncomps.com", p["search"])),
    "sclgaming": ("SCL Gaming", lambda p: woocommerce("https://sclgaming.in", p["search"])),
    "aarna": ("Aarna Computers", lambda p: woocommerce("https://aarnacomputers.com", p["search"])),
    "techcure": ("TechCure India", lambda p: shopify("https://techcureindia.com", "techcure", p)),
    "hardwarecorpus": ("Hardware Corpus", lambda p: shopify("https://hardwarecorpus.in", "hardwarecorpus", p)),
}
STORES.update(EXTRA_STORES)
for _key in EXTRA_STORES:
    CONFIG["stores"].setdefault(_key, True)
for _product in CONFIG["products"]:
    if _product["id"] == "rtx5090":
        _product.setdefault("shopify_collections", {}).setdefault("techcure", "rtx-5090-graphics-cards")


def matches(product, item):
    t = item["title"].lower()
    excludes = CONFIG["exclude_words"] + product.get("exclude_words", [])
    return (re.search(product["match_regex"], t, re.I) and not any(w in t for w in excludes)
            and item["price"] and product["min_price"] <= item["price"] <= product["max_price"])


def collect(product):
    listings, errors = {}, {}
    statuses = {}
    for key, enabled in CONFIG["stores"].items():
        if not enabled:
            continue
        name, fn = STORES[key]
        try:
            raw = [i for i in fn(product) if matches(product, i)]
            unknown = sum(i["in_stock"] is None for i in raw)
            items = [i for i in raw if i["in_stock"] is not None]
            if unknown:
                errors[name] = f"{unknown} listings have unknown stock; prior observations retained"
                print(f"  {name}: {unknown} unknown-stock listings excluded")
            for i in items:
                i["store"] = name
                listings[i["url"]] = i
            statuses[name] = {"listings": len(items), "in_stock": sum(i["in_stock"] for i in items), "unknown_stock": unknown}
            print(f"  {name}: {len(items)} listings ({sum(i['in_stock'] for i in items)} in stock)")
        except Exception as e:  # one broken store must not stop the others
            errors[name] = str(e)[:200]
            print(f"  {name}: FAILED ({e})")
    COVERAGE[product["id"]] = {"checked_ist": datetime.now(IST).isoformat(timespec="minutes"), "stores": statuses, "errors": errors}
    return listings, errors


def fmt(p):
    s = str(p)
    if len(s) <= 3:
        return "₹" + s
    head, tail = s[:-3], s[-3:]
    head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
    return f"₹{head},{tail}"


def check(product, old):
    """Returns (new state listings, alert subject or None, alert body)."""
    name = product["name"]
    now, errors = collect(product)
    # Alert when a listing comes (back) in stock, and after that only when its price drops
    # below the last in-stock price seen for the same listing.
    alerts = []
    known_stores = {i["store"] for i in old.values()}  # a newly added store starts as a silent baseline
    for url, item in now.items():
        prev = old.get(url)
        last = prev and (prev.get("last_price") or (prev["price"] if prev["in_stock"] else None))
        if item["in_stock"]:
            was_in_stock = prev and prev["in_stock"]
            if prev and item["store"] in known_stores and not was_in_stock:
                note = f" (last in-stock price {fmt(last)})" if last else ""
                alerts.append(f"BACK IN STOCK at {fmt(item['price'])}{note} | {item['store']} | {item['title']}\n  {url}")
            elif last and item["price"] < last:
                alerts.append(f"PRICE DROP {fmt(last)} -> {fmt(item['price'])} "
                              f"(-{fmt(last - item['price'])}) | {item['store']} | {item['title']}\n  {url}")
            item["last_price"] = item["price"]
        elif last:
            item["last_price"] = last

    # Keep listings not seen this run (store failed, or search results shuffled) so a
    # listing that briefly drops out of results doesn't look "back in stock" later.
    merged = dict(old)
    merged.update(now)

    in_stock = sorted((i for i in now.values() if i["in_stock"]), key=lambda i: i["price"])
    summary = [f"In-stock {name} listings, cheapest first:"]
    summary += [f"  {fmt(i['price'])} | {i['store']} | {i['title']}\n    {i['url']}" for i in in_stock[:15]] or ["  none in stock right now"]
    if errors:
        summary.append("\nStores that could not be checked this run: " +
                       "; ".join(f"{k} ({v})" for k, v in errors.items()))

    if not alerts and not (os.environ.get("SEND_SUMMARY") == "1"):
        return merged, None, None
    subject = (f"{name} alert: {alerts[0].split(' |')[0]}" if alerts
               else f"{name} price summary ({len(in_stock)} in stock)")
    body = "\n\n".join(alerts + ["\n".join(summary)]) if alerts else "\n".join(summary)
    return merged, subject, body


def main():
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    products = state.get("products", {})
    for product in CONFIG["products"]:
        if not product.get("enabled", True):
            continue
        pid = product["id"]
        print(f"{product['name']}:")
        listings, subject, body = check(product, products.get(pid, {}))
        first_run = pid not in products
        products[pid] = listings
        if subject and os.environ.get("DRY_RUN") != "1" and not (first_run and os.environ.get("SEND_SUMMARY") != "1"):
            send_email(pid, subject, body)
        else:
            print("  no alert")
    (HERE / "coverage.json").write_text(json.dumps(COVERAGE, indent=2, ensure_ascii=False), encoding="utf-8")
    report = os.environ.get("GITHUB_STEP_SUMMARY")
    if report:
        with open(report, "a", encoding="utf-8") as f:
            for pid, coverage in COVERAGE.items():
                f.write(f"\n### {pid} coverage — {coverage['checked_ist']}\n\n| Store | Listings | In stock | Status |\n|---|---:|---:|---|\n")
                for store in sorted(set(coverage["stores"]) | set(coverage["errors"])):
                    data = coverage["stores"].get(store, {})
                    status = coverage["errors"].get(store, "Checked")
                    status = status.replace("|", "/").replace("\n", " ")
                    f.write(f"| {store} | {data.get('listings', '—')} | {data.get('in_stock', '—')} | {status} |\n")
    if os.environ.get("DRY_RUN") == "1":
        print("Dry run: no alerts sent and no price history changed")
        return
    STATE_FILE.write_text(json.dumps({"updated": datetime.now(IST).isoformat(timespec="minutes"),
                                      "products": products},
                                     indent=1, ensure_ascii=False, sort_keys=True) + "\n")


def send_email(pid, subject, body):
    print(f"Subject: {subject}\n\n{body}")
    out = os.environ.get("ALERT_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / f"{pid}.title").write_text(subject)
        # Markdown: hard line breaks so the plain-text layout survives in the issue/email.
        md = "  \n".join(line.strip() for line in body.splitlines())
        (Path(out) / f"{pid}.md").write_text(md + "\n")


if __name__ == "__main__":
    main()
