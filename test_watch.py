"""Run with: uv run --no-project python -m unittest test_watch.py"""

import io
import re
import sqlite3
import sys
import tomllib
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import dashboard  # noqa: E402
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
            db.execute("INSERT INTO listing (watch, item_id, price, listed_at) VALUES ('gpu', ?, ?, ?)", (f"h{d}-{i}", level(d) + 2 * i - 9, NOW - d * DAY - 3600 * (i + 1)))
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


class SearchTest(unittest.TestCase):
    def test_only_us_located_listings_by_default(self) -> None:
        def filters(w: dict) -> str:
            with mock.patch.object(watch, "ebay_token", return_value="t"), mock.patch.object(watch.urllib.request, "urlopen") as urlopen:
                urlopen.return_value.__enter__.return_value = io.BytesIO(b"{}")
                watch.search(w, "10001")
            return watch.urllib.parse.parse_qs(watch.urllib.parse.urlsplit(urlopen.call_args.args[0].full_url).query)["filter"][0]

        self.assertIn("itemLocationCountry:US", filters(W))
        self.assertNotIn("itemLocationCountry", filters({**W, "item_location": ""}))


class MarketTest(unittest.TestCase):
    def test_overpriced_listings_do_not_widen_the_spread(self) -> None:
        # Imports listed at two to three times the going rate linger unsold;
        # they must not push the alert bar down.
        db = new_db()
        seed(db, lambda d: 300)
        before = watch.market(history(db), NOW, 15, 7)
        for i in range(20):
            db.execute("INSERT INTO listing (watch, item_id, price, listed_at) VALUES ('gpu', ?, ?, ?)", (f"import{i}", 600 + 15 * i, NOW - i * DAY - 99))
        after = watch.market(history(db), NOW, 15, 7)
        self.assertAlmostEqual(after["spread"], before["spread"], delta=1.5)

    def test_none_until_enough_listings_and_days(self) -> None:
        def h(n: int, days: int) -> list[tuple[float, float]]:
            return [(NOW - (i % days) * DAY - 60 - i, 300.0 + i) for i in range(n)]

        self.assertIsNone(watch.market(h(14, 7), NOW, 15, 7))
        self.assertIsNone(watch.market(h(15, 6), NOW, 15, 7))
        self.assertIsNotNone(watch.market(h(15, 7), NOW, 15, 7))

    def test_reads_a_falling_market_without_inflating_the_spread(self) -> None:
        db = new_db()
        seed(db, lambda d: 200 + 10 * d)  # $10/day cheaper, $200 today
        m = watch.market(history(db), NOW, 15, 7)
        self.assertAlmostEqual(m["per_week"], -70, delta=1)
        self.assertAlmostEqual(m["expected"], 200, delta=6)
        self.assertGreater(m["level"], 250)
        # Scatter around the line is the ±$9 noise, not the $130 trend
        self.assertLess(m["spread"], 12)

    def test_many_units_at_one_price_keep_the_spread_tight(self) -> None:
        # The reviewer's case: 14 listings at $300 and 6 cheaper ones. Measured
        # only strictly below the going rate, the bar fell to $226.
        h = [(NOW - (i % 7) * DAY - 60 - i, 300.0) for i in range(14)] + [(NOW - (i % 7) * DAY - 99 - i, p) for i, p in enumerate([250, 260, 270, 280, 285, 290])]
        self.assertGreater(watch.bar(watch.market(h, NOW, 15, 7), -2.0), 270)

    def test_identical_prices_keep_a_floor_spread(self) -> None:
        db = new_db()
        seed(db, lambda d: 300)
        db.execute("UPDATE listing SET price = 300")
        self.assertEqual(watch.market(history(db), NOW, 15, 7)["spread"], 6)


class CheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.db = new_db()

    def deals(self, items: list[dict], **overrides: object) -> list[dict]:
        return watch.check(self.db, {**W, **overrides}, items, NOW)[0]

    def test_first_poll_seeds_history_from_listing_dates(self) -> None:
        already_up = [item(f"b{d}-{i}", 300 + i, listed=NOW - d * DAY - 60) for d in range(10) for i in range(5)]
        deals, baseline = watch.check(self.db, W, already_up + [item("cheap-but-old", 200, listed=NOW - 60)], NOW)
        self.assertEqual(deals, [])
        self.assertTrue(baseline["ready"])
        self.assertEqual(len(self.deals([item("new", 260)])), 1)

    def test_short_seed_says_how_long_until_alerts(self) -> None:
        _, baseline = watch.check(self.db, W, [item(f"b{i}", 300 + i, listed=NOW - 60) for i in range(30)], NOW)
        self.assertFalse(baseline["ready"])
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
        m = watch.market(history(self.db), NOW, 15, 7)
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
        ignored = [item("parts", 250, title="RX 6800 FOR PARTS"), item("box", 20), group]
        self.assertEqual(self.deals(ignored), [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM listing WHERE item_id IN ('parts', 'box', 'group')").fetchone()[0], 0)

    def test_calculated_shipping_is_looked_up_once(self) -> None:
        seed(self.db, lambda d: 300)
        calc = item("calc", 240, None)
        calc["shippingOptions"] = [{"shippingCostType": "CALCULATED"}]
        with mock.patch.object(watch, "shipping_quote", return_value=20.0) as quote:
            deals = watch.check(self.db, W, [calc], NOW, "10001")[0]
            self.assertEqual(watch.check(self.db, W, [calc], NOW, "10001")[0], [])
        quote.assert_called_once_with("calc", "10001")
        self.assertEqual([(d["price"], d["shipping"]) for d in deals], [(260, 20)])

    def test_failing_lookup_is_retried_twice_then_given_up_without_failing_the_poll(self) -> None:
        seed(self.db, lambda d: 300)
        calc = item("calc", 240, None)
        calc["shippingOptions"] = [{"shippingCostType": "CALCULATED"}]
        recorded = "SELECT count(*) FROM listing WHERE item_id = 'calc'"
        with mock.patch.object(watch, "_quote_failures", watch.collections.Counter()), mock.patch.object(watch, "shipping_quote", side_effect=OSError("eBay 503")) as quote:
            for _ in range(2):
                self.assertEqual(watch.check(self.db, W, [calc], NOW, "10001")[0], [])
                self.assertEqual(self.db.execute(recorded).fetchone()[0], 0)
            watch.check(self.db, W, [calc], NOW, "10001")
            watch.check(self.db, W, [calc], NOW, "10001")
        self.assertEqual(quote.call_count, 3)
        self.assertEqual(self.db.execute(recorded + " AND price IS NULL").fetchone()[0], 1)

    def test_unquotable_listing_is_remembered_but_kept_out_of_the_statistics(self) -> None:
        seed(self.db, lambda d: 300)
        pickup, ended, old = item("pickup", 250, None), item("ended", 250, None), item("old", 250, None, listed=NOW - 40 * DAY)
        for it in (ended, old):
            it["shippingOptions"] = [{"shippingCostType": "CALCULATED"}]
        with mock.patch.object(watch, "shipping_quote", return_value=None) as quote:
            self.assertEqual(watch.check(self.db, W, [pickup, ended, old], NOW, "10001")[0], [])
            watch.check(self.db, W, [pickup, ended, old], NOW, "10001")
        # Only the in-window calculated listing is looked up, and only once.
        quote.assert_called_once_with("ended", "10001")
        # All three recorded with no price; had a NULL reached the statistics,
        # the second check above would have crashed computing a median.
        self.assertEqual(self.db.execute("SELECT count(*) FROM listing WHERE item_id IN ('pickup', 'ended', 'old') AND price IS NULL").fetchone()[0], 3)

    def test_titles_must_match_the_model(self) -> None:
        # Real titles eBay returned for "rx 6800" in the graphics card category
        seed(self.db, lambda d: 300)
        titles = ["ASUS PRIME GeForce RTX 5080 OC Edition 16GB GDDR7 PCIe 5.0 GPU", "GIGABYTE Radeon RX 9070 XT Gaming OC 16G", "ASUS TUF-RX6800XT-016G-GAMING", "XFX Speedster MERC 319 Radeon RX 6800 XT 16GB", "ASRock Radeon RX 6800  Phantom Gaming 16GB GDDR6 Graphics Card"]
        deals = self.deals([item(f"t{i}", 250, title=t) for i, t in enumerate(titles)], match=r"6800(?!\s*-?xt)")
        self.assertEqual([d["title"] for d in deals], [titles[4]])

    def test_title_patterns_from_watches_toml(self) -> None:
        patterns = {w["name"]: w["match"] for w in tomllib.loads((Path(__file__).parent / "watches.toml").read_text())["watch"]}
        cases = {
            "RTX 4070 Ti Super": (["ASUS TUF RTX 4070 Ti SUPER 16GB", "Gigabyte 4070TI Super Gaming OC", "RTX 4070 Ti-Super", "MSI RTX 4070 16GB Ventus", "RTX 4070Ti 16G", "Gigabyte RTX 4070 Ti Gaming OC 16GB GDDR6X"], ["MSI RTX 4070 SUPER 12GB", "Zotac RTX 4070 Ti 12GB", "RTX 4070 12GB", "RTX 4060 Ti 16GB", "4070 SUPER - RYZEN 9 7945HX BD790i Minisforum 16GB DDR5, 512GB Storage"]),
            "RX 9070 XT": (["PowerColor Reaper Radeon RX 9070 XT 16GB", "XFX Quicksilver AMD RX 9070XT White"], ["ASUS PRIME Radeon RX 9070 16GB GDDR6"]),
            "RTX 5070 Ti": (["ASUS TUF RTX 5070 Ti 16GB", "MSI RTX 5070TI Ventus"], ["MSI RTX 5070 12GB", "Zotac RTX 5070 Twin Edge"]),
        }
        for name, (yes, no) in cases.items():
            for t in yes:
                self.assertIsNotNone(re.search(patterns[name], t, re.IGNORECASE), f"{name} should match {t!r}")
            for t in no:
                self.assertIsNone(re.search(patterns[name], t, re.IGNORECASE), f"{name} should not match {t!r}")

    def test_workstation_card_is_not_an_rx_6800(self) -> None:
        seed(self.db, lambda d: 300)
        titles = ["AMD Radeon Pro W6800 32GB GDDR6 Workstation Graphics Card", "MSI RADEON RX6800 GAMING X TRIO 16"]
        deals = self.deals([item(f"t{i}", 250, title=t) for i, t in enumerate(titles)], match=r"(?<!w)6800(?!\s*-?xt)")
        self.assertEqual([d["title"] for d in deals], [titles[1]])

    def test_for_parts_condition_is_ignored(self) -> None:
        seed(self.db, lambda d: 300)
        broken = item("broken", 150)
        broken["conditionId"] = "7000"
        self.assertEqual(self.deals([broken]), [])

    def test_new_sellers_still_alert(self) -> None:
        # People make an account, or revive an old one, just to sell a card.
        seed(self.db, lambda d: 300)
        fresh = item("fresh", 250)
        fresh["seller"] = {"username": "fraben-7365", "feedbackScore": 6, "feedbackPercentage": "100.0"}
        self.assertEqual([d["seller"] for d in self.deals([fresh])], ["seller 6 (100.0%)"])


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
    def setUp(self) -> None:
        # Each test starts with no watch checked yet. Tests that check twice
        # at the same frozen time clear it in between, standing in for the
        # watch's interval passing.
        patcher = mock.patch.dict(watch._last_polled, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_watch_is_checked_at_its_own_interval(self) -> None:
        clock = [NOW]
        config = {"zip": "10001", "interval_minutes": 10, "watch": [{**W, "name": "cpu", "interval_minutes": 1.5}, {**W, "name": "gpu", "interval_minutes": 15}, {**W, "name": "default"}]}
        db = new_db()
        checked = []
        with mock.patch.object(watch.time, "time", side_effect=lambda: clock[0]), mock.patch.object(watch, "search", side_effect=lambda w, *a: checked.append(w["name"]) or []), mock.patch.object(watch, "push"):
            for minute in (0, 0.5, 1, 1.5, 3, 10, 15):
                clock[0] = NOW + minute * 60
                result = watch.poll(db, config)
                if minute == 0.5:
                    self.assertIsNone(result)  # nothing due
        self.assertEqual(checked, ["cpu", "gpu", "default", "cpu", "cpu", "cpu", "default", "cpu", "gpu"])

    def test_new_watch_pages_back_until_the_window_is_covered(self) -> None:
        # 200 listings a page, pages ten days apart: the fourth reaches 31
        # days back, past the 30-day window, so paging stops there and its
        # listings don't count towards the history.
        pages = [[item(f"p{p}-{i}", 300, listed=NOW - (p * 10 + 1) * DAY + i) for i in range(200)] for p in range(5)]
        with mock.patch.object(watch.time, "time", return_value=NOW), mock.patch.object(watch, "search", side_effect=pages) as search, mock.patch.object(watch, "push") as push:
            self.assertEqual(watch.poll(new_db(), {"zip": "10001", "watch": [W]}), [])
        self.assertEqual([c.args[2] for c in search.call_args_list[1:]], [200, 400, 600])
        self.assertIn("600 listings", push.call_args.args[1])

    def test_one_failing_watch_alerts_even_while_others_succeed(self) -> None:
        clock = [NOW]
        config = {"zip": "10001", "watch": [{**W, "name": "cpu", "interval_minutes": 1.5}, {**W, "name": "gpu", "interval_minutes": 15}]}

        def search(w: dict, *a: object) -> list:
            if w["name"] == "cpu":
                raise OSError("HTTP Error 400")
            return []

        with mock.patch.dict(watch._failing, clear=True), mock.patch.object(watch.time, "time", side_effect=lambda: clock[0]), mock.patch.object(watch, "search", side_effect=search), mock.patch.object(watch, "push") as push:
            # Two hours of 30-second ticks; the healthy gpu watch succeeds in between.
            for tick in range(240):
                clock[0] = NOW + tick * 30
                if watch.poll(new_db(), config) is not None:
                    watch.alert_failures(clock[0])
        alerts = [c for c in push.call_args_list if c.args[0] == "eBay watcher is failing"]
        self.assertEqual(len(alerts), 1)  # once, not every tick after the hour
        self.assertIn("cpu, for 6", alerts[0].args[1])
        self.assertIn("HTTP Error 400", alerts[0].args[1])
        self.assertNotIn("gpu", alerts[0].args[1])

    def test_seeding_happens_once_even_if_nothing_was_recorded_or_announced(self) -> None:
        # Every listing filtered out, and the "Watching" push fails: the
        # second poll must still be one API call, not another 5-page seed.
        page = [item(f"x{i}", 300, title="RX 6800 for parts", listed=NOW - 60) for i in range(200)]
        db = new_db()
        with mock.patch.object(watch.time, "time", return_value=NOW), mock.patch.object(watch, "search", side_effect=lambda *a: list(page)) as search, mock.patch.object(watch, "push", side_effect=OSError("ntfy down")):
            watch.poll(db, {"zip": "10001", "watch": [W]})
            self.assertEqual(search.call_count, 5)
            watch._last_polled.clear()
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
            watch._last_polled.clear()
            self.assertEqual(watch.poll(db, config), [])
            watch._last_polled.clear()
            self.assertEqual(watch.poll(db, config), [])
        self.assertEqual([c.args[0] for c in push.call_args_list], ["gpu: $250.00 delivered", "gpu: $250.00 delivered"])
        self.assertEqual(db.execute("SELECT alerted, title, url FROM listing WHERE item_id = 'new'").fetchone(), (1, "Sapphire Pulse RX 6800 16GB", "https://www.ebay.com/itm/new"))


class DashboardTest(unittest.TestCase):
    def test_renders_a_ready_watch_with_its_bar_and_alerted_listing(self) -> None:
        db = new_db()
        seed(db, lambda d: 300)
        db.execute("INSERT INTO listing VALUES ('gpu', 'deal', 250, ?, '<b>RX 6800</b> steal', 'https://www.ebay.com/itm/deal', 0, 'bob 900 (99.9%)', 1)", (NOW - 3600,))
        data = watch.dashboard_data(db, {"watch": [W]}, NOW)
        self.assertEqual(len(data[0]["bar_line"]), 31)
        self.assertAlmostEqual(data[0]["bar_line"][-1][1], watch.bar(data[0]["market"], -2.0), places=6)
        page = dashboard.render(data, {"last_poll": NOW - 30, "errors": []}, NOW)
        self.assertIn("Alerts on", page)
        self.assertIn("Alerted</span>", page)
        self.assertIn("&lt;b&gt;RX 6800&lt;/b&gt; steal", page)
        self.assertNotIn("<b>RX 6800</b>", page)
        self.assertIn("Last checked 30 s ago", page)

    def test_renders_a_warming_up_watch_and_a_failing_poll(self) -> None:
        db = new_db()
        seed(db, lambda d: 300, days=3)
        data = watch.dashboard_data(db, {"watch": [W, {**W, "name": "empty"}]}, NOW)
        page = dashboard.render(data, {"last_poll": NOW - 120, "errors": ["gpu: HTTP Error 401"]}, NOW)
        # 30 listings is enough; only the days are short, so only they show.
        self.assertIn("Warming up: 3 of 7 days</span>", page)
        self.assertIn("failed: gpu: HTTP Error 401", page)
        self.assertIn("No priced listings yet.", dashboard.render(data, {"last_poll": None, "errors": []}, NOW, "empty"))

    def test_tabs_show_one_card_and_fall_back_to_the_first(self) -> None:
        db = new_db()
        seed(db, lambda d: 300)
        data = watch.dashboard_data(db, {"watch": [W, {**W, "name": "R&D card"}]}, NOW)
        status = {"last_poll": NOW, "errors": []}
        second = dashboard.render(data, status, NOW, "R&D card")
        self.assertIn("<h2>R&amp;D card</h2>", second)
        self.assertNotIn("<h2>gpu</h2>", second)
        self.assertIn('href="?card=R%26D+card"', second)
        self.assertIn('aria-current="page" href="?card=R%26D+card"', second)
        for card in (None, "no such card"):
            self.assertIn("<h2>gpu</h2>", dashboard.render(data, status, NOW, card))

    def test_tooltip_data_is_escaped(self) -> None:
        db = new_db()
        seed(db, lambda d: 300)
        db.execute("""INSERT INTO listing VALUES ('gpu', 'x', 250, ?, 'RX 6800 "mint" <img src=x onerror=alert(1)>', 'https://www.ebay.com/itm/x', 10, 'eve', 0)""", (NOW - 60,))
        page = dashboard.render(watch.dashboard_data(db, {"watch": [W]}, NOW), {"last_poll": NOW, "errors": []}, NOW)
        self.assertIn('data-title="RX 6800 &quot;mint&quot; &lt;img src=x onerror=alert(1)&gt;"', page)
        self.assertIn('data-ship="$240 + $10 shipping"', page)
        self.assertNotIn("<img", page)
