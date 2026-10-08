# Parlay CLV (design, 2026-10-03)

## Goal

Market CLV for cross-game parlays, split into **markup** (what the venue charged over the legs' fair price when the parlay was placed) and **leg movement** (the legs' price change from placement to their own kickoffs). Done when the dashboard shows it for eligible parlays on both venues and lists every other parlay with a reason.

## Decisions

- Breakdown (option B): `fair_entry` = Π leg mids at placement; `close_mid` = Π leg mids at each leg's own kickoff (option A). Markup = `avg_entry − fair_entry` (positive = overpaid). Leg movement = `close_mid − fair_entry`. CLV = `close_mid − avg_entry` = movement − markup.
- Leg mid on the parlay's side: YES mid, or `1 − YES mid` for a NO leg (`parlay_legs.leg_outcome`).
- Mids, archiving into `raw_pages`, staleness (60 min) and "after fees" (`close_mid − stake / entry_qty`) follow phase 4.
- Same-game parlays get no CLV: multiplying correlated legs understates the joint probability. No correlation model.

## Findings (2026-10-03)

- 73 parlays: 43 pregame with every leg a different game (Kalshi 26, Polymarket 17), 22 same-game, 4 live, 2 with no start time (all-futures), 2 with a non-game leg (Kalshi).
- Post-review (2026-10-03): leg quotes must be ≤ 5¢ wide and ≤ 15 min old (3 Kalshi parlays drop out); habits timing CLV stays singles-only; pending parlays read "waiting for kickoff".
- 202 distinct leg markets (Kalshi 116, Polymarket 86). Leg kickoffs in one parlay span 0 h (24 parlays) up to ~4 days.
- A 16-leg sample returned prices at both kickoff and placement on both venues. Thin Kalshi props have fewer 1-minute candles but still a quote inside the hour.

## Eligibility (`archive.parlay_clv_legs`)

Returns `{bet_id: (reason, opened_ts, [(venue, leg_market_id, leg_outcome, leg_start_time)])}` for every parlay. `reason` is None when eligible, else the first of: `void`, `live`, `no start time` (`is_live` NULL), `leg without a game` (`markets.game_id` NULL), `same-game legs` (two legs share a `game_id`). Lives in `archive` because both `archive` and `normalize` use it (normalize imports archive).

## Archive

`sync_closes` adds, per leg of each eligible parlay, two windows: ending at the leg's kickoff and ending at the parlay's `opened_ts` (floored to the second). Same endpoints, keys `(venue, market, start_ts)`, skip and retry rules, and the 5-minute cutoff (a leg whose game hasn't started waits).

## Normalize

- `closing_lines` is renamed **`quotes`**: `venue, market_id, at_time, quote_ts, yes_bid, yes_ask, yes_mid`, key `(venue, market_id, at_time)`. It holds quotes at kickoff and at placement. The old table is dropped on rebuild.
- `bets.fair_entry` (new, micros): set only for parlays with CLV.
- Parlays with every leg quoted at both times get `close_mid`, `fair_entry`, `clv`, `clv_usd`, `clv_net`. `clv_move` stays NULL (no closing ask for a parlay). Lookup keys use `iso_epoch(epoch(t))` so fractional seconds can't break the join.

## Metrics and dashboard

- `metrics.parlay_clv(conn, venues)`: `eligible`, `with_clv`, `net_cents` + `net_ci`, `mean_cents` + `ci`, `markup_cents`, `move_cents`, `beat`, `clv_usd` and `pnl` (closed only), `excluded` = `[(bet_id, reason)]` including `missing leg quote`.
- `metrics.parlay_bets(conn)`: per-parlay rows.
- Dashboard: "Parlays" block under Market CLV: summary by Kalshi / Polymarket / Both, per-parlay table, excluded list, note on same-game exclusion.

## Testing (offline)

- 2-leg cross-game parlay with a NO leg: products, markup, movement, after-fees.
- Excluded reasons: same-game, live, leg without a game, void, missing leg quote.
- `sync_closes`: leg windows at kickoff and placement; a leg shared by two parlays fetches its kickoff window once.
- Existing singles CLV tests pass against `quotes`.

## Docs

`pnl-rules.md` (parlay CLV; remove from deferred), `data-model.md` (`quotes`, `fair_entry`), `roadmap.md`.
