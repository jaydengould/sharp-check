# Phase 8b: cross-venue line shopping (design, 2026-10-05)

## Goal

Two features on one matcher:

1. **Live checker** (new "Line shop" tab): pick an upcoming game both venues list and see, per outcome, Kalshi all-in vs Polymarket all-in, the cheaper venue, and a thin-book flag. For deciding where to place a bet.
2. **Backward-looking table** (`line_shop`, on Habits): for past singles, your actual all-in price vs the other venue's all-in at the same minute, with cents and dollars left on the table and coverage.

Done when the Line shop tab shows matched outcomes with all-in prices for a live NFL game on both venues, and Habits shows the backward-looking table for singles on registry markets both venues list, with priced x of y and every unmatched single listed with a reason on Data health.

## Decisions (2026-10-05)

- 8b changes from "measure past bets only" to "live checker plus backward-looking table". The user places bets from their phone but carries the laptop; laptop-only is accepted.
- No host: the checker runs inside the local Dash app, fetching on demand. Read-only market-data GETs only. It stores nothing.
- Parlays are out of scope (the venues build combos differently; nothing to compare). So are alerts, phone access and walking the book.
- Checker leagues (decided 2026-10-05): NFL, college football, MLB, as a league list in code next to the prop registry. The backward-looking table isn't limited by league (its games come from your own bets).
- Checker shows pregame games only (decided 2026-10-05): in-play prices move too fast to check on the laptop first. The backward-looking table still reports in-play bets (medians, see below).
- Markets: full-game moneyline, spread, total, plus NFL props: anytime TD, first TD, rushing yards, receiving yards, scrimmage yards. Prop types live in a registry in code (one entry per type); adding or dropping a type is an entry edit. No settings screen.
- Price compared = taker all-in (ask + fee). Every fill so far has been a taker fill.
- Depth: best ask plus size at it; flagged "thin" below the usual stake (one constant, $10 = median single stake). Book walking is deferred until stakes grow and thin flags keep firing.
- Layout: a slate of games, then pick one (cheapest on API calls; you already know the game you want).
- Archive full Polymarket event pages raw for the backward-looking table (~3 MB per NFL game, ~300–400 MB/year at the current pace). Gzip these pages if the DB passes ~1 GB or rebuilds get slow (gzip is ~35×).

## Changed while building (2026-10-05)

- Polymarket prices come from the event page's `bestBidQuote`/`bestAskQuote` (one call per game), and the live fee from each market's `feeCoefficient` (the date schedule is for past bets only). Its gateway throttles after ~5 quick calls, so book sizes (thin flag) are fetched only for rows where Polymarket is cheaper, at most 10 per game view.
- Game lists: Polymarket `/v1/events?tagSlug=&marketTypes=moneyline&startTimeMin/Max=`; Kalshi `/milestones?competition=`. Past Polymarket bets find their Kalshi game through `sportradarGameId`.
- Extra reason `not fetched yet`; neutral-site games listed in a different team order are dropped by the side check.

## Findings (2026-10-05 spike, throwaway, read-only)

- **Game identity:** Polymarket event `sportradarGameId` equals one of the Kalshi milestone's `source_ids` (`source_3_id` on NFL, e.g. NYJ @ CHI `62a57ae8-…`; soccer uses `sr:sport_event:N`). Exact join, no team-name matching.
- **Polymarket US has events back to 2025-11** (slug prefix `aec-`), with 1-minute price history. Until about September 2026 those events carry the moneyline market only; recent events carry every market (~680 on an NFL game).
- `GET gateway/v1/events` filters by `startDateMin`/`startDateMax`, but each listed event embeds all its markets (28 MB per 100 events). `GET /milestones` filters by `minimum_start_date`/`maximum_start_date`, but returns every category, so a window pages slowly.
- Polymarket price history `longPrice`/`shortPrice` are the YES and NO asks, so both venues give asks at a past minute; neither gives past depth.
- Taker fee coefficient θ (fee ÷ (contracts × p × (1−p))) from your fills: Kalshi 0.07 throughout (higher effective rates are cent rounding on small orders); Polymarket ~0.05 (June), ~0.06 (July–September), ~0.07 (October), with a few 0.0 fills.
- Coverage of past singles (65 on moneyline/spread/total/prop; spike matcher, game lines with an other-venue quote at the fill):

  | Your venue | Game lines pregame | Game lines in play | Props |
  |---|---|---|---|
  | Polymarket | 9 of 10 priced | 7 of 7 | 19: game found for all; 11 are NFL registry types (8 pregame) |
  | Kalshi | 5 of 11 (3 no Polymarket game, 3 spreads/totals from the moneyline-only era) | 5 of 8 (3 no game) | 10: game found; none pairable (moneyline-only era, or passing yards) |

  Expected backward-looking coverage: ~22 pregame (14 game lines + ~8 NFL props) and ~15 in play. The spike's prop pairing was throwaway and paired none; a direct check found every registry type on Kalshi with matching player names (below). It also mapped a Polymarket team-to-advance and a team moneyline to Kalshi's tie market, so side mapping needs the tests below.
- Kalshi NFL prop series: anytime TD = `KXNFLTD` "1+" (2026; ladder 1+..4+, so Polymarket `td-…-gte2` pairs with "2+") and `KXNFLANYTD` (2025); first TD `KXNFLFIRSTTD`; rushing `KXNFLRSHYDS`; receiving `KXNFLRECYDS`; scrimmage `KXNFLRRYDS` ("Rush and Receiving Yards"). Thresholds are `N+` with `floor_strike` N − 0.5 (Swift receiving "25+" ↔ Polymarket `recyd-dswi-gte25`).

## Matching (`sharp_check/match.py`, pure, no network)

