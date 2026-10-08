# Phase 8a: spread paid at entry and exit (design, 2026-10-05)

## Goal

Measure what crossing the spread costs, per bet and per venue, at entry and at exit (cash-outs), next to the fees already on Habits. Done when Habits shows spread at entry and exit per bet and summed per venue with coverage (priced fills x of y), and Data health lists every fill without a spread with its reason.

## Decisions

- Phase 8 is split (2026-10-05). 8a = spread at entry and exit (this spec). 8b = cross-venue line shopping, its own spec later; it needs cross-venue game matching, which overlaps phase 5. 8b keeps the original line-shopping exit check.
- Approach: a quote at every fill (not one per bet), so scaled-in or scaled-out positions are priced fill by fill.
- Parlay exits are included: 8 of 10 parlay cash-outs are cross-game, and parlays hold almost all of the cash-out hindsight value. n = 8 is shown as a per-bet table and a total, no CI.
- Parlay entry spread needs no new quotes: it is the parlay CLV markup, `(avg_entry − fair_entry) × entry_qty`.
- Live bets are included and shown split from pregame (in-game prices move fast, so their cost is noisy).
- No venue switch on Habits (2026-10-05): cost tables compare venues side by side in rows. Several Habits metrics are cross-venue by definition (previous result on either venue, combined bankroll). A Habits switch is a possible follow-up after phase 8, with its own rules for those metrics.

## Findings (2026-10-05)

- 191 fills (Kalshi 110, Polymarket 81) in 151 bets. 25 bets have exits: 15 singles, 10 parlays.
- Kalshi: 1-minute `yes_bid`/`yes_ask` candles for every market back to 2025-11, in play included (all 9 live singles had a full hour of candles at their fill). A decided leg shows its latest candle pinned at 0.99/1.00 while the market is still open, and no candles at all once it has closed (every leg checked 4.6 h+ after kickoff). Candles carry a quote every minute whether or not anything trades, so "no candles" means closed, not thin.
- Polymarket: phase 4 fetched 1-minute history on 2026-10-04 for markets resolved up to ~104 days earlier (back to 2026-06-22), so resolved markets and history older than the changelog's 45-day market-data retention were served. On 2026-10-05 (after 01:07 UTC) every resolved market returned an empty history while open markets returned full history. No changelog entry; treated as an outage. The existing retry rule (empty responses aren't stored) covers it with no new code. Re-test before implementing the Polymarket part; if it persists, see "If Polymarket history stays empty".
- Unverified until the outage ends: Polymarket in-game points (documented `fixedInterval=INTERVAL_LIVE`, "starts 15 minutes before the event begins", implies they exist) and decided-leg behaviour (pinned vs empty).

## The cost rule

Every fill is already normalized to "acquire `outcome` at `price`" (an exit acquires the other side). One formula covers entries and exits on both sides:

    cost = price − mid(outcome)      per contract, micro-dollars, fees excluded

`mid(yes)` = the YES mid; `mid(no)` = ONE − YES mid. Positive = paid over mid. For an exit of a YES position (acquire `no` at q) this equals `mid_yes − (ONE − q)` = mid − exit price. Costs stay signed (negative = the price moved your way within the minute) and are summed as is.

## Which quote

The fill moves the book, so use the last quote that ends before the fill's minute. Let `m` = the fill's timestamp floored to the minute (Unix seconds); the key time is `m` on Kalshi and `m − 60` on Polymarket.

- Kalshi: a candle's `end_period_ts` is the inclusive end of its minute, so the candle ending at `m` is the minute before the fill.
- Polymarket: a point at `m − 60` covers at most up to `m` whichever end of its minute the timestamp marks.

