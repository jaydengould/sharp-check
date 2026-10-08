# Phase 6: habit metrics and execution cost (design, 2026-10-03)

## Goal

Show how the user bets, not just how the bets did: bankroll over time, stake after wins vs losses, stake as a share of bankroll, when bets are placed relative to kickoff, and what fees cost, with an estimate of what limit (maker) orders would have saved. Exit check: all five views render on real data, and the bankroll curve ends where reconcile does (reported cash + open stake, within 5¢).

Phase 5 is deferred (roadmap decision 2026-10-03), so this follows phase 4.

## Findings that shaped the design (2026-10-03)

- All 183 fills are taker fills; there are no maker fills to compare against.
- 145 bets (72 singles, 73 parlays), 2025-10-31 to 2026-09-28; 113 are pregame with a start time.
- Balance snapshots exist only from 2026-10-02, so the bankroll history must be rebuilt from the ledger and bets.
- Fee schedules (no API reports maker rates for past fills; hand-entered):
  - Polymarket US (https://docs.polymarket.us/fees.md, effective 2026-10-01): taker `0.0695 × C × p(1−p)`, maker **rebate** `0.0125 × C × p(1−p)`. Combos have their own schedule.
  - Kalshi: taker `0.07 × C × p(1−p)`. Maker fee depends on the series `fee_type`: `quadratic` has none, `quadratic_with_maker_fees` charges `0.0175 × C × p(1−p)`. We assume 0.0175 for every series (worst case, so savings are understated).

## Decisions

- Execution cost is **fees only** (option A). Spread paid at entry isn't archived; measuring it (option B: price window around each entry fill) may come later.
- "After a loss" = the most recent bet, on either venue, that settled or closed **before this bet was opened**, judged by the sign of its realized P&L. A result of exactly 0 is its own tag (`after_push`); see view 2.
- Bankroll = account cash + open position cost, both venues combined (existing decision).
- Maker savings for singles only: Kalshi parlays are RFQ-quoted and Polymarket combos have a separate fee schedule.

## Data

One schema change: `bets.closed_ts` (UTC, `%Y-%m-%dT%H:%M:%S.%fZ`): the settlement's `settled_ts` for `settled`/`void`, the last exit fill's `ts` for `closed_early`, NULL for `open`. Set in `normalize.derive_bets`. No new archiving.

## Views (`sharp_check/habits.py`, computed at page load)

### 1. Bankroll curve

- `bankroll_series(conn) -> [(ts, bankroll, betting_pnl)]`, both venues: events are `cash_ledger` rows (amount) and closed bets (realized P&L at `closed_ts`), sorted by time; running sums. Placing a bet doesn't move bankroll (cash becomes open position cost).
- Bankroll at time t = all events with `ts <= t`.
- Check: last bankroll == Σ reported cash + Σ open stake (within 5¢). Shown on the page as OK/MISMATCH.
- Dashboard: one Plotly line chart, bankroll and cumulative betting P&L.

### 2. Stake after a win vs a loss

- For each bet, the previous result = the bet with the latest `closed_ts < opened_ts` (any venue). Tag: `after_win` (realized P&L > 0), `after_loss` (< 0), `after_push` (= 0), `first` (none yet).
- Per tag: n, median stake, median stake % of bankroll.

### 3. Stake as % of bankroll

- Bankroll just before each bet (events with `ts < opened_ts`).
- Median, 90th percentile, max; table of the 5 largest by %.

### 4. Time placed before kickoff

- Pregame bets with a `start_time` (`is_live = 0`). Lead = `start_time − opened_ts`.
- Buckets: `< 15 min`, `15 min–1 h`, `1–6 h`, `6–24 h`, `> 24 h` (lower bound inclusive).
- Per bucket: n, median stake; mean CLV after fees (`clv_net`) only when that bucket has n ≥ 30 bets with CLV, else "hidden (n < 30)".

### 5. Execution cost

- By venue × singles/parlays: contracts, fees $, fees ¢ per contract, fees % of stake. Fees come from `fills.fee` of entry and exit fills.
- Estimated maker savings (singles): Σ over single fills of `fee − maker_fee`, `maker_fee = θ_maker × qty × p(1−p)` with `p` the fill price as a fraction, θ_maker = 0.0175 (Kalshi), −0.0125 (Polymarket). Rates live as constants in `habits.py` with the source and date.
- UI caveat: assumes a limit order fills at the same price; ignores spread saved and orders that never fill.

## Dashboard

New "Habits" section after "Market CLV": bankroll chart + check line, the four tables, and the execution-cost table with its caveat. Chart follows the dataviz skill.

## Testing (offline, in-memory DB)

- `closed_ts` for settled, closed-early (last exit fill) and open bets.
- Bankroll series: deposit, a closed-early bet and a settled bet; open bets don't move it; final value check.
- Win/loss tags: a bet that settles after the next bet is opened isn't its previous result; cross-venue ordering; `first` and `after_push`.
- Bucket boundaries (exactly 15 min goes to `15 min–1 h`).
- Maker savings for one Kalshi and one Polymarket fill, including a negative (rebate) maker fee.

## Docs to update

`docs/data-model.md` (`closed_ts`), `docs/pnl-rules.md` (habit definitions, fee assumptions), `docs/roadmap.md` (phase 6 done when exit check passes).

## Out of scope

Spread at entry (option B, maybe later), per-series Kalshi maker fee lookup, Polymarket combo fees, losing-streak depth, daily-session tilt.