**Games:** Kalshi milestone ↔ Polymarket event where the event's `sportradarGameId` is one of the milestone's `source_ids` values.

**Outcomes** (the unit compared, since the venues split markets differently):

| Type | Kalshi | Polymarket | Outcomes |
|---|---|---|---|
| Moneyline | one market per team (+ tie in soccer); back a team = its YES | one two-sided market, side 0 = YES | each team (+ draw) |
| Spread | "X wins by over N" per team | "X −N" two-sided | X −N; Y +N (= NO on Kalshi's X market) |
| Total | "Over N" | Over/Under sides | Over N, Under N |
| Anytime TD (and 2+) | `KXNFLTD` "N+" (2026), `KXNFLANYTD` (2025) | `td-…-gteN` | Yes / No |
| First TD | `KXNFLFIRSTTD` | `firsttd` | Yes / No |
| Rush / rec / scrimmage yds | `N+` thresholds | `gteN` | same player, same N |

- Lines match exactly or not at all.
- Kalshi NO ask = 1 − YES bid; Polymarket NO ask = short ask.
- Player names normalized (accents, punctuation, Jr./Sr./II/III dropped) and must match within the game; zero or several matches → skipped.
- Only outcomes listed on both venues are shown or compared.
- **Prop registry:** per type, the Kalshi series (one or more), the Polymarket slug token / `sportsMarketType`, and the line mapping (`N+` ↔ `gteN`). Verify while planning that Polymarket `scrimmage_yards` means rush + receiving yards, like `KXNFLRRYDS`.

## All-in price and depth

- All-in per contract = ask + taker fee, fee = θ × p × (1−p).
  - Kalshi θ = 0.07, rounded up to the cent per order at the usual stake.
  - Polymarket θ from a date-keyed schedule (~0.05 / 0.06 / 0.07). Confirm change dates against the Polymarket US changelog and your fills while planning; if they disagree, fills win and the gap is noted.
  - Fee-free markets get the normal fee unless market data marks them fee-free (check whether either API exposes it).
- Live checker, per outcome: Kalshi all-in, Polymarket all-in, gap in ¢, cheaper venue highlighted; "no clear edge" when the gap is under 0.5¢.
- Thin flag: size at the best ask in dollars; below `USUAL_STAKE` ($10) → "thin: $X at this price". The price is still compared.

## Backward-looking table (`line_shop`)

- **Eligible:** singles on a registry market type whose game and outcome were found on the other venue. Every other single on a registry type gets a reason: `game not on other venue`, `no matching line`, `prop not listed`, `no quote`, `stale quote`.
- **Per bet:**
  - yours = stake ÷ entry qty (actual fees);
  - theirs = other venue's ask for your outcome at your first fill's quote key + modeled fee (schedule in force at the fill);
  - gap = yours − theirs, ¢ per contract (positive = the other venue was cheaper);
  - $ left = gap × entry qty when positive; a negative gap is shown as "you picked the better venue".
- **Quote rule:** 8a's: key = fill minute (Polymarket one minute earlier); the latest point at or before the key decides; two-sided and ≤ 5 min old, no fallback.
- **Pregame** bets are summed: $ left, mean gap with 95% CI, other venue cheaper on x of y. **In-play** bets are shown as median gap per venue, out of the totals (a score inside the quote minute swamps the comparison, as with the spread rule).
- Entries only; cash-outs are covered by 8a's exit spread.
- Shown on Habits next to spread; unmatched singles with reasons on Data health.

## Archive

For each eligible single, `archive` stores (once per game; past games are never re-fetched):

- the other venue's game record: Kalshi `/milestones` page (already the stored shape) or Polymarket `/v1/events/slug/{slug}` (full page, raw);
- Kalshi `/markets?event_ticker=` for the paired event only;
- the other venue's quote window at the fill key (`fetch_close`, as in 8a; empty pages retried).

Game discovery (date-window listings) is not stored: the Polymarket listing embeds every market. `normalize` builds `line_shop` from these pages, so it rebuilds from `raw_pages`.

## Live checker tab

- **Slate:** games in the checker leagues starting in the next 48 h (not yet started) and listed on both venues, grouped by league with kickoff; loaded on tab open, cached in memory 10 min, Refresh button.
- **Game:** tap → fetch that game's markets on both venues → table, one row per outcome: outcome, Kalshi all-in, Polymarket all-in, gap, thin flag; game lines first, then props by type. "Prices as of hh:mm:ss" and Refresh. No auto-refresh.
- **Load risks, resolve while planning:**
  1. Polymarket listing size (28 MB per 100 events). Look for a league/series filter or a parameter that omits markets; fallback: list per checker league.
  2. Kalshi milestones paging. Look for a competition/category filter; fallback: list game events by series (`KXNFLGAME`, `KXMLBGAME`, …).
  3. Depth: check whether market lists carry size at the best ask; else fetch order books for matched outcomes only, a few at a time within rate limits (a prop-heavy game may take a few seconds).
- **Errors:** one venue failing or timing out → show the other's prices with "<venue> unavailable", no comparison. No asks → "no offer". The checker never writes to the DB.

## Testing (no network)

- `match.py` on small saved public market fixtures: side mapping per type, exact lines, name normalization, ambiguous names skipped. Public market data only; no account data in these fixtures.
- Fee functions against your real fills: the model reproduces the fee paid within cent rounding.
- `line_shop` build on a fake DB: eligible, each reason, pregame vs in-play split, sums.
- Line shop tab in headless Chrome at phone and desktop width (as in phase 7).

## Docs to update when built

`docs/data-model.md` (`line_shop`, new archived endpoints), `docs/pnl-rules.md` (line-shop rule), `docs/kalshi.md` / `docs/polymarket-us.md` (game join, listings, fee schedule), `docs/roadmap.md` (8b redefined, exit check, results).
