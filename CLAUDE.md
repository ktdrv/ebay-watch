# eBay watcher

Polls eBay for new listings of the one item you want to buy and pushes the real deals to ntfy. Standard library only. Code: `watch.py` (poller, statistics, ntfy pushes), `dashboard.py` (dashboard served on :8091), `watches.toml` (the watches, re-read every poll), `test_watch.py` (`uv run --no-project python -m unittest test_watch.py`; it also checks the title regexes in `watches.toml`), `backtest.py` (replays a copy of the database to check the alert odds against what happened).

Split out of the deployment repo on 2026-10-28 with full history. The homelab keeps only the deployment: `ebay-watch/compose.yaml` and `ebay-watch/.env` (eBay and ntfy keys). `task deploy-ebay-watch` in the homelab repo copies this tree, `watches.toml` included, to the Docker host and restarts the container. So editing a watch means editing it here, then deploying from the homelab. The dashboard sits behind the homelab's LAN-only `ebay.example.com` and has no login, so keep it off the public internet.

## Current task: deal confidence

Handoff from the 2026-10-28 session.

## The goal, in the owner's words

> The use case here is I want to buy this one item. I want to get a good deal for it. I'm going to wait for a good deal. So I monitor what's being sold and I just want certainty that this is indeed a good deal so that when I click buy I don't see a better deal the next day. That's what I really want is just peace of mind that when this thing says this is a good deal, it is indeed a good deal, and I will be happy to spend the money.

In other words: **minimise buyer's regret**. An alert should mean "you're unlikely to see a comparable item cheaper soon", and the alert should say how unlikely, with a number backed by this watch's own history.

**Explicitly not the goal: time-to-sell.** The owner rejected it as a headline feature. A great deal gets bought on the spot anyway, and a bad deal's shelf life doesn't matter. Time-to-sell is useful only internally (see "attainable" below).

The owner thinks the "is it actually cheap" logic is the differentiator, and may open-source it. That's not decided.

## How it decides today

`market()` in `watch.py`:
- **Price data:** asking prices (delivered: item plus cheapest shipping to the configured ZIP) of listings seen in the last `history_days` (30).
- **Trend:** a Theil-Sen line through those prices (median pairwise slope, robust to ~29% junk).
- **Spread:** a one-sided robust spread below the line (1.4826 × median distance below), floored at 2% of price.
- **Alert rule:** a new listing alerts when it is at least `max_z` (default −2) spreads under the lower of the 30-day median and the trend's price for today.

Listings are filtered first by title regex, an exclude list, condition "for parts", multi-variation listings and non-USD prices. Only listings located in the US count.

Active watches: RTX 4070 Ti Super, RX 9070 XT, RTX 5070 Ti, every 2 min (watch the quota sum in `watches.toml`). The database on the box (`/data/listings.db` in the `ebay-watch` container) also holds history for retired watches (RTX 3090, RX 6900 XT, RX 7900 GRE/XT, RX 9070), about 950 listings in total.

## Finding: eBay already tells us what sold

The Browse API single-listing lookup (`GET /buy/browse/v1/item/{id}`, already used for shipping quotes) **still answers for ended listings**, with `itemEndDate` and `estimatedAvailabilities[].estimatedSoldQuantity`:

- end date in the past and sold quantity > 0 → **sold**
- end date in the past and sold quantity 0 → **ended unsold**
- `itemEndDate` in the future (fixed-price "good till cancelled" listings carry one) → **still live**
- 404s are rare (2 of 276)

So sold prices are available without scraping and without eBay's restricted Marketplace Insights API.

**Caveat:** about half of sales are Best Offer (87 of 173), and `price` is the list price, not the accepted offer. A "sold price" is therefore an upper bound on what the buyer paid.

Experiment on 2026-10-28: the three active watches, 274 priced listings first seen in the last 21 days. z is computed against today's market, which is slight look-ahead. The script was throwaway and deleted; it is about 60 lines to redo: for each listing, one item lookup, classify as above, bucket by z.

| z band | sold | unsold | live | sold % | median hours to sell |
|---|---|---|---|---|---|
| ≤ −2 (alerts) | 20 | 0 | 0 | 100% | 0.5 |
| −2 to −1 | 21 | 2 | 4 | 78% | 1.1 |
| −1 to 0 | 73 | 10 | 9 | 79% | 9.6 |
| 0 to 1 | 37 | 7 | 5 | 76% | 21 |
| > 1 | 22 | 21 | 41 | 26% | 94 |

