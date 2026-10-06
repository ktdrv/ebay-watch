"""Render the watcher's dashboard: one HTML page, charts drawn as inline SVG.

Takes plain numbers from watch.dashboard_data() and knows nothing about eBay
or the statistics, so a layout change can't break alerting. No JavaScript:
hovering a dot shows its listing through the SVG's native <title> tooltip,
and the table under each chart is the same data in readable form.
"""

import html
import time

# Plot geometry in SVG units; the SVG scales to the card's width.
W, H = 720, 260
LEFT, RIGHT, TOP, BOTTOM = 52, 132, 14, 30
DAY = 86400
TABLE_ROWS = 20

CSS = """
.viz-root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --border: rgba(11,11,11,0.10);
  --listing: #2a78d6; --deal: #eb6834; --good: #0ca30c; --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  .viz-root {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --border: rgba(255,255,255,0.10);
    --listing: #3987e5; --deal: #d95926;
  }
}
* { box-sizing: border-box; }
body { margin: 0; }
.viz-root { background: var(--page); color: var(--ink); min-height: 100vh; padding: 32px 16px 64px;
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 960px; margin: 0 auto; }
header { margin-bottom: 24px; }
h1 { font-size: 20px; font-weight: 600; margin: 0 0 4px; }
.sub { color: var(--ink-2); margin: 0; }
.status-bad { color: var(--ink); }
.status-bad::before, .pill-on::before { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; content: ""; }
.status-bad::before { background: var(--critical); }
section { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 24px; }
.head { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
h2 { font-size: 17px; font-weight: 600; margin: 0; }
.pill { color: var(--ink-2); font-size: 13px; }
.pill-on::before { background: var(--good); }
.stats { display: flex; gap: 32px; flex-wrap: wrap; margin: 16px 0 8px; }
.stat-label { color: var(--ink-2); font-size: 12px; }
.stat-value { font-size: 20px; font-weight: 600; }
.legend { display: flex; gap: 16px; color: var(--ink-2); font-size: 12px; margin: 8px 0 4px; }
.key { display: inline-block; vertical-align: middle; margin-right: 6px; }
.key-dot { width: 8px; height: 8px; border-radius: 50%; }
.key-line { width: 14px; height: 0; border-top: 2px solid; }
svg { display: block; width: 100%; height: auto; }
svg text { font: 11px system-ui, -apple-system, "Segoe UI", sans-serif; fill: var(--muted); }
svg .line-label { fill: var(--ink-2); }
table { width: 100%; border-collapse: collapse; margin-top: 16px; font-size: 13px; }
th { text-align: left; font-weight: 500; color: var(--ink-2); font-size: 12px; padding: 6px 8px; border-bottom: 1px solid var(--grid); }
td { padding: 6px 8px; border-bottom: 1px solid var(--grid); vertical-align: top; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
td.when { color: var(--ink-2); white-space: nowrap; }
td.seller { color: var(--ink-2); }
a { color: inherit; }
tr.deal td:first-child { box-shadow: inset 3px 0 var(--deal); }
.badge { font-size: 12px; color: var(--ink); white-space: nowrap; }
.badge::before { content: ""; display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--deal); margin-right: 5px; }
.note { color: var(--ink-2); font-size: 12px; margin: 8px 0 0; }
.empty { color: var(--ink-2); padding: 32px 0; }
"""


def money(x: float) -> str:
    return f"${x:,.0f}"


def ago(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f} s ago"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 36 * 3600:
        return f"{seconds / 3600:.0f} h ago"
    return f"{seconds / DAY:.0f} d ago"


def nice_step(span: float) -> float:
    """A round tick step giving about five ticks across span."""
    for step in (5, 10, 20, 25, 50, 100, 200, 250, 500, 1000):
        if span / step <= 6:
            return step
    return 2000


