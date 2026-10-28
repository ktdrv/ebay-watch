"""Check outlook()'s odds of a cheaper listing against what actually happened.

Run with: uv run --no-project python backtest.py listings.db
on a copy of the box's database (it needs listing.seen_at, which the watcher
adds on start).

Replays every priced listing the watcher saw live as if it had just appeared:
the market and odds use only what the watcher had seen by then, then the
database says whether a cheaper one (by the watch's cheaper_by margin, in
delivered dollars, also seen live) did turn up within a day and a week.
Listings too close to the end of a watch's data for a full horizon are left
out. Settings come from
watches.toml, with the defaults for watches no longer in it.
"""

import sqlite3
import statistics
import sys
import tomllib
from pathlib import Path

import watch

DAY = watch.DAY
BANDS = [(0, 0.1), (0.1, 0.3), (0.3, 0.6), (0.6, 0.9), (0.9, 1.01)]


def main(path: str) -> None:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    config = {w["name"]: w for w in tomllib.loads((Path(__file__).parent / "watches.toml").read_text())["watch"]}
    # (watch, horizon days, predicted, happened, alert, days until a cheaper one or None, predicted even-odds day)
    rows = []
    for (name,) in db.execute("SELECT DISTINCT watch FROM listing WHERE price IS NOT NULL"):
        w = config.get(name, {})
        days, max_z, margin = w.get("history_days", 30), w.get("max_z", -2.0), w.get("cheaper_by", 3) / 100
        # A NULL seen_at (a database never backfilled) counts as seen live.
        listings = db.execute("SELECT listed_at, price, coalesce(seen_at, listed_at) FROM listing WHERE watch = ? AND price IS NOT NULL ORDER BY listed_at", (name,)).fetchall()
        start, end = min(s for _, _, s in listings), max(s for _, _, s in listings)
        live = [(t, p) for t, p, s in listings if s - t <= watch.LIVE]
        for t, p in live:
            history = [(u, q) for u, q, s in listings if s <= t and t - days * DAY <= u < t]
            m = watch.market(history, t, *watch.readiness(w))
            o = m and watch.outlook([(u, q) for u, q in live if t - days * DAY <= u < t], m, p, t, max(start, t - days * DAY), margin)
            if not o:
                continue
            nxt = next((u for u, q in live if u > t and q < p * (1 - margin)), None)
            wait = None if nxt is None else (nxt - t) / DAY
            for horizon, key in ((1, "day"), (7, "week")):
                if t + horizon * DAY <= end:
                    rows.append((name, horizon, o[key], wait is not None and wait <= horizon, p <= watch.bar(m, max_z), wait, o["even_odds_days"]))

    for horizon in (1, 7):
        print(f"\n== Cheaper one within {horizon} day(s)")
        print(f"{'predicted':>12} {'listings':>9} {'mean predicted':>15} {'actual':>7}")
        for lo, hi in BANDS:
            band = [r for r in rows if r[1] == horizon and lo <= r[2] < hi]
            if band:
                print(f"{lo:>5.0%}-{min(hi, 1):<5.0%} {len(band):>9} {statistics.mean(r[2] for r in band):>15.0%} {statistics.mean(r[3] for r in band):>7.0%}")
        for label, subset in (("alerts", [r for r in rows if r[1] == horizon and r[4]]), ("all", [r for r in rows if r[1] == horizon])):
            if subset:
                print(f"{label}: {len(subset)} listings, predicted {statistics.mean(r[2] for r in subset):.0%}, actual {statistics.mean(r[3] for r in subset):.0%}, Brier {statistics.mean((r[2] - r[3]) ** 2 for r in subset):.3f}")
        for name in sorted({r[0] for r in rows}):
            a = [r for r in rows if r[1] == horizon and r[0] == name and r[4]]
            if a:
                print(f"  {name}: {len(a)} alerts, predicted {statistics.mean(r[2] for r in a):.0%}, actual {statistics.mean(r[3] for r in a):.0%}")

    print("\n== Alerts: predicted even-odds day vs days until a cheaper one actually listed")
    for r in sorted((r for r in rows if r[4] and r[1] == 1), key=lambda r: (r[0], r[6] or 99)):
        print(f"  {r[0]:<18} predicted {'>30' if r[6] is None else f'{r[6]:.1f}':>5}  actual {'none yet' if r[5] is None else f'{r[5]:.1f}':>8}")
    if not rows:
        print("Nothing to replay: no watch has a week of live listings with a ready market.")


if __name__ == "__main__":
    main(sys.argv[1])
