"""Regression tests: prices, stock uncertainty, transport failures and alert history."""
import copy
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from bs4 import BeautifulSoup
import tracker as t

P = t.CONFIG["products"][0]
BASE = "https://shop.example"


def item(price=900000, stock=True, url=BASE + "/card"):
    return {"title": "MSI RTX 5090 32GB", "price": price, "in_stock": stock,
            "url": url, "store": "Test store"}


class ParserTests(unittest.TestCase):
    def test_clarion_templates_do_not_claim_sold_out(self):
        html = '''<div class="product-card"><template><span>Out of Stock</span></template>
        <a class="product-name" href="/card">RTX 5090</a>
        <div class="product-price"><span class="special-price">₹962,990</span><span class="previous-price">₹1,099,990</span></div>
        <button title="Add to Cart"></button></div>'''
        rows, _ = t.parse_cards(html, BASE, "5090")
        self.assertEqual(rows[0]["price"], 962990)
        self.assertIs(rows[0]["in_stock"], True)

    def test_mdcomputers_also_polls_sold_out_filter(self):
        with patch.object(t, "html_catalog", side_effect=[[item()], [item(stock=False, url=BASE + "/soldout")]]) as catalog:
            rows = t.mdcomputers("rtx 5090")
        self.assertEqual(len(rows), 2)
        self.assertIn("stock_status=out", catalog.call_args.kwargs["start_url"])
        self.assertEqual([row["in_stock"] for row in rows], [True, False])

    def test_clarion_uses_the_current_shop_domain(self):
        with patch.object(t, "html_catalog", return_value=[]) as catalog:
            t.clarion("rtx 5090")
        self.assertEqual(catalog.call_args.args[0], "https://shop.clarioncomputers.in")
        self.assertEqual(catalog.call_args.args[2], "clarion")
        self.assertIn("/products?query=5090", catalog.call_args.kwargs["start_url"])

    def test_mdcomputers_sale_price_not_mrp(self):
        html = '''<div class="product-grid-item"><h3><a href="/card">MSI RTX 5090 32GB</a></h3>
        <span class="price"><span class="del">₹1,200,000</span><span class="ins">₹903,200</span></span>
        <button>Add to Cart</button></div>'''
        rows, _ = t.parse_cards(html, BASE, "rtx 5090")
        self.assertEqual(rows[0]["price"], 903200)
        self.assertIs(rows[0]["in_stock"], True)

    def test_vedant_price_normal_and_stock(self):
        html = '''<div class="product-thumb"><div class="name"><a href="/card?search=5090">RTX 5090</a></div>
        Stock: In Stock <span class="price-normal">₹926,000.00</span></div>'''
        rows, _ = t.parse_cards(html, BASE, "5090")
        self.assertEqual(rows[0]["price"], 926000)
        self.assertEqual(rows[0]["url"], BASE + "/card")
        self.assertIs(rows[0]["in_stock"], True)

    def test_stock_uncertain_disabled_and_negative(self):
        for html, expected in [('<div>₹900000</div>', None),
                               ('<div><button disabled>Add to Cart</button></div>', None),
                               ('<div class="outofstock"><button>Add to Cart</button></div>', False),
                               ('<div>Notify Me <button>Add to Cart</button></div>', False),
                               ('<div>Pre-order In Stock</div>', False)]:
            with self.subTest(html=html):
                self.assertIs(t.stock_status(BeautifulSoup(html, "html.parser").div), expected)

    def test_cross_site_links_excluded(self):
        html = '<li class="product"><h2><a href="https://other.example/card">RTX 5090</a></h2><span class="price">₹900000</span></li>'
        self.assertEqual(t.parse_cards(html, BASE, "5090")[0], [])

    def test_challenge_not_a_success(self):
        self.assertTrue(t.blocked('<title>Just a moment...</title>'))
        self.assertFalse(t.blocked('<title>Graphics cards</title><div>RTX 5090</div>'))

    def test_accessory_and_d_variant_filtered(self):
        self.assertFalse(t.matches(P, dict(item(), title="RTX 5090 water block")))
        self.assertFalse(t.matches(P, dict(item(), title="RTX 5090 D V2 24GB")))
        self.assertFalse(t.matches(P, item(price=0)))

    def test_unrecognized_html_is_failure_not_empty_stock(self):
        with patch.object(t, "get", return_value=SimpleNamespace(text='<html>Unrecognized shop</html>')):
            with self.assertRaisesRegex(RuntimeError, "unrecognized"):
                t.html_catalog(BASE, "5090")

    def test_woocommerce_html_fallback(self):
        with patch.object(t, "_woocommerce_api", side_effect=RuntimeError("HTTP 403")), \
             patch.object(t, "html_catalog", return_value=[item()]) as fallback:
            self.assertEqual(len(t.woocommerce(BASE, "rtx 5090")), 1)
            fallback.assert_called_once_with(BASE, "rtx 5090")

    def test_woocommerce_minor_units_and_unpurchasable(self):
        data = [{"name": "RTX 5090", "prices": {"price": "90320000", "currency_minor_unit": 2},
                 "permalink": BASE + "/card", "is_in_stock": True, "is_purchasable": False}]
        with patch.object(t, "get", return_value=SimpleNamespace(json=lambda: data)):
            result = t._woocommerce_api(BASE, "5090")[0]
        self.assertEqual(result["price"], 903200)
        self.assertFalse(result["in_stock"])

    def test_shopify_variants_have_distinct_keys(self):
        data = {"products": [{"title": "RTX 5090", "handle": "card", "variants": [
            {"id": 1, "title": "Black", "price": "900000", "available": True},
            {"id": 2, "title": "White", "price": "950000", "available": False}]}]}
        product = dict(P, shopify_collections={"test": "5090"})
        with patch.object(t, "get", return_value=SimpleNamespace(json=lambda: data)):
            rows = t.shopify(BASE, "test", product)
        self.assertEqual(len({row["url"] for row in rows}), 2)
        self.assertEqual([row["in_stock"] for row in rows], [True, False])


