"""Poll eBay for new listings and push the good deals to ntfy.

A new listing is a deal when its delivered price (item + cheapest shipping to
the configured ZIP) is well under both the recent median and where the recent
price trend puts today's price. The trend is what keeps a falling market from
making every new listing look like a bargain.
Standard library only, so it runs on a stock python image with nothing to build.
"""

import base64
import bisect
import collections
import contextlib
import http.server
import json
import logging
import math
import os
import re
import sqlite3
import statistics
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import dashboard

EBAY = "https://api.ebay.com"
# Alert once every poll has failed for this long, so an eBay hiccup doesn't
# page the phone. Time rather than a poll count, so the interval can change.
FAIL_ALERT_AFTER = 3600
# More deals than this in one poll for one watch gets the cheapest few plus a
# summary, instead of a wall of pushes.
MAX_PUSHES = 5
DAY = 86400
# A new watch's first poll pages back through this many listings to seed its
# history, so it can alert from day one instead of after a week of watching.
SEED_LIMIT = 1000
# listed_at is eBay's listing date. A new watch's history is seeded from
# listings that are still up, and the older ones still up are the ones that
# didn't sell, so they skew high. That reads as a falling market, which lowers
# the trend's price for today and makes the trend test stricter: the bias
# costs missed deals, not false alarms, and ages out of the window.
#
# seeded marks a watch as past its first poll. It is separate from listing so a
# watch whose first poll recorded nothing (all filtered out) isn't treated as
# new again, re-seeding at 5 API calls a minute.
#
# title, url, shipping and seller are only for the dashboard. alerted marks
# the listings that were pushed. seen_at is when the watcher first saw the
# listing; NULL for listings recorded before it existed.
SCHEMA = """
CREATE TABLE IF NOT EXISTS listing (
    watch TEXT, item_id TEXT, price REAL, listed_at REAL,
    title TEXT, url TEXT, shipping REAL, seller TEXT, alerted INTEGER NOT NULL DEFAULT 0, seen_at REAL,
    PRIMARY KEY (watch, item_id)
);
CREATE TABLE IF NOT EXISTS seeded (watch TEXT PRIMARY KEY);
"""
DASHBOARD_PORT = 8091

QUOTE_ATTEMPTS = 3
# A listing first seen within this long of going up was seen live; search can
# lag a listing's creation date by an hour or two.
LIVE = 6 * 3600
# The odds of a cheaper one need this many days of live listings. The
# half-listing prior in outlook() is spread over the days watched, so before
# this it outweighs the data and every alert reads "likely beaten this week".
MIN_WATCHED = 7

log = logging.getLogger("ebay-watch")
_token = {"value": "", "expires": 0.0}
# Failed shipping lookups per listing. In memory: a restart just allows a few
# more retries.
_quote_failures: collections.Counter = collections.Counter()
# Last poll's time and errors, for the dashboard.
_status: dict = {"last_poll": None, "errors": []}
# When each watch was last checked. In memory: a restart checks them all once.
_last_polled: dict[str, float] = {}
# How often the main loop wakes to see which watches are due.
TICK = 30
# Watches whose checks are failing: name -> since, last error, alerted yet.
_failing: dict[str, dict] = {}


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set (see .env.example)")
    return value


