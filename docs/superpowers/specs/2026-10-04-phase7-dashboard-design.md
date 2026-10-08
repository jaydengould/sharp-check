# Phase 7: dashboard design and layout (design, 2026-10-04)

## Goal

Turn the one long scroll (six sections, ~15 plain tables, bet IDs as labels) into a tabbed dashboard that answers three questions: am I any good (verdict), how did recent bets go (recent), what should I change (diagnosis). Exit check (roadmap): headline P&L and CLV readable at a glance without scrolling; every section reachable in one click; no horizontal scroll at phone width.

Layout only: no metric changes. One ingestion addition (Polymarket parlay-leg lookups) is folded in because readable bet labels depend on it.

## Findings that shaped the design (2026-10-04)

- Headline today: 149 closed bets; single CLV after fees −1.3¢ (CI −2.0 to −0.6, n 44); parlay −1.6¢ (CI −2.2 to −1.1, n 40); wins 18 / 124 settled vs 29.1 expected (CI 20.6–37.6).
- Bet labels: Polymarket singles already carry a readable market (`title` "Kyle Monangai 1+ touchdowns", `metadata.playerName`). Kalshi parlay markets list every leg in `title` ("yes Joey Cantillo: 8+,yes Payton Tolle: 8+"). Polymarket parlay legs (`comboLegDetails`) carry only the game title, team and a slug like `astatc-nfl-nyj-chi-2026-10-04-td-dswi-gte1`; the player and stat need a `/v1/market/slug/{slug}` lookup per leg.

## Decisions

