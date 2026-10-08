# Phase 3: game linkage and segments (design, 2026-10-03)

## Goal

Give every bet a game start time (or say why it has none), flag it live or pregame, classify it by sport and market type, and show the segments that have enough bets to mean something. Start times also feed phase 4 CLV.

Scope is option A: linkage and classification done properly, but only three segment dimensions (live/pregame, sport, market type). The full grid (league, line, ...) waits for more volume.

## Findings that shaped the design

- Kalshi market `occurrence_datetime` is **not** kickoff. For ATL @ GB it is 03:15Z; kickoff was 00:15Z (about expected end). It is null on LAC @ CHI.
- Kalshi `GET /milestones?related_event_ticker=<event>` (public) returns the game with a correct `start_date`. Verified on ATL @ GB (00:15Z) and LAR @ CHI (23:30Z). The `event_ticker` filter is ignored and returns unrelated milestones.
- Coverage on 2026-10-03: 132 of 154 traded Kalshi events (singles and parlay legs) hit a milestone directly. Player-prop events miss, but their game code matches the game event: `KXNFLTD-26SEP24ATLGB` → `KXNFLGAME-26SEP24ATLGB`. That recovers all props except the Super Bowl LX game (`26FEB08SEANE`, 2 parlay legs). The rest are futures or non-game markets.
- Milestone `type` is unreliable: `soccer_tournament_multi_leg` is used for single World Cup matches and `hockey_tournament` for an NHL game total, both with correct kickoffs. So **game vs future is decided by market classification (below), never by milestone type**: a market classified moneyline/spread/total/prop/other is a game market and takes its milestone's start; a `future` takes none even if a milestone exists (golf, World Series, Oscars).
- Polymarket: every parlay leg in archived trades has `eventStartTime`; every single has an `eventSlug`, and `gateway/v1/events/slug/{slug}` gives `startTime` plus `sportsMarketType` and `line` for each market.

## Data flow

### Archive (`archive.py`)

Read-only, public endpoints, stored in `raw_pages` like everything else.

- Kalshi: one milestone lookup per traded event (singles and parlay legs). An event with no milestone whose series starts `KX{NFL|MLB|NBA|WC}` and whose second ticker segment is a game code (`\d\d[A-Z]{3}\d\d...`) is retried as `KX{league}GAME-<code>`.
- Polymarket: event by slug for each single's `eventSlug`. Parlay legs need no fetch.
- A game is re-fetched each archive run until its start time has passed (catches postponements), then left alone.

### Normalize (`normalize.py`)

New tables, rebuilt from `raw_pages`:

- `games`: `game_id, venue, league, title, start_time`. Per venue: `game_id` = `kalshi:<milestone id>` or `polymarket:<event slug>`. Cross-venue columns (`odds_event_id`, `sportradar_id`, ...) wait for phase 5.
- `markets`: `venue, market_id, event_id, title, sport, league, market_type, line, game_id`. Status/result stay in `settlements`.

New `bets` columns: `start_time`, `link` (`game | no_start | unlinked`), `sport`, `market_type`. `is_live` and `game_id` get filled.

### Link and live rules

| Case | `link` | `start_time` | `is_live` |
|---|---|---|---|
| Single on a game market | `game` | game start | `opened_ts >= start_time` |
| Single on a future / non-game market | `no_start` | NULL | NULL |
| Single on a game market with no game found | `unlinked` | NULL | NULL |
| Parlay, some linked leg had started at `opened_ts` | `game` | earliest leg start | 1 (even if another leg is unlinked) |
| Parlay, no leg started, no leg unlinked, ≥1 game leg | `game` | earliest leg start | 0 |
| Parlay, no leg started, some leg unlinked | `unlinked` | NULL | NULL |
| Parlay, every leg a future / non-game | `no_start` | NULL | NULL |

Future legs inside a parlay are ignored for timing. A parlay's `game_id` stays NULL (it can span games).

## Classification

- League from the ticker or slug, never the title: Kalshi series prefix (`KXNFL…` → NFL), Polymarket slug segment (`…-nfl-…`). A small dict maps league → sport. A parlay gets its legs' sport if they all share one, else `multi`.
- `market_type`:

| Type | Kalshi series suffix | Polymarket `sportsMarketType` |
|---|---|---|
| moneyline | `GAME`, `ADVANCE` | `*_full_game_winner`, `*_full_time_winner`, `*_to_advance` |
| spread | `SPREAD` | `*_full_game_spread` |
| total | `TOTAL` | game (not team) full-game total |
| prop | `ANYTD`, `FIRSTTD`, `TD`, `REC`, `PASSYDS`, `RECYDS`, `RSHYDS`, `KS`, `PTS`, `GOAL`, `FIRSTGOAL`, `BTTS` | contains `_player_` |
| future | market with no game | event with no game |
| parlay | `KXMVE*` | `caoc-*` |
| other | anything else | period lines, team totals, exact score, race-to, ... |

- Only full-game lines count as moneyline/spread/total, because phase 4 market CLV is scoped to those and period lines close differently.
- `line`: Kalshi `floor_strike`, Polymarket market `line`.
- Unknown series or type → `other`; the normalize CLI prints a count per unknown code. Not a rebuild failure: a wrong label never corrupts money.

## Dashboard

- **Segments** under the headline tables, for live/pregame, sport and market type. Each row uses the headline columns (bets, stake, P&L, ROI, wins vs expected with 95% CI). Rows with fewer than 30 closed bets are hidden, with a line per dimension saying how many were hidden.
- `metrics.headline` gains one generic column = value filter instead of a join per dimension.
- **Linkage panel**: counts by `link`, and a table of every `unlinked` bet (market, opened time, stake).

## Testing

No network, same style as existing tests.

- One test per row of the link/live table, including "unlinked leg but another leg already started → live".
- Classification on a few real tickers/slugs per type, plus an unknown one landing in `other`.
- Sibling-game derivation: `KXNFLTD-26SEP24ATLGB-ATLBROBINSON7` → `KXNFLGAME-26SEP24ATLGB`.
- Fixtures: one milestone and one Polymarket event (trimmed to a few markets) under `tests/fixtures/`. Public data, nothing to scrub.
- Real-data check by query: ATL @ GB pregame, LAC @ CHI live.

## Exit check (reworded)

Every settled bet has a game, is flagged `no_start`, or is listed as `unlinked`.

## Docs to update

- `kalshi.md`: milestones, the ignored `event_ticker` filter, `occurrence_datetime` is not kickoff.
- `data-model.md`: `games`, trimmed `markets`, new `bets` columns.
- `roadmap.md`: exit check wording; phase 3 resume point.

## Out of scope

Cross-venue game identity (phase 5), the full segment grid (option B), a manual game override file, CLV.
