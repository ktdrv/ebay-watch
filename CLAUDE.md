# eBay watcher: deal confidence

Handoff from the 2026-10-28 session. Code: `ebay-watch/watch.py` (poller, statistics, ntfy pushes), `ebay-watch/dashboard.py` (LAN-only dashboard at `ebay.example.com`), `ebay-watch/watches.toml` (watches, re-read every poll), `ebay-watch/test_watch.py`. Deployed to LXC 100 by `task deploy -- ebay-watch`; a `watch.py` change also needs `docker restart ebay-watch`.

## The goal, in the owner's words

> The use case here is I want to buy this one item. I want to get a good deal for it. I'm going to wait for a good deal. So I monitor what's being sold and I just want certainty that this is indeed a good deal so that when I click buy I don't see a better deal the next day. That's what I really want is just peace of mind that when this thing says this is a good deal, it is indeed a good deal, and I will be happy to spend the money.

In other words: **minimise buyer's regret**. An alert should mean "you're unlikely to see a comparable item cheaper soon", and the alert should say how unlikely, with a number backed by this watch's own history.

**Explicitly not the goal: time-to-sell.** The owner rejected it as a headline feature. A great deal gets bought on the spot anyway, and a bad deal's shelf life doesn't matter. Time-to-sell is useful only internally (see "attainable" below).

The owner thinks the "is it actually cheap" logic is the differentiator, and may later split this out as its own open-source project. That's not decided.

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

## Proposed approach (not started; the owner hasn't approved a design yet)

1. **Backtest the regret rate before building anything.** It needs no API calls, only the existing database. For every past listing at or below the alert bar (the bar computed with only the data available when it was listed, so no look-ahead): did a cheaper listing for the same watch appear within the next N days (try 1, 3, 7)? That gives "of past alerts at this level, X% were beaten within a week". This is the number behind "peace of mind", and it shows whether `max_z = -2` is already enough or needs tuning per watch. Run it on the retired watches' data as well.
2. **Show the regret probability on each alert.** Chance a cheaper comparable listing appears within H days ≈ 1 − exp(−λ·H·F(p)), where:
   - λ = this watch's arrival rate of listings per day;
   - F(p) = the share of listings at or below this price relative to the trend, so a falling market raises it over the horizon.

   The alert could read: "One this cheap turns up about every 3 weeks; 20% chance of a cheaper one this week." Then check the predicted probabilities against the backtest (calibration), because a wrong number is worse than none.
3. **Count only attainable listings as "a better deal".** A cheaper listing that sold within minutes, before an alert could reach the owner, wasn't a real option. Use a sold time from the item lookup, or just "would it have alerted", so the regret number is honest.
4. **Build the baseline from listings that sold (or sold plus still live), not from all asks.** Recording outcomes means one item lookup per listing once it disappears from search or after N days. That's a few hundred calls a day against a 5,000/day quota shared with polling: budget it.
5. **Keep the comparisons like for like.** Mismatched comparables are the main way a "deal" turns out fake, or a "better deal" turns out not comparable. Candidates: condition buckets (new / used / refurbished), and title flags for bundles, broken or missing parts, "box only", "read description". An LLM (Haiku) could tag each title. Seller signals such as feedback or a new account could separately mark a deal as too good to be true; z far below −4 is suspicious, not great.

Order: 1 first (cheap, and it decides whether 2–5 are needed), then 2 + 3, then 4, then 5.

## Open questions for the owner

- The horizon H for "a better deal": a day, a week, or until a typical buyer would give up?
- Whether alerts should be suppressed (rather than labelled) when the regret probability is above some threshold.
- Whether this becomes its own repo. If it does, it follows Almanac's pattern: source in its own repo, the homelab keeps compose and `.env`, and a `deploy-*` task copies the code across.
