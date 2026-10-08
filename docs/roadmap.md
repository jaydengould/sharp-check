# Roadmap

Build one phase at a time. A phase is done when its exit check passes; update **Current phase** then.

**Current phase: none in progress; 8b done 2026-10-05** (phases 1–4, 6, 7, 8a and 8b plus parlay CLV completed 2026-09-30 to 2026-10-05; phase 5 deferred 2026-10-03; phase 8 split 2026-10-05, see Decisions)

| # | Phase | Exit check |
|---|-------|-----------|
| 1 | Smoke test + raw archive: signed client per venue, page-everything sync into `raw_pages` | Both auth paths return 200; earliest record and total count known per venue; every field for `fills`/`markets` located or confirmed missing |
| 2 | Normalize to `bets` + headline P&L: realized P&L, ROI on stake, actual vs expected wins with CI, open positions separate, parlays vs singles split | P&L for a few known bets matches each venue's app; cash reconciles to each venue's balance within a few cents |
| 3 | Per-venue game linkage: sport, league, market type, line, start time → live/pregame flag, segment views with n and CI (hidden when n < 30) | Every settled bet has a game, is flagged no-start (futures etc.), or is listed as unlinked |
| 4 | Market CLV (free): entry price vs the venue's own price at game start, pulled after the fact | CLV shown for pregame moneylines, spreads/totals and props |
| 5 | Sharp CLV (gated on paying for odds data): match games to odds API events, backfill de-vigged Pinnacle closes. Moneylines always, spreads/totals only on exact line match, soccer de-vigs 3-way | Coverage % shown; unmatched games listed |
| 6 | Habit metrics: stake after loss vs win, time placed before start, stake % of bankroll, bankroll curve, maker vs taker (fees and spread paid) | Views render on real data |
| 7 | Dashboard design and layout: visual style, navigation between sections (tabs or a sidebar), headline numbers up top, readable tables and charts, light/dark, phone width | Headline P&L and CLV readable at a glance without scrolling; every section reachable in one click; no horizontal scroll at phone width |
| 8a | Spread at entry and exit (cash-outs): fill price vs the mid in the minute before each fill | Spread at entry and exit shown per bet and summed per venue, with priced fills x of y; every unpriced fill listed with a reason |
| 8b | Cross-venue line shopping: live checker (Line shop tab) plus a backward-looking table of past singles, on one Sportradar-ID matcher | Line shop shows matched outcomes with all-in prices (ask + fee) for a live NFL game on both venues; Habits shows past singles on registry markets vs the other venue's all-in at the fill, with priced x of y; every unmatched single listed with a reason |

Sync is on demand (CLI or Dash button). No scheduler or always-on host in v1.

## Next up

Every session starts with: `python -m sharp_check.archive && python -m sharp_check.normalize && python -m sharp_check.reconcile`. Archive also rebuilds and fetches closes for newly started games. A reconcile mismatch usually means a new Kalshi reward is missing from `data/manual_cash.csv`.

Phase 4 (done 2026-10-03): all 44 eligible bets have a market close. Mean CLV vs mid −0.0¢ (CI −0.7 to +0.7); after entry fees −1.3¢ (CI −1.95 to −0.6); price move vs close ask +0.8¢.

Phase 6 (done 2026-10-03, spec and plan in `docs/superpowers/`): habits section (bankroll curve with a reconcile check, stake after win/loss, stake % of bankroll, timing buckets, fees with estimated maker saving). On 2026-10-03: median stake the same after wins and losses (no tilt signal); stake % of bankroll mostly reflects funding habits (deposits get bet right away); every fill was a taker fill; estimated maker saving on singles about equal to the single fees paid.

Parlay CLV (done 2026-10-03, spec and plan in `docs/superpowers/`): 40 of 43 eligible cross-game parlays have CLV (3 Kalshi have a wide or stale leg quote). On 2026-10-03: after fees −1.6¢ (CI −2.2 to −1.1), markup +5.2% of fair price (Kalshi 4.1%, Polymarket 7.4%), leg movement +0.1¢. Longshot combos carry much larger relative markups (one Polymarket parlay: 4.8¢ paid for 1.3¢ of legs).

