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
CONFIG = json.loads((HERE / "config.json").read_text())
STATE_FILE = HERE / "state.json"
IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9",
}


def get(url, **kw):
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30, **kw)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = str(e)
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(last)


def rupees(text):
    m = re.search(r"[\d,]+(?:\.\d+)?", text.replace("₹", " "))
    return int(float(m.group().replace(",", ""))) if m else None


# --- Store adapters. Each returns a list of dicts: title, price, url, in_stock.

def woocommerce(base, query):
    # WooCommerce Store API: structured JSON, no HTML scraping needed.
    out = []
    for page in range(1, 4):
        items = get(f"{base}/wp-json/wc/store/v1/products",
                    params={"search": query, "per_page": 100, "page": page}).json()
        for p in items:
            minor = p["prices"].get("currency_minor_unit", 0)
            price = int(p["prices"]["price"] or 0) // (10 ** minor)
            out.append({"title": BeautifulSoup(p["name"], "html.parser").get_text(),
                        "price": price, "url": p["permalink"], "in_stock": p["is_in_stock"]})
        if len(items) < 100:
            break
    return out


def shopify(base, key, product):
    # Shopify exposes every collection as JSON at /collections/<handle>/products.json.
    # Use the product's collection for this store when config names one, since
    # Shopify's own search ranks poorly; otherwise fall back to predictive search.
    handle = product.get("shopify_collections", {}).get(key)
    if not handle:
        data = get(f"{base}/search/suggest.json", params={
            "q": product["search"], "resources[type]": "product", "resources[limit]": 10}).json()
        return [{"title": p["title"], "price": rupees(str(p["price"])),
                 "url": urljoin(base, p["url"].split("?")[0]), "in_stock": p["available"]}
                for p in data["resources"]["results"]["products"]]
    data = get(f"{base}/collections/{handle}/products.json", params={"limit": 250}).json()
    out = []
    for p in data["products"]:
        for v in p["variants"]:
            title = p["title"] if v["title"] == "Default Title" else f"{p['title']} ({v['title']})"
            out.append({"title": title, "price": rupees(v["price"]),
                        "url": f"{base}/products/{p['handle']}", "in_stock": v["available"]})
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


def vedant(query):
    soup = BeautifulSoup(get("https://www.vedantcomputers.com/index.php", params={
        "route": "product/search", "search": query, "limit": 100}).text, "html.parser")
    out = []
    for card in soup.select(".product-thumb"):
        name = card.select_one(".name a")
        price = card.select_one(".price-new") or card.select_one(".price-normal")
        if not name or not price:
            continue
        text = card.get_text(" ", strip=True)
        out.append({"title": name.get_text(strip=True), "price": rupees(price.get_text()),
                    "url": name["href"].split("?")[0],
                    "in_stock": bool(re.search(r"Stock:\s*In Stock", text, re.I))})
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


def matches(product, item):
    t = item["title"].lower()
    excludes = CONFIG["exclude_words"] + product.get("exclude_words", [])
    return (re.search(product["match_regex"], t, re.I) and not any(w in t for w in excludes)
            and item["price"] and product["min_price"] <= item["price"] <= product["max_price"])


def collect(product):
    listings, errors = {}, {}
    for key, enabled in CONFIG["stores"].items():
        if not enabled:
            continue
        name, fn = STORES[key]
        try:
            items = [i for i in fn(product) if matches(product, i)]
            for i in items:
                i["store"] = name
                listings[i["url"]] = i
            print(f"  {name}: {len(items)} listings ({sum(i['in_stock'] for i in items)} in stock)")
        except Exception as e:  # one broken store must not stop the others
            errors[name] = str(e)[:200]
            print(f"  {name}: FAILED ({e})")
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
            if item["store"] in known_stores and not was_in_stock:
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
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    products = state.get("products", {})
    for product in CONFIG["products"]:
        if not product.get("enabled", True):
            continue
        pid = product["id"]
        print(f"{product['name']}:")
        listings, subject, body = check(product, products.get(pid, {}))
        first_run = pid not in products
        products[pid] = listings
        if subject and not (first_run and os.environ.get("SEND_SUMMARY") != "1"):
            send_email(pid, subject, body)
        else:
            print("  no alert")
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