class HistoryTests(unittest.TestCase):
    def run_check(self, old, now, errors=None):
        with patch.dict(os.environ, {"SEND_SUMMARY": "0"}), \
             patch.object(t, "collect", return_value=(copy.deepcopy(now), errors or {})):
            return t.check(P, copy.deepcopy(old))

    def test_failure_retains_last_observation_without_alert(self):
        old = {BASE + "/card": item()}
        merged, subject, _ = self.run_check(old, {}, {"Test store": "HTTP 403"})
        self.assertEqual(merged, old)
        self.assertIsNone(subject)

    def test_new_url_is_not_mislabeled_restock(self):
        old = {BASE + "/card": item()}
        _, subject, _ = self.run_check(old, {BASE + "/new": item(url=BASE + "/new")})
        self.assertIsNone(subject)

    def test_restock_and_price_drop_alert_once(self):
        url = BASE + "/card"
        old = {url: item(stock=False)}
        merged, subject, _ = self.run_check(old, {url: item()})
        self.assertIn("BACK IN STOCK", subject)
        merged, subject, _ = self.run_check(merged, {url: item(price=899000)})
        self.assertIn("PRICE DROP", subject)
        self.assertIsNone(self.run_check(merged, {url: item(price=899000)})[1])

    def test_unknown_stock_never_overwrites_history(self):
        config = dict(t.CONFIG, stores={"test": True})
        with patch.object(t, "CONFIG", config), \
             patch.dict(t.STORES, {"test": ("Test store", lambda p: [item(stock=None)])}):
            now, errors = t.collect(P)
        self.assertEqual(now, {})
        self.assertIn("unknown stock", errors["Test store"])


if __name__ == "__main__":
    unittest.main()