def ebay_token() -> str:
    """Application token from the client-credentials grant, cached until near expiry."""
    if time.time() < _token["expires"] - 60:
        return _token["value"]
    creds = base64.b64encode(f"{env('EBAY_CLIENT_ID')}:{env('EBAY_CLIENT_SECRET')}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"}).encode()
    req = urllib.request.Request(f"{EBAY}/identity/v1/oauth2/token", body, {"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    _token.update(value=data["access_token"], expires=time.time() + data["expires_in"])
    return _token["value"]


def search(watch: dict, offset: int = 0) -> list[dict]:
    """200 listings matching the watch, newest first, skipping `offset`."""
    filters = ["buyingOptions:{" + "|".join(watch.get("buying_options", ["FIXED_PRICE"])) + "}", "deliveryCountry:US"]
    # Delivered price leaves out import duties, so an overseas listing is
    # dearer than it looks; for these cards that was ~40% of listings, mostly
    # China and Japan, and they would be the likeliest false deals.
    if watch.get("item_location", "US"):
        filters.append(f"itemLocationCountry:{watch.get('item_location', 'US')}")
    if "condition_ids" in watch:
        filters.append("conditionIds:{" + "|".join(map(str, watch["condition_ids"])) + "}")
    # No price filter on purpose: the statistics need the whole distribution.
    params = {"q": watch["query"], "sort": "newlyListed", "limit": 200, "offset": offset, "filter": ",".join(filters)}
    if "category_ids" in watch:
        params["category_ids"] = ",".join(map(str, watch["category_ids"]))
    req = urllib.request.Request(f"{EBAY}/buy/browse/v1/item_summary/search?{urllib.parse.urlencode(params)}", headers=ebay_headers())
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp).get("itemSummaries", [])


def shipping_quote(item_id: str) -> float | None:
    """Cheapest shipping to $ZIP from the single-listing lookup, which,
    unlike search, prices calculated shipping. None if eBay quotes none."""
    req = urllib.request.Request(f"{EBAY}/buy/browse/v1/item/{urllib.parse.quote(item_id)}", headers=ebay_headers())
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            options = json.load(resp).get("shippingOptions", [])
    except urllib.error.HTTPError as e:
        if e.code == 404:  # ended or pulled since the search
            return None
        raise
    costs = [float(o["shippingCost"]["value"]) for o in options if "shippingCost" in o]
    return min(costs) if costs else None


def ebay_headers() -> dict:
    return {
        "Authorization": f"Bearer {ebay_token()}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        # Shipping is quoted to $ZIP. Search still leaves calculated
        # shipping unpriced; the single-listing lookup prices it.
        "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country%3DUS%2Czip%3D{env('ZIP')}",
    }


def delivered(item: dict) -> tuple[float, float] | None:
    """(item price + cheapest shipping, shipping), or None when the search
    result carries no shipping price: calculated shipping, pickup, freight."""
    costs = [float(o["shippingCost"]["value"]) for o in item.get("shippingOptions", []) if "shippingCost" in o]
    if not costs:
        return None
    return float(item["price"]["value"]) + min(costs), min(costs)


def listed_at(item: dict, default: float) -> float:
    # A malformed date falls back like a missing one rather than raising,
    # which would fail the whole poll every minute until the listing aged out.
    try:
        return datetime.fromisoformat(item["itemCreationDate"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return default


def market(history: list[tuple[float, float]], now: float, min_listings: int, min_days: int) -> dict | None:
    """Where asking prices sit and where they are heading, or None until the
    history has min_listings listings across min_days separate days.

    history is (listed_at, delivered price). The trend is a Theil-Sen line: the
    median slope over every pair of listings, which shrugs off up to ~29% odd
    listings where least squares would follow them. It is fitted to listings
    rather than daily medians because these cards list about once a day.

    spread is how far below that line listings typically sit, scaled to match a
    standard deviation. Measured around the line, so a steep trend doesn't
    widen it, and on the cheap side only: overpriced listings (imports at twice
    the going rate) linger unsold and would otherwise widen it until the alert
    bar sat below anything that ever lists.
    """
    if len(history) < min_listings or len({int((now - t) // DAY) for t, _ in history}) < min_days:
        return None
    # x is in days relative to now, so the line's intercept is today's expected price.
    points = [((t - now) / DAY, p) for t, p in history]
    slope = statistics.median((y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(points) for x2, y2 in points[i + 1:] if x2 != x1)
    expected = statistics.median(y - slope * x for x, y in points)
    level = statistics.median(p for _, p in history)
    residuals = [y - (expected + slope * x) for x, y in points]
    center = statistics.median(residuals)
    # <= so listings right at the going rate count as zero distance: a seller
    # with a dozen units at one price is a tight market, not an absent one.
    below = [center - r for r in residuals if r <= center]
    # Floored at 2% of the price: when most listings share one price (a
    # refurbisher's stock), the scatter is ~0 and any small undercut would
    # look like a huge outlier.
    cheap_side = 1.4826 * statistics.median(below) if below else 0.0
    return {"level": level, "expected": expected, "slope": slope, "spread": max(cheap_side, 0.02 * level), "per_week": slope * 7}


def bar(m: dict, max_z: float, days_from_now: float = 0.0) -> float:
    """The delivered price at or under which a listing alerts: max_z spreads
    under the lower of the median and the trend. In a falling market that's
    the trend, so riding the slope down isn't a deal; in a rising one it's the
    median, so lagging the climb isn't either."""
    return min(m["level"], m["expected"] + m["slope"] * days_from_now) + max_z * m["spread"]


def outlook(live: list[tuple[float, float]], m: dict, price: float, now: float, since: float, margin: float) -> dict | None:
    """Odds that a listing at least `margin` (a fraction) cheaper than `price`
    turns up soon, from how often such ones turned up since `since`, when the
    watch started. A margin rather than any cheaper dollar, so a few dollars'
    difference isn't a regret whatever the item costs. None until it has
    watched for MIN_WATCHED days.

    live is (listed_at, delivered price) of the listings seen within LIVE of
    going up. The rest were found days later (a new watch's seed, a widened
    search), and those are the leftovers that hadn't sold: the cheap ones are
    missing, so counting them would understate the odds.

    A past listing counts as cheaper for a future day when it sat further under
    its day's going rate (the lower of median and trend, as in bar()) than
    `price` sits under that future day's. So in a falling market the same
    discount gets cheaper in dollars by the day and the odds rise with the
    horizon. Arrivals are treated as a Poisson process, with half a listing
    added to every count so "none so far" reads as rare, not never. The trend
    is extrapolated at most a month ahead.
    """
    span = (now - since) / DAY
    if span < MIN_WATCHED:
        return None

    def going(x: float) -> float:
        return min(m["level"], m["expected"] + m["slope"] * x)

    below = sorted(p - going((t - now) / DAY) for t, p in live if t >= since)
    target = price * (1 - margin)
    step, cumulative, expected = 0.25, [0.0], 0.0
    while len(cumulative) <= 30 / step:
        x = (len(cumulative) - 0.5) * step
        expected += (bisect.bisect_left(below, target - going(x)) + 0.5) / span * step
        cumulative.append(expected)
    even = next((i * step for i, c in enumerate(cumulative) if c >= math.log(2)), None)
    return {"cheaper": bisect.bisect_left(below, target - going(0)), "of": len(below), "span": span, "day": 1 - math.exp(-cumulative[4]), "week": 1 - math.exp(-cumulative[28]), "even_odds_days": even}


def readiness(watch: dict) -> tuple[int, int]:
    """(min_listings, min_days) before a watch alerts. A trend needs at least
    two days to have a slope."""
    return watch.get("min_listings", 15), max(2, watch.get("min_days", 7))


def check(db: sqlite3.Connection, watch: dict, items: list[dict], now: float) -> tuple[list[dict], dict | None]:
    """Record this poll's listings, uncommitted, and return (deals among the new ones, baseline).

    baseline is set only on a watch's first poll, which seeds the history with
    what is already listed and alerts on nothing, since every listing would
    otherwise look new.
    """
    name = watch["name"]
    first_poll = db.execute("INSERT OR IGNORE INTO seeded VALUES (?)", (name,)).rowcount == 1
    cutoff = now - watch.get("history_days", 30) * DAY
    min_listings, min_days = readiness(watch)
    rows = db.execute("SELECT listed_at, price, seen_at FROM listing WHERE watch = ? AND listed_at >= ? AND price IS NOT NULL", (name, cutoff)).fetchall()
    history = [(t, p) for t, p, _ in rows]
    m = market(history, now, min_listings, min_days)
    live = [(t, p) for t, p, seen in rows if seen is None or seen - t <= LIVE]
    # The watch's first poll. Listings from before seen_at existed stand in
    # with their listing date, which for a seed reaches back past the window.
    since = max(db.execute("SELECT min(coalesce(seen_at, listed_at)) FROM listing WHERE watch = ?", (name,)).fetchone()[0] or cutoff, cutoff)
    max_z = watch.get("max_z", -2.0)
    exclude = [w.lower() for w in watch.get("exclude", [])]

    deals = []
    for item in items:
        if item.get("price", {}).get("currency") != "USD":
            continue
        title = item.get("title", "")
        # eBay's keyword search is loose: "rx 6800" also returns RTX 5080s and
        # RX 9070s from the same category, which would wreck the statistics.
        if "match" in watch and not re.search(watch["match"], title, re.IGNORECASE):
            continue
        if any(w in title.lower() for w in exclude):
            continue
        # A multi-variation listing ("6700 XT / 6800 XT") can surface in a
        # search priced at whichever variation is cheapest, which is a false
        # deal for the model you searched.
        if "itemGroupHref" in item:
            continue
        # eBay's "For parts or not working" condition. Sellers often say so
        # only there, not in the title, so the exclude list misses them.
        if item.get("conditionId") == "7000":
            continue
        # No seller-feedback filter on purpose: plenty of people make an account,
        # or dust off an old one, just to sell their card.
        seller = item.get("seller", {})
        # On the item price, so junk is dropped before any shipping lookup.
        if float(item["price"]["value"]) < watch.get("min_price", 0):
            continue
        # Already seen: keep the price it was listed at.
        if db.execute("SELECT 1 FROM listing WHERE watch = ? AND item_id = ?", (name, item["itemId"])).fetchone():
            continue
        t = min(listed_at(item, now), now)
        d = delivered(item)
        # Search results never price calculated shipping, which most private
        # sellers use. Look those up one at a time: only new listings that
        # passed every filter above and fall inside the history window, so it
        # stays at a few API calls a day.
        if d is None and t >= cutoff and any(o.get("shippingCostType") == "CALCULATED" for o in item.get("shippingOptions", [])):
            try:
                cost = shipping_quote(item["itemId"])
            except Exception:
                # Never fail the poll over one lookup: the rollback would throw
                # away this poll's other lookups and redo them every minute,
                # burning the quota all watches share. Retry on the next
                # couple of polls, then record it unpriced and move on.
                _quote_failures[item["itemId"]] += 1
                log.warning("shipping lookup for %s failed (%d)", item["itemId"], _quote_failures[item["itemId"]], exc_info=True)
                if _quote_failures[item["itemId"]] < QUOTE_ATTEMPTS:
                    continue
                cost = None
            d = None if cost is None else (float(item["price"]["value"]) + cost, cost)
        # Recorded either way, so nothing is looked up twice. A NULL price (no
        # quote: pickup only, freight, ended) stays out of the statistics.
        seller_text = f"{seller.get('username', '?')} {seller.get('feedbackScore', '?')} ({seller.get('feedbackPercentage', '?')}%)"
        db.execute(
            "INSERT INTO listing (watch, item_id, price, listed_at, title, url, shipping, seller, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, item["itemId"], d and d[0], t, title, item.get("itemWebUrl"), d and d[1], seller_text, now),
        )
        if d is None or first_poll or m is None:
            continue
        price, shipping = d
        if price <= bar(m, max_z):
            z = (price - min(m["level"], m["expected"])) / m["spread"]
            deals.append({**m, "item_id": item["itemId"], "title": title, "price": price, "shipping": shipping, "z": z, "outlook": outlook(live, m, price, now, since, watch.get("cheaper_by", 3) / 100), "url": item["itemWebUrl"], "image": item.get("image", {}).get("imageUrl"), "condition": item.get("condition", ""), "seller": f"seller {seller.get('feedbackScore', '?')} ({seller.get('feedbackPercentage', '?')}%)"})
    baseline = None
    if first_poll:
        recorded = db.execute("SELECT listed_at, price FROM listing WHERE watch = ? AND listed_at >= ? AND price IS NOT NULL", (name, cutoff)).fetchall()
        baseline = {"count": len(recorded), "median": statistics.median(p for _, p in recorded) if recorded else None, "ready": market(recorded, now, min_listings, min_days) is not None, "needs": f"{min_listings} listings across {min_days} days"}
    return sorted(deals, key=lambda d: d["price"]), baseline


def push(title: str, message: str, click: str | None = None, image: str | None = None, priority: int = 3, tags: list[str] | None = None) -> None:
    # JSON publishing rather than headers: listing titles are often not
    # latin-1, which HTTP headers can't carry.
    body = {"topic": os.environ.get("NTFY_TOPIC", "ebay"), "title": title, "message": message, "priority": priority, "tags": tags or []}
    if click:
        body["click"] = click
    if image:
        body["attach"] = image
    req = urllib.request.Request(env("NTFY_URL"), json.dumps(body).encode(), {"Authorization": f"Bearer {env('NTFY_TOKEN')}", "Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=30).close()
    except urllib.error.HTTPError as e:
        # ntfy refusing this one message as malformed or too big will refuse
        # it every time. Raising would roll the poll back and retry it
        # forever, stalling the watch, so drop it instead. Anything else
        # (auth, permissions, outage) raises, so the poll fails and is retried
        # and the failure alert can fire.
        if e.code in (400, 413):
            log.error("ntfy rejected %r: %s %s", title, e.code, e.read()[:200])
            return
        raise


def push_deals(watch: dict, deals: list[dict]) -> None:
    for d in deals[:MAX_PUSHES]:
        ship = "free shipping" if d["shipping"] == 0 else f"${d['price'] - d['shipping']:.2f} + ${d['shipping']:.2f} shipping"
        lines = [
            d["title"],
            " · ".join(x for x in (ship, d["condition"], d["seller"]) if x),
            f"Recent median ${d['level']:.0f}; trend ({d['per_week']:+.0f}/week) puts today at ${d['expected']:.0f}. z {d['z']:.1f} under the lower of the two.",
        ]
        # Every alert is a good price; the verdict says whether to expect a
        # better one, and goes first so it shows in a collapsed notification.
        # No odds for the next day: the backtest found them about twice too high.
        if o := d["outlook"]:
            by = f"{watch.get('cheaper_by', 3):g}%+ cheaper"
            even = o["even_odds_days"]
            if even is not None and even <= 7:
                verdict = f"Good price, but one {by} is likely soon: even odds by day {math.ceil(even)}, {o['week']:.0%} within a week."
            else:
                when = f"even odds by day {math.ceil(even)}" if even is not None else "under even odds within a month"
                verdict = f"Good price, and one {by} is unlikely soon: {o['week']:.0%} within a week, {when}."
            lines.insert(0, f"{verdict} ({o['cheaper']} of {o['of']} listings seen in {o['span']:.0f} days were {by} for their day.)")
        push(f"{watch['name']}: ${d['price']:.2f} delivered", "\n".join(lines), click=d["url"], image=d["image"], priority=4, tags=["moneybag"])
    if len(deals) > MAX_PUSHES:
        rest = deals[MAX_PUSHES:]
        push(f"{watch['name']}: {len(rest)} more deals", f"From ${rest[0]['price']:.2f} to ${rest[-1]['price']:.2f} delivered.", click=f"https://www.ebay.com/sch/i.html?{urllib.parse.urlencode({'_nkw': watch['query'], '_sop': 10})}")


def poll(db: sqlite3.Connection, config: dict) -> list[str] | None:
    """Check every watch that is due. Returns the errors, one per failed
    watch, or None when no watch was due."""
    errors, polled = [], False
    now = time.time()
    for watch in config.get("watch", []):
        every = watch.get("interval_minutes", config.get("interval_minutes", 10)) * 60
        # A few seconds' slack so a watch due at the next tick isn't pushed a whole tick late.
        if now - _last_polled.get(watch["name"], 0) < every - 5:
            continue
        # Set before trying, so a failing watch is retried at its own pace
        # rather than every tick, which would eat the shared API quota.
        _last_polled[watch["name"]] = now
        polled = True
        try:
            items = search(watch)
            if db.execute("SELECT 1 FROM seeded WHERE watch = ?", (watch["name"],)).fetchone() is None:
                # New watch: page back through what's already listed to seed
                # its history. A full page means there may be more.
                since = time.time() - watch.get("history_days", 30) * DAY
                while items and len(items) < SEED_LIMIT and len(items) % 200 == 0 and listed_at(items[-1], 0) >= since:
                    items += search(watch, len(items))
            deals, baseline = check(db, watch, items, time.time())
            if baseline:
                # Commit the seed before announcing it. A first poll has no
                # deals to lose, and if a failed push rolled it back, the next
                # poll would page back through 1,000 listings again.
                db.commit()
                median = f", median ${baseline['median']:.2f} delivered" if baseline["median"] is not None else ""
                start = "Alerts are on." if baseline["ready"] else f"Alerts start once there are {baseline['needs']}, enough to tell a deal from a falling market."
                try:
                    push(f"Watching {watch['name']}", f"{baseline['count']} listings from the last {watch.get('history_days', 30)} days recorded{median}. {start}", tags=["eyes"])
                except Exception:
                    log.exception("could not announce %s", watch["name"])
            push_deals(watch, deals)
            db.executemany("UPDATE listing SET alerted = 1 WHERE watch = ? AND item_id = ?", [(watch["name"], d["item_id"]) for d in deals])
            # Only now are this poll's listings "seen". Committing before the
            # pushes would lose a deal for good whenever ntfy is unreachable.
            db.commit()
            _failing.pop(watch["name"], None)
            log.info("%s: %d listings fetched, %d deals", watch["name"], len(items), len(deals))
        except Exception as e:
            # Undo this watch's half-recorded poll so its listings are retried
            # next time (at worst a repeat push) and the next watch's commit
            # doesn't save them by accident.
            db.rollback()
            log.exception("%s failed", watch.get("name"))
            errors.append(f"{watch.get('name')}: {e}")
            _failing.setdefault(watch["name"], {"since": now, "alerted": False})["error"] = str(e)
    return errors if polled else None


def alert_failures(now: float) -> None:
    """Push once for each watch that has failed every check for an hour.
    Tracked per watch: with watches on different intervals, a healthy
    watch's success must not reset a broken one's clock."""
    due = [(name, f) for name, f in _failing.items() if not f["alerted"] and now - f["since"] >= FAIL_ALERT_AFTER]
    if not due:
        return
    try:
        push("eBay watcher is failing", "\n".join(f"{name}, for {(now - f['since']) / 60:.0f} min: {f['error']}" for name, f in due), priority=4, tags=["warning"])
    except Exception:
        log.exception("could not push the failure alert")
        return
    for _, f in due:
        f["alerted"] = True


def dashboard_data(db: sqlite3.Connection, config: dict, now: float) -> list[dict]:
    """Per watch: its market, alert bar and priced listings in the window,
    newest first, in the plain numbers dashboard.render() draws."""
    out = []
    for watch in config.get("watch", []):
        days = watch.get("history_days", 30)
        max_z = watch.get("max_z", -2.0)
        rows = db.execute(
            "SELECT listed_at, price, shipping, title, url, seller, alerted FROM listing"
            " WHERE watch = ? AND listed_at >= ? AND price IS NOT NULL ORDER BY listed_at DESC",
            (watch["name"], now - days * DAY),
        ).fetchall()
        m = market([(r[0], r[1]) for r in rows], now, *readiness(watch))
        listings = []
        for t, price, shipping, title, url, seller, alerted in rows:
            x = (t - now) / DAY
            z = (price - min(m["level"], m["expected"] + m["slope"] * x)) / m["spread"] if m else None
            listings.append({"days_ago": -x, "price": price, "shipping": shipping, "title": title, "url": url, "seller": seller, "alerted": bool(alerted), "z": z})
        out.append({
            "name": watch["name"], "query": watch["query"], "days": days, "max_z": max_z, "needs": readiness(watch), "market": m, "listings": listings,
            # Lines as (days ago, price) points; the bar kinks where the trend crosses the median.
            "bar_line": [(-x, bar(m, max_z, x)) for x in range(-days, 1)] if m else [],
            "trend_line": [(days, m["expected"] - m["slope"] * days), (0, m["expected"])] if m else [],
        })
    return out


def serve_dashboard(db_path: str, config_path: Path) -> None:
    """Serve the dashboard from a background thread, reading the database
    read-only so a page view can never interfere with a poll."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            if url.path != "/":
                self.send_error(404)
                return
            card = urllib.parse.parse_qs(url.query).get("card", [None])[0]
            try:
                with contextlib.closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as db:
                    now = time.time()
                    body = dashboard.render(dashboard_data(db, tomllib.loads(config_path.read_text()), now), _status, now, card).encode()
            except Exception:
                log.exception("dashboard failed")
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass  # a line per page view would drown the poll log

    server = http.server.ThreadingHTTPServer(("", DASHBOARD_PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_path = Path(os.environ.get("WATCHES", "/app/watches.toml"))
    db_path = os.environ.get("DB", "/data/listings.db")
    db = sqlite3.connect(db_path)
    # WAL so a dashboard read never blocks the poll's commit. A commit that
    # timed out after its pushes went out would roll back and push them again.
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    # Databases from before seen_at existed.
    with contextlib.suppress(sqlite3.OperationalError):
        db.execute("ALTER TABLE listing ADD COLUMN seen_at REAL")
    serve_dashboard(db_path, config_path)
    while True:
        started = time.time()
        try:
            # Re-read every tick so editing watches.toml needs no restart.
            config = tomllib.loads(config_path.read_text())
            # Forget failures of watches no longer configured, and of the
            # config itself now that it reads.
            for name in set(_failing) - {w["name"] for w in config.get("watch", [])}:
                del _failing[name]
            polled = poll(db, config) is not None
        except Exception as e:
            # Unreadable watches.toml: tracked like a failing watch so it alerts too.
            log.exception("could not read watches.toml")
            _failing.setdefault("watches.toml", {"since": started, "alerted": False})["error"] = str(e)
            polled = True
        if polled:
            _status.update(last_poll=time.time(), errors=[f"{name}: {f['error']}" for name, f in _failing.items()])
            alert_failures(time.time())
        # Less the check's own duration, so watches keep their intervals.
        time.sleep(max(1.0, TICK - (time.time() - started)))


if __name__ == "__main__":
    main()
