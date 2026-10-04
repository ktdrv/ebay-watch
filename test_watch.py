"""Run with: uv run --no-project python -m unittest ebay-watch/test_watch.py"""

import io
import sqlite3
import sys
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import watch  # noqa: E402

DAY = watch.DAY
NOW = 100 * DAY


def item(item_id: str, price: float, shipping: float | None = 0.0, title: str = "Sapphire Pulse RX 6800 16GB", listed: float | None = None) -> dict:
    it = {"itemId": item_id, "title": title, "price": {"value": f"{price:.2f}", "currency": "USD"}, "itemWebUrl": f"https://www.ebay.com/itm/{item_id}", "condition": "Used"}
    if shipping is not None:
        it["shippingOptions"] = [{"shippingCostType": "FIXED", "shippingCost": {"value": f"{shipping:.2f}", "currency": "USD"}}]
    if listed is not None:
        it["itemCreationDate"] = datetime.fromtimestamp(listed, timezone.utc).isoformat().replace("+00:00", "Z")
    return it


def new_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(watch.SCHEMA)
    return db


def seed(db: sqlite3.Connection, level: Callable[[int], float], days: int = 14) -> None:
    """A watch past its first poll, with ten listings on each of the past
    `days` days, priced level(days_ago) - 9 .. + 9."""
    db.execute("INSERT INTO seeded VALUES ('gpu')")
    for d in range(days):
        for i in range(10):
            db.execute("INSERT INTO listing VALUES ('gpu', ?, ?, ?)", (f"h{d}-{i}", level(d) + 2 * i - 9, NOW - d * DAY - 3600 * (i + 1)))
    db.commit()


def history(db: sqlite3.Connection) -> list[tuple[float, float]]:
    return db.execute("SELECT listed_at, price FROM listing").fetchall()


W = {"name": "gpu", "query": "rx 6800", "min_price": 100, "exclude": ["for parts"]}


class DeliveredTest(unittest.TestCase):
    def test_adds_cheapest_shipping(self) -> None:
        it = item("a", 100, 15)
        it["shippingOptions"].append({"shippingCost": {"value": "9.50", "currency": "USD"}})
        self.assertEqual(watch.delivered(it), (109.5, 9.5))

    def test_unquoted_shipping_is_none(self) -> None:
        self.assertIsNone(watch.delivered(item("a", 100, None)))

    def test_unreadable_listing_date_falls_back(self) -> None:
        self.assertEqual(watch.listed_at({"itemCreationDate": "last Tuesday"}, 5.0), 5.0)
        self.assertEqual(watch.listed_at(item("a", 100, listed=NOW), 5.0), NOW)


