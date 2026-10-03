"""Poll eBay for new listings and push the good deals to ntfy.

A new listing is a deal when its delivered price (item + cheapest shipping to
the configured ZIP) is at or under the watch's max_price, or when its robust
z-score against recently seen listings for the same watch is at or under max_z.
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
import urllib.parse
import urllib.request
from pathlib import Path

EBAY = "https://api.ebay.com"
# Alert after this many consecutive failed polls (an hour at the default
# interval), so one eBay hiccup doesn't page the phone.
FAIL_ALERT_AFTER = 6
# More deals than this in one poll for one watch gets the cheapest few plus a
# summary, instead of a wall of pushes.
MAX_PUSHES = 5

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


def search(watch: dict, zip_code: str) -> list[dict]:
    """The newest 200 listings matching the watch."""
    filters = ["buyingOptions:{" + "|".join(watch.get("buying_options", ["FIXED_PRICE"])) + "}", "deliveryCountry:US"]
    if "condition_ids" in watch:
        filters.append("conditionIds:{" + "|".join(map(str, watch["condition_ids"])) + "}")
    # No price filter on purpose: the z-score needs the whole distribution.
    params = {"q": watch["query"], "sort": "newlyListed", "limit": 200, "filter": ",".join(filters)}
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


def delivered(item: dict) -> tuple[float, float | None]:
    """(item price + cheapest shipping, shipping). Shipping is None when eBay quoted none."""
    price = float(item["price"]["value"])
    costs = [float(o["shippingCost"]["value"]) for o in item.get("shippingOptions", []) if "shippingCost" in o]
    if not costs:
        return price, None
    return price + min(costs), min(costs)


def robust_z(x: float, sample: list[float]) -> float | None:
    """z-score using median and MAD, so a few $1 "box only" or $5000 bundle
    listings don't drag the baseline the way they would a mean and stdev."""
    median = statistics.median(sample)
    mad = statistics.median(abs(p - median) for p in sample)
    if mad == 0:
        return None
    return (x - median) / (1.4826 * mad)


def check(db: sqlite3.Connection, watch: dict, items: list[dict], now: float) -> tuple[list[dict], dict | None]:
    """Record this poll's listings, uncommitted, and return (deals among the new ones, baseline).

    baseline is set only on a watch's first poll, which records everything
    and alerts on nothing, since every listing would otherwise look new.
    """
    name = watch["name"]
    first_poll = db.execute("SELECT 1 FROM listing WHERE watch = ? LIMIT 1", (name,)).fetchone() is None
    cutoff = now - watch.get("history_days", 30) * 86400
    history = [p for (p,) in db.execute("SELECT price FROM listing WHERE watch = ? AND last_seen >= ? AND shipping_known", (name, cutoff))]
    enough = len(history) >= watch.get("min_samples", 20)
    median = statistics.median(history) if history else None
    exclude = [w.lower() for w in watch.get("exclude", [])]

    deals = []
    for item in items:
        if item.get("price", {}).get("currency") != "USD":
            continue
        title = item.get("title", "")
        if any(w in title.lower() for w in exclude):
            continue
        price, shipping = delivered(item)
        if price < watch.get("min_price", 0):
            continue
        is_new = db.execute("SELECT 1 FROM listing WHERE watch = ? AND item_id = ?", (name, item["itemId"])).fetchone() is None
        db.execute(
            "INSERT INTO listing (watch, item_id, price, shipping_known, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (watch, item_id) DO UPDATE SET price = excluded.price, shipping_known = excluded.shipping_known, last_seen = excluded.last_seen",
            (name, item["itemId"], price, shipping is not None, now, now),
        )
        if first_poll or not is_new:
            continue
        # An unknown shipping cost makes the z-score meaningless, so those
        # listings can only qualify on max_price and the push says so.
        z = robust_z(price, history) if enough and shipping is not None else None
        cheap = price <= watch.get("max_price", 0)
        outlier = z is not None and z <= watch.get("max_z", float("-inf"))
        if cheap or outlier:
            deals.append({"title": title, "price": price, "shipping": shipping, "z": z, "median": median, "url": item["itemWebUrl"], "image": item.get("image", {}).get("imageUrl"), "condition": item.get("condition", ""), "variations": "itemGroupHref" in item})
    baseline = None
    if first_poll:
        recorded = [p for (p,) in db.execute("SELECT price FROM listing WHERE watch = ? AND shipping_known", (name,))]
        baseline = {"count": len(recorded), "median": statistics.median(recorded) if recorded else None}
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
    urllib.request.urlopen(req, timeout=30).close()


def push_deals(watch: dict, deals: list[dict]) -> None:
    for d in deals[:MAX_PUSHES]:
        ship = "shipping not quoted" if d["shipping"] is None else ("free shipping" if d["shipping"] == 0 else f"${d['price'] - d['shipping']:.2f} + ${d['shipping']:.2f} shipping")
        stats = f"z {d['z']:.1f}, median ${d['median']:.2f}" if d["z"] is not None else (f"median ${d['median']:.2f}" if d["median"] else "")
        lines = [d["title"], " · ".join(x for x in (ship, d["condition"], stats) if x)]
        if d["variations"]:
            lines.append("Has variations: this may be the cheapest option's price.")
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
            deals, baseline = check(db, watch, items, time.time())
            if baseline:
                median = f", median ${baseline['median']:.2f} delivered" if baseline["median"] is not None else ""
                push(f"Watching {watch['name']}", f"{baseline['count']} listings recorded as the baseline{median}. Alerts start with the next new listing.", tags=["eyes"])
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
    db.execute(
        "CREATE TABLE IF NOT EXISTS listing (watch TEXT, item_id TEXT, price REAL, shipping_known INTEGER,"
        " first_seen REAL, last_seen REAL, PRIMARY KEY (watch, item_id))"
    )
    failures = 0
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
        failures = failures + 1 if errors else 0
        if failures == FAIL_ALERT_AFTER:
            try:
                push("eBay watcher is failing", f"{failures} polls in a row. Last errors:\n" + "\n".join(errors), priority=4, tags=["warning"])
            except Exception:
                log.exception("could not push the failure alert")
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()