def chart(w: dict, now: float) -> str:
    listings, m, days = w["listings"], w["market"], w["days"]
    prices = sorted(l["price"] for l in listings)
    lines = [p for _, p in w["bar_line"] + w["trend_line"]] + ([m["level"]] if m else [])
    # Top of the scale at the 90th percentile, not the maximum: one import at
    # three times the going rate would otherwise flatten everything else.
    # Anything above is drawn pinned to the top edge as a triangle.
    hi = max([prices[int(0.9 * (len(prices) - 1))]] + lines) * 1.06
    lo = min(prices[:1] + lines) * 0.94
    step = nice_step(hi - lo)
    lo = (lo // step) * step
    hi = -(-hi // step) * step
    plot_w, plot_h = W - LEFT - RIGHT, H - TOP - BOTTOM

    def x(days_ago: float) -> float:
        return LEFT + plot_w * (1 - days_ago / days)

    def y(price: float) -> float:
        return TOP + plot_h * (hi - min(price, hi)) / (hi - lo)

    out = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="{html.escape(w["name"])} delivered prices over the last {days} days">']
    v = lo
    while v <= hi + 0.01:
        out.append(f'<line x1="{LEFT}" x2="{W - RIGHT}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="var(--grid)" stroke-width="1"/>')
        out.append(f'<text x="{LEFT - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{money(v)}</text>')
        v += step
    out.append(f'<line x1="{LEFT}" x2="{W - RIGHT}" y1="{TOP + plot_h}" y2="{TOP + plot_h}" stroke="var(--axis)" stroke-width="1"/>')
    for d in range(days, -1, -7):
        label = time.strftime("%b %-d", time.localtime(now - d * DAY))
        out.append(f'<text x="{x(d):.1f}" y="{H - 8}" text-anchor="middle">{label}</text>')

    labels = []
    if m:
        out.append(f'<line x1="{x(days):.1f}" x2="{x(0):.1f}" y1="{y(m["level"]):.1f}" y2="{y(m["level"]):.1f}" stroke="var(--muted)" stroke-width="1"/>')
        (d0, p0), (d1, p1) = w["trend_line"]
        out.append(f'<line x1="{x(d0):.1f}" x2="{x(d1):.1f}" y1="{y(p0):.1f}" y2="{y(p1):.1f}" stroke="var(--ink-2)" stroke-width="2" stroke-linecap="round"/>')
        pts = " ".join(f"{x(d):.1f},{y(p):.1f}" for d, p in w["bar_line"])
        # Dashed because it is a threshold, not data.
        out.append(f'<polyline points="{pts}" fill="none" stroke="var(--deal)" stroke-width="2" stroke-dasharray="5 4"/>')
        labels = [(y(m["level"]), f"Median {money(m['level'])}"), (y(p1), f"Trend {money(p1)}"), (y(w["bar_line"][-1][1]), f"Alert at ≤ {money(w['bar_line'][-1][1])}")]

    # Oldest first so the newest dots sit on top.
    for l in reversed(listings):
        cx, cy = x(l["days_ago"]), y(l["price"])
        tip = html.escape(f"{l['title']}\n{money(l['price'])} delivered · {ago(l['days_ago'] * DAY)}")
        color = "var(--deal)" if l["alerted"] else "var(--listing)"
        mark = (f'<path d="M{cx:.1f},{cy - 5:.1f} l5,8 h-10 z" fill="{color}" stroke="var(--surface)" stroke-width="2"/>' if l["price"] > hi
                else f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="{color}" stroke="var(--surface)" stroke-width="2"/>')
        # The transparent circle is the hover target, much bigger than the dot.
        out.append(f'<a href="{html.escape(l["url"] or "#")}" target="_blank"><title>{tip}</title><circle cx="{cx:.1f}" cy="{cy:.1f}" r="12" fill="transparent"/>{mark}</a>')

    # Direct labels at the right end of each line. Where two would overlap,
    # push them apart and draw a short leader back to the line.
    labels.sort()
    placed = []
    for line_y, text in labels:
        label_y = max(line_y, placed[-1] + 14) if placed else line_y
        placed.append(label_y)
        if abs(label_y - line_y) > 1:
            out.append(f'<line x1="{x(0) + 2:.1f}" y1="{line_y:.1f}" x2="{x(0) + 8:.1f}" y2="{label_y:.1f}" stroke="var(--axis)" stroke-width="1"/>')
        out.append(f'<text class="line-label" x="{x(0) + 10:.1f}" y="{label_y + 4:.1f}">{html.escape(text)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def table(w: dict, now: float) -> str:
    rows = []
    for l in w["listings"][:TABLE_ROWS]:
        ship = "free" if l["shipping"] == 0 else ("" if l["shipping"] is None else money(l["shipping"]))
        z = "" if l["z"] is None else f"{l['z']:+.1f}".replace("-", "−")
        title = html.escape(l["title"] or "")
        link = f'<a href="{html.escape(l["url"])}" target="_blank" rel="noopener">{title}</a>' if l["url"] else title
        badge = ' <span class="badge">Alerted</span>' if l["alerted"] else ""
        rows.append(
            f'<tr class="{"deal" if l["alerted"] else ""}"><td class="when">{ago(l["days_ago"] * DAY)}</td>'
            f'<td class="num">{money(l["price"])}</td><td class="num">{ship}</td><td class="num">{z}</td>'
            f'<td>{link}{badge}</td><td class="seller">{html.escape(l["seller"] or "")}</td></tr>'
        )
    more = f'<p class="note">Newest {TABLE_ROWS} of {len(w["listings"])}. Every listing is a dot in the chart; hover one for its title, click to open it.</p>' if len(w["listings"]) > TABLE_ROWS else ""
    return (
        '<table><thead><tr><th>Listed</th><th class="num">Delivered</th><th class="num">Shipping</th>'
        '<th class="num" title="Spreads above (+) or below (−) the going rate when it was listed">z</th><th>Listing</th><th>Seller</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>{more}"
    )


def section(w: dict, now: float) -> str:
    m, listings = w["market"], w["listings"]
    min_listings, min_days = w["needs"]
    if m:
        pill = '<span class="pill pill-on">Alerts on</span>'
        trend = f"{'−' if m['per_week'] < 0 else '+'}{money(abs(m['per_week']))}/week"
        stats = [("Going rate", money(min(m["level"], m["expected"]))), ("Trend", trend), ("Alerts at or under", money(w["bar_line"][-1][1])), (f"Listings, last {w['days']} days", str(len(listings)))]
    else:
        days_seen = len({int(l["days_ago"]) for l in listings})
        short = ([f"{len(listings)} of {min_listings} listings"] if len(listings) < min_listings else []) + ([f"{days_seen} of {min_days} days"] if days_seen < min_days else [])
        pill = f'<span class="pill">Warming up: {", ".join(short)}</span>'
        stats = [(f"Listings, last {w['days']} days", str(len(listings)))]
    stat_html = "".join(f'<div><div class="stat-label">{label}</div><div class="stat-value">{value}</div></div>' for label, value in stats)
    legend = (
        '<div class="legend"><span><span class="key key-dot" style="background:var(--listing)"></span>Listing</span>'
        '<span><span class="key key-dot" style="background:var(--deal)"></span>Alerted</span>'
        + ('<span><span class="key key-line" style="border-color:var(--deal);border-top-style:dashed"></span>Alert bar</span>' if m else "")
        + "</div>"
    )
    body = f"{legend}{chart(w, now)}{table(w, now)}" if listings else '<p class="empty">No priced listings yet.</p>'
    return f'<section><div class="head"><h2>{html.escape(w["name"])}</h2>{pill}</div><div class="stats">{stat_html}</div>{body}</section>'


def render(watches: list[dict], status: dict, now: float) -> str:
    if status["last_poll"] is None:
        sub = '<p class="sub">Starting up: no check has finished yet.</p>'
    elif status["errors"]:
        sub = f'<p class="sub status-bad">Last check {ago(now - status["last_poll"])} failed: {html.escape("; ".join(status["errors"]))}</p>'
    else:
        sub = f'<p class="sub">Last checked {ago(now - status["last_poll"])}. Delivered prices to your ZIP; the page refreshes every two minutes.</p>'
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta http-equiv="refresh" content="120"><title>eBay watch</title><style>{CSS}</style></head>'
        f'<body><div class="viz-root"><main><header><h1>eBay watch</h1>{sub}</header>'
        f"{''.join(section(w, now) for w in watches)}</main></div></body></html>"
    )
