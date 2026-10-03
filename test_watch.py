"""Run with: uv run --no-project python -m unittest ebay-watch/test_watch.py"""

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import watch  # noqa: E402

DAY = 86400.0


def item(item_id: str, price: float, shipping: float | None = 0.0, title: str = "Steam Deck OLED 512GB") -> dict:
    it = {"itemId": item_id, "title": title, "price": {"value": f"{price:.2f}", "currency": "USD"}, "itemWebUrl": f"https://www.ebay.com/itm/{item_id}", "condition": "Used"}
    if shipping is not None:
        it["shippingOptions"] = [{"shippingCostType": "FIXED", "shippingCost": {"value": f"{shipping:.2f}", "currency": "USD"}}]
    return it


def market(n: int = 30) -> list[dict]:
    # Delivered prices spread 400..458, median 429
    return [item(f"m{i}", 400 + 2 * i) for i in range(n)]


class DeliveredTest(unittest.TestCase):
    def test_adds_cheapest_shipping(self) -> None:
        it = item("a", 100, 15)
        it["shippingOptions"].append({"shippingCost": {"value": "9.50", "currency": "USD"}})
        self.assertEqual(watch.delivered(it), (109.5, 9.5))

    def test_unquoted_shipping_is_none(self) -> None:
        self.assertEqual(watch.delivered(item("a", 100, None)), (100.0, None))


class RobustZTest(unittest.TestCase):
    def test_junk_listings_do_not_hide_a_deal(self) -> None:
        # A mean/stdev z for 80 here is about -0.3, because the $9999 listing
        # blows up the stdev. The median/MAD version still sees it.
        sample = list(range(100, 121)) + [1, 9999]
        self.assertLess(watch.robust_z(80, sample), -3)

    def test_flat_sample_has_no_z(self) -> None:
        self.assertIsNone(watch.robust_z(50, [100] * 10))


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE listing (watch TEXT, item_id TEXT, price REAL, shipping_known INTEGER, first_seen REAL, last_seen REAL, PRIMARY KEY (watch, item_id))")
        self.w = {"name": "deck", "query": "steam deck", "max_price": 300, "max_z": -2.0, "min_price": 150, "exclude": ["for parts"]}
        self.baseline_deals, self.baseline = watch.check(self.db, self.w, market(), 1000.0)

    def test_first_poll_is_a_silent_baseline(self) -> None:
        self.assertEqual(self.baseline_deals, [])
        self.assertEqual(self.baseline["count"], 30)
        self.assertEqual(self.baseline["median"], 429)

    def test_cheap_new_listing_alerts(self) -> None:
        deals, baseline = watch.check(self.db, self.w, market() + [item("new", 280, 15)], 2000.0)
        self.assertIsNone(baseline)
        self.assertEqual([d["price"] for d in deals], [295])

    def test_low_z_alerts_above_max_price(self) -> None:
        deals, _ = watch.check(self.db, self.w, [item("new", 360)], 2000.0)
        self.assertEqual(len(deals), 1)
        self.assertLess(deals[0]["z"], -2)

    def test_ordinary_new_listing_is_quiet(self) -> None:
        deals, _ = watch.check(self.db, self.w, [item("new", 425)], 2000.0)
        self.assertEqual(deals, [])

    def test_seen_listing_does_not_alert_again(self) -> None:
        watch.check(self.db, self.w, [item("new", 280)], 2000.0)
        deals, _ = watch.check(self.db, self.w, [item("new", 280)], 3000.0)
        self.assertEqual(deals, [])

    def test_excluded_and_too_cheap_are_ignored(self) -> None:
        deals, _ = watch.check(self.db, self.w, [item("p", 200, title="Steam Deck FOR PARTS"), item("box", 20)], 2000.0)
        self.assertEqual(deals, [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM listing WHERE item_id IN ('p', 'box')").fetchone()[0], 0)

    def test_unquoted_shipping_only_qualifies_on_price(self) -> None:
        deals, _ = watch.check(self.db, self.w, [item("a", 360, None), item("b", 290, None)], 2000.0)
        self.assertEqual([(d["price"], d["z"]) for d in deals], [(290, None)])

    def test_z_needs_enough_recent_history(self) -> None:
        # 31 days later the whole baseline has aged out of the window
        deals, _ = watch.check(self.db, self.w, [item("new", 360)], 1000.0 + 31 * DAY)
        self.assertEqual(deals, [])


    def test_variation_listings_are_ignored(self) -> None:
        group = item("g", 200)
        group["itemGroupHref"] = "https://api.ebay.com/buy/browse/v1/item/get_items_by_item_group?item_group_id=1"
        deals, _ = watch.check(self.db, self.w, [group], 2000.0)
        self.assertEqual(deals, [])

    def test_new_and_poorly_rated_sellers_are_ignored(self) -> None:
        w = {**self.w, "min_seller_feedback": 10, "min_seller_percent": 97}
        fresh, shady, good = item("fresh", 200), item("shady", 200), item("good", 200)
        fresh["seller"] = {"feedbackScore": 2, "feedbackPercentage": "100.0"}
        shady["seller"] = {"feedbackScore": 900, "feedbackPercentage": "91.2"}
        good["seller"] = {"feedbackScore": 900, "feedbackPercentage": "99.6"}
        deals, _ = watch.check(self.db, w, [fresh, shady, good], 2000.0)
        self.assertEqual([(d["url"], d["seller"]) for d in deals], [("https://www.ebay.com/itm/good", "seller 900 (99.6%)")])


class PushDealsTest(unittest.TestCase):
    def test_caps_pushes_and_summarises_the_rest(self) -> None:
        deals = [{"title": "t", "price": float(p), "shipping": 0.0, "z": -3.0, "median": 400.0, "url": "u", "image": None, "condition": "Used", "seller": "seller 500 (100.0%)"} for p in range(200, 208)]
        with mock.patch.object(watch, "push") as push:
            watch.push_deals({"name": "deck", "query": "steam deck"}, deals)
        titles = [c.args[0] for c in push.call_args_list]
        self.assertEqual(titles[:2], ["deck: $200.00 delivered", "deck: $201.00 delivered"])
        self.assertEqual(titles[-1], "deck: 3 more deals")
        self.assertEqual(len(titles), 6)


class PollTest(unittest.TestCase):
    def test_failed_push_leaves_the_deal_for_next_poll(self) -> None:
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE listing (watch TEXT, item_id TEXT, price REAL, shipping_known INTEGER, first_seen REAL, last_seen REAL, PRIMARY KEY (watch, item_id))")
        config = {"zip": "10001", "watch": [{"name": "deck", "query": "steam deck", "max_price": 300}]}
        with mock.patch.object(watch, "search", side_effect=[market(), market() + [item("new", 280)], market() + [item("new", 280)]]), mock.patch.object(watch, "push") as push:
            self.assertEqual(watch.poll(db, config), [])
            push.side_effect = OSError("ntfy down")
            self.assertEqual(len(watch.poll(db, config)), 1)
            push.side_effect = None
            push.reset_mock()
            self.assertEqual(watch.poll(db, config), [])
        self.assertEqual([c.args[0] for c in push.call_args_list], ["deck: $280.00 delivered"])


if __name__ == "__main__":
    unittest.main()