- Five tabs: **Overview**, **CLV**, **Performance**, **Habits**, **Data health**. Overview answers verdict and recent together, so a good day reads next to CLV rather than as a results scoreboard; diagnosis lives in the other tabs.
- Overview headline shows **betting P&L only**; own-money P&L moves to Performance.
- A **Both | Kalshi | Polymarket** switch drives the Overview headline tiles only. Detail tabs keep their per-venue rows (no global venue filter: segments, per-bet tables and habits don't take a venue and bankroll is combined by definition).
- Build: plain Dash + `assets/` (option 1). No new dependencies. Sortable tables (`DataTable`/AG Grid) are a maybe-later, revisit when per-bet tables get long (~150 bets).
- Style: "Quiet ledger": neutral, low colour, tabular numerals; green/red only on signed values.
- Themes: System (default; follows OS between Light and Dark), Light, Dark, Blue, Bears, Giants. A theme changes surfaces and the accent only. Accent marks location (active tab, selected venue), never a number. Gain/loss green/red are tuned per theme and never take the accent colour (Bears and Giants accents are orange, next to loss red). Colours only, no logos.
- Bet labels everywhere: readable market text, never raw IDs (IDs stay in Data health lists where they identify a record to look up).

## Data

### Polymarket parlay-leg lookups (`archive.py`)

`archive_pm_markets` already fetches `/v1/market/slug/{slug}` for singles. Extend the slug set with every `comboLegDetails[].slug`, under the same "done" rule (stored, and game start passed or none). Read-only GETs, archived in `raw_pages`. One-time backfill of every past leg, then only new ones.

### Market labels (`normalize.py`)

New column `markets.label TEXT`: the venue's short text for the YES side, or NULL if none.

| Market | `label` |
|---|---|
| Polymarket single or leg with a stored lookup | market `title` (fallback `question`) |
| Polymarket leg without a lookup | NULL |
| Kalshi market | `yes_sub_title` |
| Kalshi parlay (`KXMVE*`) | `title` (comma-separated legs, `yes ` prefixes dropped) |

Polymarket legs with a stored lookup also take its `sportsMarketType` through `classify.pm_market`, so `market_type` is no longer NULL. This fixes the latent issue "PM parlay legs have NULL `market_type`, so a futures leg would count as a game leg" (`link_bets` skips `future` legs). Other headline numbers must be unchanged; any difference is explained in the plan's verification step.

Verify on real data during the build: print every distinct label and eyeball it. If a market type reads badly (e.g. a Kalshi `yes_sub_title` that is only a team name for a total), adjust the rule in the table above rather than special-casing in the UI.

### Queries (`metrics.py`)

- `bet_label(conn, bet_id) -> (label, legs)`. Single: market `label` (fallback market title, then market ID), prefixed `No: ` when the outcome is `no`. Parlay: `"N-leg parlay"`, `legs` = leg labels (Kalshi from the parlay title; Polymarket from leg markets, falling back to the leg's game title), each prefixed `No: ` for a no-side leg.
- `recent_bets(conn, n=10)`: the n most recently closed bets (`closed_ts` desc), with venue, label, result (`won`, `lost`, `cashed out`, `void`, plus `live`), stake, CLV after fees (singles: `clv_net`; parlays: parlay CLV after fees; empty when none, with the reason "live" when live), realized P&L.
- `open_bets(conn)`: open bets (`status = 'open'`), placed time, label, venue, start time, stake, average entry.

## Page

### Header

`sharp-check` · last sync (newest `raw_pages.fetched_at`, UTC) · ✓ when both venues reconcile (both `fills` and `bets` checks within tolerance), ✗ otherwise (✗ links to Data health) · theme picker.

### Tabs

1. **Overview**
   - Venue switch, then five tiles: betting P&L (closed bets), ROI (staked), CLV after fees for singles (CI, n), CLV after fees for parlays (CI, n), wins vs expected (CI).
   - Open positions (count and cost at top).
   - Recent bets: last 10 closed (closed time, label with parlay legs underneath, venue, result, stake, CLV after fees, P&L).
   - Bankroll chart (existing, themed).
2. **CLV**: singles summary (venue rows) and explanation; parlay summary and explanation; per-bet singles and parlay tables each collapsed in a native `<details>` ("Show all N bets").
3. **Performance**: betting P&L table (venue × all/singles/parlays); own-money P&L; segments (hidden when n < 30).
4. **Habits**: bankroll chart; stake after win/loss; stake % as one sentence (median, p90, max) with the top-5 table in `<details>`; timing buckets; execution cost and maker saving.
5. **Data health**: reconcile per venue; linkage counts and unlinked bets; bets with no close; parlay CLV exclusions with reasons; bankroll end-point check; `market_type other` codes (today `atc`, `tsc`).

Nothing is dropped; every number on the current page appears in some tab.

### Phone (≤ 600px)

- Tiles wrap two per row; betting P&L spans the full row with ROI in its subtitle (the separate ROI tile hides).
- Tab labels shorten (`Perf.`, `Data`), venue switch shows `PM`.
- Tables mark minor columns with a `minor` class hidden on phone. Recent bets become Bet | CLV | P&L, with date and result under the label. Tables that still can't fit scroll inside their own container, never the page.

## Build

- `sharp_check/app.py`
  - `dcc.Tabs` with the five tabs; existing section functions regrouped.
  - Venue switch: `dcc.RadioItems` styled as a segmented control; one callback renders the tiles for the chosen scope.
  - `html.Details`/`html.Summary` for collapsed tables.
  - Expected ~350 lines; split only past ~500.
- `sharp_check/assets/style.css` (Dash serves `assets/` automatically)
  - All colours as variables on `:root`; `[data-theme=...]` blocks override them; System = Light, plus Dark under `prefers-color-scheme: dark`.
  - Tokens: `--bg`, `--card`, `--fg`, `--muted`, `--line`, `--accent`, `--on-accent`, `--pos`, `--neg`, `--series-1`, `--series-2`.
  - Tile grid, tables (tabular numerals, right-aligned numbers, hairline rows), tabs, segmented control, phone rules.
- `sharp_check/assets/theme.js`
  - Picker sets `data-theme` on `<html>` and saves to `localStorage` (wrapped in try/catch; falls back to System).
  - Applied before first paint where possible to avoid a flash.
- Chart theming: Plotly doesn't read CSS. A clientside callback on theme change restyles the figure (font, grid, zero line, series colours, transparent backgrounds) from the computed CSS variables.

Starting palettes (from the approved mockup; refine with the `dataviz` validator during the build):

| Theme | bg | card | fg | muted | line | accent | pos | neg |
|---|---|---|---|---|---|---|---|---|
| Light | #fbfaf8 | #ffffff | #1c1b19 | #77736b | #e8e5df | #1c1b19 | #2f7a4b | #b4372f |
| Dark | #141413 | #1c1c1a | #ecebe7 | #9a978f | #2b2a27 | #ecebe7 | #5cc58a | #f07a6e |
| Blue | #0e1a2b | #132339 | #e7eef8 | #8fa3bd | #1f3350 | #4b8ff0 | #5fd39a | #ff8577 |
| Bears | #0b162a | #111f38 | #eef1f6 | #93a0b5 | #1c2c4a | #c83803 | #5fd39a | #ff8a8a |
| Giants | #121212 | #1b1a19 | #efe6d6 | #a39a8b | #2c2a27 | #fd5a1e | #5fd39a | #ff8a8a |

## Testing

No network, alongside the existing suite.

- Leg lookup: a scrubbed fixture for one Polymarket leg market; normalize gives the leg its `label` and `market_type`.
- `bet_label`: Polymarket single, Polymarket parlay (one leg with lookup, one without, one no-side), Kalshi single, Kalshi parlay.
- `recent_bets`/`open_bets`: order, limit, open bets excluded from recent.
- Smoke render: build the layout on a small in-memory DB; all five tabs and five tiles present; the venue callback returns tiles for each scope.

## Exit check (real data, in Chrome, screenshots)

1. Desktop: the five headline tiles are visible without scrolling.
2. Each tab one click from any other.
3. 375px: no horizontal page scroll on any tab, in every theme.
4. Every theme: green/red legible, chart restyles on switch.

## After the build

- Read-only subagent logic review, then fix findings.
- Docs: roadmap (phase 7 done, current phase, resume point leading with the wins-vs-expected check below), `docs/polymarket-us.md` (leg lookup), `docs/data-model.md` (`markets.label`), latent issues list (PM leg `market_type` fixed).

## Out of scope (noted for later)

- **Wins vs expected below its CI** (18 vs 29.1, CI 20.6–37.6): a cold run or a bug in expected wins. First thing to check after phase 7.
- Sortable tables (DataTable/AG Grid).
- Global venue filter for detail tabs.
- Classifying `atc`/`tsc` market codes.

## Added after approval (2026-10-04, user-approved; details in the plan)

- **Wins vs expected judges cash-outs by how their market finished** (would it have won if held), not settled-only. Settled-only was biased low because cash-outs are mostly winning positions: 18 vs 29.1 expected (below the CI) became 30 vs 36.8 (CI 27.4–46.2). Final results live on `markets.yes_value`, never in `settlements`. Polymarket lookups are re-fetched until resolved (give up 14 days after start). Two-sided PM markets label both sides (`label_no`).
- **Cash-out hindsight metric** (Habits tab): what you got for the part you sold vs what holding it would have paid. Spread cost at exit is later, with entry spread.
- **Venue switch drives the whole Overview** (user, after the build): tiles, open positions, recent bets and the bankroll chart (per-venue bankroll = that venue's ledger cash + closed-bet P&L; its end point matches that venue's cash + open cost). Detail tabs keep per-venue rows; the Habits bankroll stays combined.
- **CLV tab venue switch** (user, after the build): filters the per-bet singles and parlay lists (an open list stays open); the summary tables keep their per-venue rows.
- **Bets tab** (user, after the build): every bet, newest first, with venue and singles/parlays switches and a text search over label, legs, venue, type and result. 60 of 149 closed bets (live, futures, voids, ineligible parlays) had no individual listing before.