class MarketTest(unittest.TestCase):
    def test_junk_listings_barely_move_the_spread(self) -> None:
        sample = list(range(100, 121))
        self.assertLess(watch.spread(sample + [1, 9999]) - watch.spread(sample), 2)

    def test_none_until_enough_days(self) -> None:
        h = [(NOW - d * DAY - 60, 300.0) for d in range(6)]
        self.assertIsNone(watch.market(h, NOW, 7))
        self.assertIsNotNone(watch.market(h + [(NOW - 6 * DAY - 60, 300.0)], NOW, 7))

    def test_reads_a_falling_market_without_inflating_the_spread(self) -> None:
        db = new_db()
        seed(db, lambda d: 200 + 10 * d)  # $10/day cheaper, $200 today
        m = watch.market(history(db), NOW, 7)
        self.assertAlmostEqual(m["per_week"], -70, delta=1)
        self.assertAlmostEqual(m["expected"], 200, delta=6)
        self.assertGreater(m["level"], 250)
        # Scatter around the line is the ±$9 noise, not the $130 trend
        self.assertLess(m["spread"], 12)

    def test_identical_prices_keep_a_floor_spread(self) -> None:
        db = new_db()
        seed(db, lambda d: 300)
        db.execute("UPDATE listing SET price = 300")
        self.assertEqual(watch.market(history(db), NOW, 7)["spread"], 6)


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = new_db()

    def deals(self, items: list[dict], **overrides: object) -> list[dict]:
        return watch.check(self.db, {**W, **overrides}, items, NOW)[0]

    def test_first_poll_seeds_history_from_listing_dates(self) -> None:
        already_up = [item(f"b{d}-{i}", 300 + i, listed=NOW - d * DAY - 60) for d in range(10) for i in range(5)]
        deals, baseline = watch.check(self.db, W, already_up + [item("cheap-but-old", 200, listed=NOW - 60)], NOW)
        self.assertEqual(deals, [])
        self.assertEqual(baseline["days_to_go"], 0)
        self.assertEqual(len(self.deals([item("new", 260)])), 1)

    def test_short_seed_says_how_long_until_alerts(self) -> None:
        _, baseline = watch.check(self.db, W, [item(f"b{i}", 300 + i, listed=NOW - 60) for i in range(30)], NOW)
        self.assertEqual(baseline["days_to_go"], 6)
        self.assertEqual(self.deals([item("new", 150)]), [])

    def test_flat_market_cheap_listing_alerts(self) -> None:
        seed(self.db, lambda d: 300)
        deals = self.deals([item("new", 260, 10)])
        self.assertEqual([d["price"] for d in deals], [270])
        self.assertLess(deals[0]["z"], -2.5)

    def test_flat_market_ordinary_listing_is_quiet(self) -> None:
        seed(self.db, lambda d: 300)
        self.assertEqual(self.deals([item("new", 290)]), [])

    def test_falling_market_on_trend_listing_is_quiet(self) -> None:
        # The case this design exists for: $195 is far under the two-week
        # median, but it is about where the falling trend puts today.
        seed(self.db, lambda d: 200 + 10 * d)
        m = watch.market(history(self.db), NOW, 7)
        self.assertLess((195 - m["level"]) / m["spread"], -2.5)
        self.assertEqual(self.deals([item("new", 195)]), [])

    def test_falling_market_listing_under_the_trend_alerts(self) -> None:
        seed(self.db, lambda d: 200 + 10 * d)
        self.assertEqual(len(self.deals([item("new", 165)])), 1)

    def test_rising_market_lagging_listing_is_quiet(self) -> None:
        # $270 is under where the climb puts today ($300), but above the median.
        seed(self.db, lambda d: 300 - 10 * d)
        self.assertEqual(self.deals([item("new", 270)]), [])

    def test_rising_market_listing_under_the_median_alerts(self) -> None:
        seed(self.db, lambda d: 300 - 10 * d)
        self.assertEqual(len(self.deals([item("new", 200)])), 1)

    def test_no_alerts_while_history_is_short(self) -> None:
        seed(self.db, lambda d: 300, days=5)
        self.assertEqual(self.deals([item("new", 150)]), [])

    def test_seen_listing_does_not_alert_again(self) -> None:
        seed(self.db, lambda d: 300)
        self.assertEqual(len(self.deals([item("new", 250)])), 1)
        self.assertEqual(self.deals([item("new", 250)]), [])

    def test_ignored_listings_are_not_recorded(self) -> None:
        seed(self.db, lambda d: 300)
        group = item("group", 250)
        group["itemGroupHref"] = "https://api.ebay.com/buy/browse/v1/item/get_items_by_item_group?item_group_id=1"
        ignored = [item("parts", 250, title="RX 6800 FOR PARTS"), item("box", 20), item("pickup", 250, None), group]
        self.assertEqual(self.deals(ignored), [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM listing WHERE item_id IN ('parts', 'box', 'pickup', 'group')").fetchone()[0], 0)

    def test_new_and_poorly_rated_sellers_are_ignored(self) -> None:
        seed(self.db, lambda d: 300)
        fresh, shady, good = item("fresh", 250), item("shady", 250), item("good", 250)
        fresh["seller"] = {"feedbackScore": 2, "feedbackPercentage": "100.0"}
        shady["seller"] = {"feedbackScore": 900, "feedbackPercentage": "91.2"}
        good["seller"] = {"feedbackScore": 900, "feedbackPercentage": "99.6"}
        deals = self.deals([fresh, shady, good], min_seller_feedback=10, min_seller_percent=97)
        self.assertEqual([(d["url"], d["seller"]) for d in deals], [("https://www.ebay.com/itm/good", "seller 900 (99.6%)")])


class PushTest(unittest.TestCase):
    def test_caps_pushes_and_summarises_the_rest(self) -> None:
        deals = [{"title": "t", "price": float(p), "shipping": 0.0, "condition": "Used", "seller": "seller 500 (100.0%)", "url": "u", "image": None, "level": 300.0, "expected": 290.0, "per_week": -7.0, "z": -3.0} for p in range(200, 208)]
        with mock.patch.object(watch, "push") as push:
            watch.push_deals({"name": "gpu", "query": "rx 6800"}, deals)
        titles = [c.args[0] for c in push.call_args_list]
        self.assertEqual(titles[:2], ["gpu: $200.00 delivered", "gpu: $201.00 delivered"])
        self.assertEqual(titles[-1], "gpu: 3 more deals")
        self.assertEqual(len(titles), 6)
        self.assertIn("trend (-7/week) puts today at $290", push.call_args_list[0].args[1])

    def test_rejected_message_is_dropped_but_outage_raises(self) -> None:
        env = {"NTFY_URL": "http://ntfy", "NTFY_TOKEN": "tk_x"}

        def refuse(code: int) -> urllib.error.HTTPError:
            return urllib.error.HTTPError("http://ntfy", code, "no", {}, io.BytesIO(b"{}"))

        with mock.patch.dict(watch.os.environ, env), mock.patch.object(watch.urllib.request, "urlopen", side_effect=refuse(400)):
            watch.push("t", "m")
        for code in (401, 403, 429, 503):
            with mock.patch.dict(watch.os.environ, env), mock.patch.object(watch.urllib.request, "urlopen", side_effect=refuse(code)), self.assertRaises(urllib.error.HTTPError):
                watch.push("t", "m")


class PollTest(unittest.TestCase):
    def test_new_watch_pages_back_until_the_window_is_covered(self) -> None:
        # 200 listings a page, pages a week apart: the third reaches 15 days
        # back, past the 14-day window, so paging stops there and its
        # listings don't count towards the history.
        pages = [[item(f"p{p}-{i}", 300, listed=NOW - (p * 7 + 1) * DAY + i) for i in range(200)] for p in range(4)]
        with mock.patch.object(watch.time, "time", return_value=NOW), mock.patch.object(watch, "search", side_effect=pages) as search, mock.patch.object(watch, "push") as push:
            self.assertEqual(watch.poll(new_db(), {"zip": "10001", "watch": [W]}), [])
        self.assertEqual([c.args[2] for c in search.call_args_list[1:]], [200, 400])
        self.assertIn("400 listings", push.call_args.args[1])

    def test_seeding_happens_once_even_if_nothing_was_recorded_or_announced(self) -> None:
        # Every listing filtered out, and the "Watching" push fails: the
        # second poll must still be one API call, not another 5-page seed.
        page = [item(f"x{i}", 300, title="RX 6800 for parts", listed=NOW - 60) for i in range(200)]
        db = new_db()
        with mock.patch.object(watch.time, "time", return_value=NOW), mock.patch.object(watch, "search", side_effect=lambda *a: list(page)) as search, mock.patch.object(watch, "push", side_effect=OSError("ntfy down")):
            watch.poll(db, {"zip": "10001", "watch": [W]})
            self.assertEqual(search.call_count, 5)
            watch.poll(db, {"zip": "10001", "watch": [W]})
            self.assertEqual(search.call_count, 6)

    def test_failed_push_leaves_the_deal_for_next_poll(self) -> None:
        db = new_db()
        seed(db, lambda d: 300)
        config = {"zip": "10001", "watch": [W]}
        with mock.patch.object(watch.time, "time", return_value=NOW), mock.patch.object(watch, "search", return_value=[item("new", 250)]), mock.patch.object(watch, "push") as push:
            push.side_effect = OSError("ntfy down")
            self.assertEqual(len(watch.poll(db, config)), 1)
            push.side_effect = None
            self.assertEqual(watch.poll(db, config), [])
            self.assertEqual(watch.poll(db, config), [])
        self.assertEqual([c.args[0] for c in push.call_args_list], ["gpu: $250.00 delivered", "gpu: $250.00 delivered"])
