"""Poll eBay for new listings and push the good deals to ntfy.

A new listing is a deal when its delivered price (item + cheapest shipping to
the configured ZIP) is well under both the recent median and where the recent
price trend puts today's price. The trend is what keeps a falling market from
making every new listing look like a bargain.
Standard library only, so it runs on a stock python image with nothing to build.
"""

import base64
import json
import logging
import os
import sqlite3
import statistics
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

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
SCHEMA = """
CREATE TABLE IF NOT EXISTS listing (watch TEXT, item_id TEXT, price REAL, listed_at REAL, PRIMARY KEY (watch, item_id));
CREATE TABLE IF NOT EXISTS seeded (watch TEXT PRIMARY KEY);
"""

log = logging.getLogger("ebay-watch")
_token = {"value": "", "expires": 0.0}


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set (see ebay-watch/.env)")
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


def search(watch: dict, zip_code: str, offset: int = 0) -> list[dict]:
    """200 listings matching the watch, newest first, skipping `offset`."""
    filters = ["buyingOptions:{" + "|".join(watch.get("buying_options", ["FIXED_PRICE"])) + "}", "deliveryCountry:US"]
    if "condition_ids" in watch:
        filters.append("conditionIds:{" + "|".join(map(str, watch["condition_ids"])) + "}")
    # No price filter on purpose: the statistics need the whole distribution.
    params = {"q": watch["query"], "sort": "newlyListed", "limit": 200, "offset": offset, "filter": ",".join(filters)}
    if "category_ids" in watch:
        params["category_ids"] = ",".join(map(str, watch["category_ids"]))
    req = urllib.request.Request(
        f"{EBAY}/buy/browse/v1/item_summary/search?{urllib.parse.urlencode(params)}",
        headers={
            "Authorization": f"Bearer {ebay_token()}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            # Without a location eBay can't quote calculated shipping and
            # leaves it out, so delivered price would be unknown for those.
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation=country%3DUS%2Czip%3D{zip_code}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp).get("itemSummaries", [])


def delivered(item: dict) -> tuple[float, float] | None:
    """(item price + cheapest shipping, shipping), or None when eBay quoted no
    shipping (local pickup, freight), which leaves nothing comparable."""
    costs = [float(o["shippingCost"]["value"]) for o in item.get("shippingOptions", []) if "shippingCost" in o]
    if not costs:
        return None
    return float(item["price"]["value"]) + min(costs), min(costs)


def spread(xs: list[float]) -> float:
    """MAD scaled to match a standard deviation. Unlike stdev, a few $1 "box
    only" or $5000 bundle listings barely move it."""
    m = statistics.median(xs)
    return 1.4826 * statistics.median(abs(x - m) for x in xs)


def listed_at(item: dict, default: float) -> float:
    # A malformed date falls back like a missing one rather than raising,
    # which would fail the whole poll every minute until the listing aged out.
    try:
        return datetime.fromisoformat(item["itemCreationDate"]).timestamp()
    except (KeyError, TypeError, ValueError):
        return default


def market(history: list[tuple[float, float]], now: float, min_days: int) -> dict | None:
    """Where asking prices sit and where they are heading, or None until the
    history covers min_days separate days.

    history is (listed_at, delivered price). The trend is a Theil-Sen line
    through daily medians: the median of every pairwise slope, so one odd day
    can't tilt it the way it would a least-squares fit. spread is the scatter
    around that line, so a steep trend doesn't inflate it.
    """
    by_day: dict[int, list[float]] = {}
    for t, p in history:
        by_day.setdefault(int((now - t) // DAY), []).append(p)
    if len(by_day) < min_days:
        return None
    # x is in days relative to now, so the line's intercept is today's expected price.
    points = [(-(d + 0.5), statistics.median(ps)) for d, ps in by_day.items()]
    slope = statistics.median((y2 - y1) / (x2 - x1) for i, (x1, y1) in enumerate(points) for x2, y2 in points[i + 1:])
    expected = statistics.median(y - slope * x for x, y in points)
    level = statistics.median(p for _, p in history)
    residuals = [p - (expected + slope * (t - now) / DAY) for t, p in history]
    # Floored at 2% of the price: when most listings share one price (a
    # refurbisher's stock), the scatter is ~0 and any small undercut would
    # look like a huge outlier.
    return {"level": level, "expected": expected, "spread": max(spread(residuals), 0.02 * level), "per_week": slope * 7}


def check(db: sqlite3.Connection, watch: dict, items: list[dict], now: float) -> tuple[list[dict], dict | None]:
    """Record this poll's listings, uncommitted, and return (deals among the new ones, baseline).

    baseline is set only on a watch's first poll, which seeds the history with
    what is already listed and alerts on nothing, since every listing would
    otherwise look new.
    """
    name = watch["name"]
    first_poll = db.execute("INSERT OR IGNORE INTO seeded VALUES (?)", (name,)).rowcount == 1
    cutoff = now - watch.get("history_days", 14) * DAY
    # A trend needs at least two days to have a slope.
    min_days = max(2, watch.get("min_days", 7))
    history = db.execute("SELECT listed_at, price FROM listing WHERE watch = ? AND listed_at >= ?", (name, cutoff)).fetchall()
    m = market(history, now, min_days)
    max_z = watch.get("max_z", -2.5)
    exclude = [w.lower() for w in watch.get("exclude", [])]

    deals = []
    for item in items:
        if item.get("price", {}).get("currency") != "USD":
            continue
        title = item.get("title", "")
        if any(w in title.lower() for w in exclude):
            continue
        # A multi-variation listing ("6700 XT / 6800 XT") can surface in a
        # search priced at whichever variation is cheapest, which is a false
        # deal for the model you searched.
        if "itemGroupHref" in item:
            continue
        # Too-good prices from new or poorly rated sellers are the classic
        # scam, and exactly what a low z-score flags.
        seller = item.get("seller", {})
        if int(seller.get("feedbackScore", 0)) < watch.get("min_seller_feedback", 0) or float(seller.get("feedbackPercentage", 0)) < watch.get("min_seller_percent", 0):
            continue
        d = delivered(item)
        if d is None or d[0] < watch.get("min_price", 0):
            continue
        price, shipping = d
        # INSERT OR IGNORE keeps the price it was listed at; rowcount says whether it is new.
        if not db.execute("INSERT OR IGNORE INTO listing VALUES (?, ?, ?, ?)", (name, item["itemId"], price, min(listed_at(item, now), now))).rowcount:
            continue
        if first_poll or m is None:
            continue
        # Measured from the lower of today's trend price and the recent
        # median: in a falling market that's the trend, so riding the slope
        # down isn't a deal; in a rising one it's the median, so lagging the
        # climb isn't either.
        z = (price - min(m["level"], m["expected"])) / m["spread"]
        if z <= max_z:
            deals.append({**m, "title": title, "price": price, "shipping": shipping, "z": z, "url": item["itemWebUrl"], "image": item.get("image", {}).get("imageUrl"), "condition": item.get("condition", ""), "seller": f"seller {seller.get('feedbackScore', '?')} ({seller.get('feedbackPercentage', '?')}%)"})
    baseline = None
    if first_poll:
        recorded = db.execute("SELECT listed_at, price FROM listing WHERE watch = ? AND listed_at >= ?", (name, cutoff)).fetchall()
        days = len({int((now - t) // DAY) for t, _ in recorded})
        baseline = {"count": len(recorded), "median": statistics.median(p for _, p in recorded) if recorded else None, "days_to_go": max(0, min_days - days)}
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
        push(f"{watch['name']}: ${d['price']:.2f} delivered", "\n".join(lines), click=d["url"], image=d["image"], priority=4, tags=["moneybag"])
    if len(deals) > MAX_PUSHES:
        rest = deals[MAX_PUSHES:]
        push(f"{watch['name']}: {len(rest)} more deals", f"From ${rest[0]['price']:.2f} to ${rest[-1]['price']:.2f} delivered.", click=f"https://www.ebay.com/sch/i.html?{urllib.parse.urlencode({'_nkw': watch['query'], '_sop': 10})}")


def poll(db: sqlite3.Connection, config: dict) -> list[str]:
    """One pass over every watch. Returns the errors, one per failed watch."""
    errors = []
    for watch in config.get("watch", []):
        try:
            items = search(watch, str(config["zip"]))
            if db.execute("SELECT 1 FROM seeded WHERE watch = ?", (watch["name"],)).fetchone() is None:
                # New watch: page back through what's already listed to seed
                # its history. A full page means there may be more.
                since = time.time() - watch.get("history_days", 14) * DAY
                while items and len(items) < SEED_LIMIT and len(items) % 200 == 0 and listed_at(items[-1], 0) >= since:
                    items += search(watch, str(config["zip"]), len(items))
            deals, baseline = check(db, watch, items, time.time())
            if baseline:
                # Commit the seed before announcing it. A first poll has no
                # deals to lose, and if a failed push rolled it back, the next
                # poll would page back through 1,000 listings again.
                db.commit()
                median = f", median ${baseline['median']:.2f} delivered" if baseline["median"] is not None else ""
                start = "Alerts are on." if not baseline["days_to_go"] else f"Alerts start in about {baseline['days_to_go']} days, once there's enough history to tell a deal from a falling market."
                try:
                    push(f"Watching {watch['name']}", f"{baseline['count']} listings from the last two weeks recorded{median}. {start}", tags=["eyes"])
                except Exception:
                    log.exception("could not announce %s", watch["name"])
            push_deals(watch, deals)
            # Only now are this poll's listings "seen". Committing before the
            # pushes would lose a deal for good whenever ntfy is unreachable.
            db.commit()
            log.info("%s: %d listings fetched, %d deals", watch["name"], len(items), len(deals))
        except Exception as e:
            # Undo this watch's half-recorded poll so its listings are retried
            # next time (at worst a repeat push) and the next watch's commit
            # doesn't save them by accident.
            db.rollback()
            log.exception("%s failed", watch.get("name"))
            errors.append(f"{watch.get('name')}: {e}")
    return errors


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config_path = Path(os.environ.get("WATCHES", "/app/watches.toml"))
    db = sqlite3.connect(os.environ.get("DB", "/data/listings.db"))
    db.executescript(SCHEMA)
    failing_since = None
    alerted = False
    while True:
        interval = 10
        try:
            # Re-read every poll so editing watches.toml needs no restart.
            config = tomllib.loads(config_path.read_text())
            interval = config.get("interval_minutes", interval)
            errors = poll(db, config)
        except Exception as e:
            log.exception("poll failed")
            errors = [str(e)]
        if not errors:
            failing_since, alerted = None, False
        elif failing_since is None:
            failing_since = time.time()
        elif not alerted and time.time() - failing_since >= FAIL_ALERT_AFTER:
            try:
                push("eBay watcher is failing", f"Every poll for {(time.time() - failing_since) / 60:.0f} minutes. Last errors:\n" + "\n".join(errors), priority=4, tags=["warning"])
                alerted = True
            except Exception:
                log.exception("could not push the failure alert")
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