Minutes to sell for the 20 alert-band listings: 10, 11, 12, then 13 between 25 and 34, then 173, 478, 502, 821.

Median ask vs median sold: 4070 Ti Super 924 vs 900, 9070 XT 805 vs 791, 5070 Ti 1165 vs 1117.

**What it shows:**
- The current z already ranks deals correctly: everything it calls a deal sells, and what it calls overpriced mostly doesn't.
- Better statistics on the same asking-price data would add little.
- Overpriced listings that never sell still sit in the baseline.

## Built on 2026-10-28: odds of a cheaper one, on every alert

Each alert now ends with a line like "Cheaper for their day: 2 of 60 listings seen in 21 days. Odds of a cheaper one within a week: 25%; even odds by day 12." The owner asked for the last part ("if I don't buy this one, how long until one as good turns up?"); it comes from the same model as the odds.

`outlook()` in `watch.py`: arrivals of cheaper listings are a Poisson process. A past listing counts as cheaper for a future day when it sat further under its own day's going rate than this price will sit under that day's, so a falling trend raises the odds with the horizon. Rate = (count + 0.5) / days watched; the half listing keeps "none so far" from reading as 0%. No odds until a watch has seen a week of live listings (`MIN_WATCHED`), because before that the half listing outweighs the data.

Only listings **seen live** count: `listing.seen_at` (new column) is when the watcher first saw a listing, and it counts as live when that is within 6 hours of eBay's listing date. The rest are unsold leftovers whose cheap siblings already sold: a new watch's seed, a widened search, and a steady trickle of months-old listings that resurface in search. Rows from before the column were backfilled on the box from insertion order (the newest listing date among all rows inserted up to that one, a lower bound on when it was seen).

"Attainable" (old step 3) is only partly handled. Every live listing reached the owner as a push within minutes if it alerted, but alert-price listings sell in 10–30 minutes, so one that listed at 3am wasn't a real chance. The regret numbers below count it anyway, so they run somewhat high.

### Backtest (`backtest.py`), 2026-10-28 snapshot

Live-seen data is thin: 21 days for the 4070 Ti Super, 13 for the 9070 XT and 5070 Ti, 8 for the retired watches. Every listing replayed with only what the watcher had seen by then:

- **Alerts are good prices, not the best of the week.** Of 12 alerts with a full week after them, 10 were beaten by a cheaper live listing within 7 days, usually by 5–15%. Within a day: 4 of 25.
- Calibration (run with the one-week minimum lowered to a day, for sample size): at 7 days alerts were predicted 82% and actually 83%, all listings 98% vs 98%. At 1 day the model overstates badly (alerts 48% predicted, 16% actual), so the alert leaves out the one-day odds. Likely cause: cheap listings come in clusters (one seller, two units), which a Poisson model counts as independent chances.
- With the one-week minimum in place, only 3 alerts have a full week to check; the calibration needs a few more weeks of data.
- Without the half-listing prior, alerts the model called 0% were beaten half the time, so it stays.
- Several of the "cheaper ones" were not comparable: Dell and HP OEM pulls (the cheapest 4070 Ti Super was an HP OEM card at $661), and on the 3090 "NVLink bridge" listings priced like cards. That is old step 5 below, and it matters more than expected: it inflates the regret odds.

Rerun: copy the database off the box (a `sqlite3` backup through `docker exec`; the box has no sqlite3 CLI), then `uv run --no-project python backtest.py listings.db`.

## Not built

- **Baseline from sold listings** (one item lookup per listing after it disappears). The sold-signal experiment above showed better statistics on the same asks add little; revisit only if the odds prove badly calibrated with more data.
- **Like-for-like comparisons**: condition buckets, title flags (OEM pull, bundle, bridge, box only), maybe a Haiku tagger; seller signals for "too good to be true". The backtest says this is now the most useful next step.

## Open questions for the owner

- Whether alerts should be suppressed (rather than labelled) when the odds of a cheaper one this week are high. At `max_z = -2` that would be most alerts.
- Whether an OEM pull (Dell, HP) counts as the same card.
- Whether "cheaper" should mean cheaper by a margin (say 3%), not by a dollar.
- Whether to open-source it. It now has its own repo, but publishing is undecided. `watches.toml` holds the home ZIP code, and the history mentions LAN addresses.