Phase 7 (done 2026-10-04, spec and plan in `docs/superpowers/`): six tabs (Overview, CLV, Performance, Bets, Habits, Data health; Bets = every bet with venue/type filters and search, added after a 60-bet gap: live bets, futures and voids had no list), a Both/Kalshi/Polymarket switch that filters the whole Overview (tiles, open and recent bets, bankroll), readable bet labels, themes System/Light/Dark/Blue/Bears/Giants, phone layout. Added on the way: Polymarket legs are looked up and every PM market is re-fetched until resolved; wins vs expected judges cash-outs by how their market finished (30 vs 36.8, CI 27.4–46.2; settled-only was 18 vs 29.1, biased low); cash-out hindsight value on Habits (positive vs holding but the CI spans zero; almost all of it from parlays). Exit check run in headless Chrome: 5 themes × 2 widths × 5 tabs, no page overflow; some wide reference tables scroll inside their own box on a phone.

Phase 8a (done 2026-10-05, spec and plan in `docs/superpowers/`): `fill_costs` prices every fill against the mid in the minute before it; Habits shows spread by venue × type, pregame vs live, per bet, and exit spread on cash-outs; Data health lists unpriced fills. Fixed while building: singles never fall back past a one-sided latest quote (a decided Kalshi market asks 1.00; the fallback priced a 0.99 sale at −53¢), and Kalshi legs of exited parlays now have archived records (their final results were missing). On 2026-10-05 (Polymarket mostly unpriced, outage): Kalshi singles 48 of 54 fills priced, pregame entry ~+0.9¢ per contract; Polymarket singles 10 of 48. Cash-outs: Kalshi parlay exit spread on all 4 eligible was 0.2–6.4¢ per contract, small next to the parlay hindsight value, so fair-price cash-outs cost little next to that luck; singles exit spread was negative, driven by two live exits (one in-game swing). Per contract by fill time: pregame +0.6¢, in play −0.15¢ (review fix: the split was by bet, which put in-play cash-outs of pregame bets under pregame). After the Polymarket outage ended (same day): Polymarket singles 48 of 48 priced; the exit total is dominated by one in-play exit sold seconds after a goal (Polymarket's key is up to 2 minutes old; YES went 0.53 → 0.06 within that minute). Polymarket parlays: entry 24 of 34 priced, exit on all 4 eligible. Per contract by fill time: pregame +0.62¢, in play +0.68¢. Exit check met: priced x of y per venue × type, 43 unpriced fills listed (35 are parlays with no fair entry). Conclusion: pregame spread is under 1¢ per contract on both venues. In-play singles are now shown as medians: entry +0.5¢ (Kalshi) / +0.75¢ (Polymarket), exit +1.0¢ / +2.0¢ per contract, so a live cash-out typically costs 1–2¢. Cash-out exit spread: 9 priced (8 parlays), 13 live singles left out.

Phase 8b (done 2026-10-05, spec and plan in `docs/superpowers/`): `match.py` pairs outcomes across venues (Sportradar game ID, away/home role, exact lines, normalized player names; 5 NFL prop types in a registry), with a 15¢ side check. Line shop tab: pregame NFL/college football/MLB games in the next 48 h, all-in (ask + fee) per outcome, cheaper venue bold, thin flag. Live check: slate of 8 games in ~5 s; an NFL game (TB @ DAL) gave 321 paired outcomes in ~16 s, Polymarket cheaper on 106, Kalshi on 77, 138 within 0.5¢. Polymarket's gateway throttles after ~5 quick calls, so its prices come from the event page and its book sizes only where it's cheaper. Fee model reproduces all 102 taker fills within 1¢. Past singles (`line_shop`, Habits): 21 pregame + 15 in play priced. Your Polymarket singles: 17 of 26 pregame priced, Kalshi cheaper on 5, mean gap +0.2¢ (CI ±0.34); your Kalshi singles: 4 of 20 priced (Polymarket listed moneylines only before ~September 2026), Polymarket cheaper on 2. In play (median): Kalshi −0.68¢, Polymarket −0.17¢. Conclusion: venue choice cost little so far (pregame within ~1¢; the props are where the gaps were, up to 2¢). On 2026-10-08 past Kalshi fees started following the fee-change history (MLB 0.5 from 2026-08-07): your Polymarket singles' mean gap +0.24¢ (CI ±0.36); in-play MLB rows assume 0.5 too, unverified (may be 1 live). Teams orient by team code (either end matching is enough, so Kalshi INDOSU vs Polymarket ohiost-ind is caught as swapped); when no code matches, the live checker orients by moneyline prices and a past bet without its own mid is left unpriced ("team order unclear").

**Resume here:** run the usual session start. No phase in progress. Options: phase 5 sharp CLV (revisit at ~150 bets with CLV), more prop types in `match.PROPS` as seasons change (MLB player props are classified since 2026-10-08 but not in the line shop; soccer goalscorer), the latent issues below (undecided: a give-up for retried quotes; recommended skip, since it saves ~2 calls a run and a plain age cutoff would block future backfills) (8a review minors are listed there too), theme tweaks.

## Phase 1 smoke test (`scripts/smoke.py`)

Read-only. Prints field names and counts, never values or balances.

- Kalshi:
  - Signed `GET /portfolio/balance`.
  - `/historical/cutoff`.
  - Fills from the live and historical tiers.
  - `/portfolio/settlements`.
  - `/markets/{ticker}`: look for a game start-time field.
  - Look for a deposit history endpoint.
- Polymarket US:
  - Signed `/v1/account/balances`.
  - `/v1/portfolio/activities?limit=5`: test signing with and without the query string.
  - Check trades for side, outcome and fee.
  - Compare against `search-executions` and the position ledger.
  - Event by slug for one traded market.
  - Price history around its start time.
- Page both venues to the end and record the earliest record date and total count.
- Save one scrubbed fixture per endpoint under `tests/fixtures/`.

## Decisions

- Phase 8b redefined (decided 2026-10-05): a live line-shop checker (runs in the local app on demand, no host, stores nothing) plus the backward-looking table. Past-only would rest on ~22 pregame singles; the checker saves on every future single. Parlays out of scope. Spec: `docs/superpowers/specs/2026-10-05-phase8b-line-shop-design.md`.
- Phase 8 split (decided 2026-10-05): 8a spread at entry and exit (reuses the quote machinery, covers every bet), 8b line shopping later (needs cross-venue game matching, which overlaps phase 5). Parlay exits included in 8a (8 of 10 parlay cash-outs are cross-game; they hold most of the cash-out hindsight value).
- In-play singles shown as medians, not summed (decided 2026-10-05, replacing "keep them in the totals"): 22 of 25 cash-outs are in play and three news fills (−34¢, −9¢, +48.5¢) set both venues' singles exit totals, while the other in-play fills sit at 0.5–2¢. Median ¢ per contract is robust to those. Parlays stay summed (leg rules guard them). Rejected: dropping all in-play fills (empties the cash-out spread), quotes after the fill (not archived; needs an archive change for ~3 fills).
- No venue switch on Habits (decided 2026-10-05): cost tables compare venues in rows, and several Habits metrics are cross-venue by definition. Possible follow-up after phase 8 with its own rules.
- Wins vs expected judges cash-outs by their market's final result (decided 2026-10-04): settled-only was biased low because the user mostly cashes out winning positions. Fractional credit at the exit price was rejected (exits are at the bid). Rule in pnl-rules.
- Cash-out analysis (decided 2026-10-04): hindsight value vs holding now (Habits); spread cost at exit later, with entry spread.
- Benchmark for sharp CLV: de-vigged Pinnacle closing line. Secondary: venue price at game start.
- Odds data: free for now. When sharp CLV is worth it, pay one month of historical (The Odds API) and backfill everything. Verify Pinnacle is in historical data before paying. Then choose: paid ~$30/mo vs free-tier snapshotter on a ~$5/mo host.
- Volume is under 200 bets, so segment slices are mostly noise. Headline P&L, expected-vs-actual wins and CLV are the primary outputs.
- Bankroll = account cash + open position cost, both venues combined.
- Kalshi perps are out of scope (decided 2026-10-02): only prediction markets are analysed. Transfers to and from perps are cash movements, not P&L.
- Phase 6 before phase 5 (decided 2026-10-03): with 44 eligible bets, venue CLV after fees (−1.3¢, CI excludes 0) already answers "is there an edge". Pinnacle wouldn't flip that, costs money, and doesn't cover props (24 of 44). Revisit phase 5 at roughly 150 eligible bets. Phase 6 adds maker vs taker because fees plus spread are what turn break-even into a loss.
- Props in CLV (decided 2026-10-03): included in phase 4 market CLV (works on both venues); excluded from phase 5 sharp CLV (thin Pinnacle prop history).
- Parlays (decided 2026-09-30):
  - Tracked like any bet: P&L, ROI, and actual vs. expected wins.
  - Parlays vs. singles is a headline split.
  - Parlay CLV from leg prices for cross-game parlays (added 2026-10-03, see pnl-rules).

## Phase 1 findings (smoke test 2026-09-30)

- Both auth paths work. History goes back to 2025-10-31 on Kalshi (110 fills, 83 markets) and 2026-06-22 on Polymarket US (73 trades).
- Polymarket trades do include side, intent and fee. All four earlier open questions are resolved; see the venue docs.
- About half of all bets are parlays (Kalshi `KXMVE*`, Polymarket `caoc-*`).
- A few non-sports markets exist (e.g. Oscars, politics).
- Raw archive: `python -m sharp_check.archive` pages all account endpoints into `raw_pages`. Re-runs add only changed pages.
- Polymarket price history around game start works (about 1-minute points), so phase 4 is feasible.

## Open questions

None right now.

## Known latent issues (reviews 2026-10-03 and 2026-10-04, none affect current data)

- 8a review minors (2026-10-05): one transient empty Kalshi exit-leg page is kept forever (the leg is then priced at its final result); a Kalshi market that 404s on both tiers is skipped and retried every archive run; missing fill quotes are refetched every archive run with no give-up (2 on 2026-10-08, both Kalshi NFL props from 2025-11-28); parlay exit spread assumes the parlay was bought YES (all are; the user says combos can't be bought NO); ineligible parlays' entry fills say "no fair entry" instead of the eligibility reason; the probe script repeats `fill_key`/`PINNED`.
- A PM leg slug that 404s is retried on every archive run, forever (none today).
- Kalshi parlay leg labels come from the parlay title and can drop the stat ("Bijan Robinson: 1+"); fixing it means archiving every Kalshi leg market.
- System theme follows the OS only at page load and theme change; flipping OS dark mode while the page is open leaves the charts in the old colours until reload.
- Cross-venue games: `game_id` is per venue (no shared game identity until phase 5), so the pooled both-venue CLV CIs don't group a Kalshi and a Polymarket bet on the same game (2026-10-08: 3 games, 2 Kalshi + 1 Polymarket parlay, 1 single). Per-venue CIs and line shop are unaffected. Measured 2026-10-08 with a Sportradar mapping: pooled CIs identical to 3 decimals.
- 8b review minors (2026-10-05): the Kalshi fee model allows fractional contracts (< 1¢ per order); a Polymarket thin size comes from a later `/book` call than the price; the `sync_line_shop` failure line names the bet's venue, not the failing call's; a side with no ask shows "–" not "no offer"; same-name players at different thresholds on one venue aren't dropped as ambiguous (2026-10-08: no collisions in 205 archived Kalshi player names; recheck when adding MLB props).

## Deferred past v1


- Injury and weather dimensions.
- Cash-out calculator, with fair value from the sharp line or a cross-venue price, never from the same market.
- Logging my own probability per bet for Brier score.