For singles, the **latest point** at or before the key time decides (built 2026-10-05: falling back to an older two-sided quote priced a decided market's 0.99 sale against a pre-result quote), from **any archived page of that market whose window covers the key time** (not only a page fetched for that key). Saves a fetch when a fill falls inside an archived close window (10 of 48 Polymarket single fills do). A quote older than 5 minutes (`key − quote_ts > 300`) is stale: no cost. Singles' quote width is not limited (the spread is what's being measured).

## Parlay exits

For an exit fill on an eligible parlay (`archive.parlay_clv_legs` reason None, same rules as parlay CLV; ineligible parlays' exit fills get no cost), fair value at the fill = Π over legs of the leg's value on the parlay's side, each at the venue's key time. Per leg, look at the **latest point at or before the key** in the hour window (not `pick_close`, which skips pinned points and would fall back to an older in-play quote: one leg pinned at 0.99/1.00 would otherwise price at 0.86):

1. Latest point pinned (YES bid ≥ 0.99 or YES ask ≤ 0.01) → decided: the leg's final result from `markets.yes_value` (0 or ONE). If the result is missing or disagrees with the pin's direction → no cost.
2. No points in the hour, and the leg's game started before the key, and `markets.yes_value` is 0 or ONE → decided at that value (Kalshi: market closed). Polymarket decided-leg behaviour is unverified (see Findings); the same three rules apply.
3. Otherwise the latest point must be usable: two-sided, ≤ 5¢ wide (`LEG_MAX_SPREAD`) and ≤ 5 minutes old. If not → no cost.

Leg results come from `markets.yes_value` (`settlements` holds only markets with your own cash, so it has no leg rows). Kalshi leg records weren't archived, so `sync_kalshi_markets` also fetches the legs of every Kalshi parlay with an exit (added while building). The exit fill acquires the parlay's opposite side, so `cost = price − (ONE − fair)`, the same formula. The per-bet fold shows how many legs were priced as decided.

## Archive (`sync_closes`)

Adds targets, each `(venue, market, key time)`:

- Every fill on a single.
- Every exit fill on an eligible parlay, for each leg market.

Parlay entry fills add nothing (placement quotes exist). Dedup, store-only-non-empty, retry, error printing and the 5-minute cutoff (`key ≤ now − 300`) are unchanged; `fetch_close` is reused as is (60-minute window).

## If Polymarket history stays empty

Not built now. If resolved-market history doesn't return, Polymarket fills (and phase 4 closes and parlay CLV on new Polymarket bets) are priceable only when archive runs between the fill and resolution. Options then: run archive during games by habit, or a launchd job on the Mac (reverses "no scheduler in v1"). Until then, unpriced Polymarket fills show as "no quote" and fill in on a later run.

## Normalize

- New table `fill_costs` (venue, fill_id, bet_id, role entry|exit, qty, `mid_before`, `cost`, `note`), one row per fill per bet it touches (a fill that flips a position has two). `mid_before` = mid of the fill's `outcome`, `cost` per contract; both NULL when not computable, with the reason in `note` (a priced parlay exit notes how many legs were decided). A separate table, not columns on `fills`, because flips need two rows.
- `bets` gains `entry_spread_usd` and `exit_spread_usd` (micro-dollars, Σ qty × cost over that bet's entry or exit fills). NULL when any of those fills has no cost, or the bet has no exit fills (exit). Parlay `entry_spread_usd` = `(avg_entry − fair_entry) × entry_qty`, NULL when `fair_entry` is NULL.
- A fill's entry vs exit role comes from the same netting `derive_bets` already does (a fill acquiring the held side is an entry, the other side an exit). All rebuilt from `raw_pages`.

## Dashboard (Habits, Data health)

1. A spread table under the fees table in the Execution cost section (venue × singles/parlays): Spread at entry ($), Spread at exit ($), Spread per contract (¢), Priced fills (x of y). Separate, not extra fee-table columns, so it fits a phone. One line under it: spread per contract pregame vs live.
2. Fold under it: "Show all N bets", one row per bet with entry and exit spread (and, for parlay exits, legs priced as decided).
3. Cash-outs table: an "Exit spread" column per summary row; the per-bet fold gains "Fair exit" and "Spread". The note's "isn't measured yet" is rewritten around the measured figure.
4. Data health: "Fills without a spread", one row per fill with the reason: no quote, stale quote, parlay leg unpriced, parlay not eligible.

## Failure handling

A failed or empty fetch isn't stored and retries next run. Fills under 5 minutes old wait. An uncomputable cost is NULL, never 0: the bet's spread is NULL, it shows in "priced x of y", and the fill is on Data health.

## Tests (no network)

- Quote selection: Kalshi candle ending exactly at `m` counts; Polymarket point at `m − 60` counts and at `m` does not; a quote 301 s older than its key is stale; a quote from a close page whose window covers the key is used.
- Cost sign for entries and exits on YES and NO positions, including a Kalshi sell-YES stored as acquiring `no`.
- Parlay exit legs: latest point pinned → final result; pinned but result disagrees → NULL; latest pinned with an older in-play point before it → decided, not the older point; no points + started + result → decided; quoted leg wider than 5¢ → NULL.
- Aggregation: two entry fills at different prices sum; one unpriced fill → bet spread NULL.
- `sync_closes` requests each (market, key time) once even when several fills share a minute.

## Docs

`pnl-rules.md` (cost rule, quote choice, parlay exit legs), `data-model.md` (new columns), `polymarket-us.md` (`fixedInterval`, the 2026-10-05 outage and its outcome), `roadmap.md` (8a/8b split, current phase, Habits switch follow-up).
