# Data model (SQLite)

UTC timestamps, money as integer micro-dollars. This is the planned schema. Update it when phase work changes it.

## Tables

### `raw_pages`
- Columns: `id, venue, endpoint, params, fetched_at, body_sha, body_json`.
- Uniqueness: `UNIQUE(venue, endpoint, body_sha)`.
- Append-only. This is the source of truth.
- Identical pages are skipped. A page whose contents shifted (new records arrived) is stored again, so records overlap across pages. **Dedup by record ID (`fill_id`, trade `id`, ...) when normalizing.**
- Also stores Kalshi market records for every traded ticker (endpoint `/markets/{ticker}`, body `{"market": ...}`, from the live or historical tier). Re-fetched each archive run until `finalized`.
- Also stores Kalshi's fee-change history (endpoint `/series/fee_changes`, body `{"series_fee_change_arr": [...]}`), for past line-shop fees.
- Also stores Polymarket market records (`/v1/market/slug/{slug}`, body `{"market": ...}`) for every traded single and parlay leg, re-fetched each run until `MARKET_STATUS_RESOLVED` or 14 days past `gameStartTime`. A 404 isn't stored.
- Also stores balance snapshots (Kalshi `/portfolio/balance`, Polymarket `/v1/account/balances`), one new page per changed balance, starting 2026-10-02.
- Live: `data/sharp_check.db` (gitignored).

### `markets`
- Columns: `venue, market_id, event_id, title, sport, league, market_type, line, game_id, label_yes, label_no, yes_value`. Key: `(venue, market_id)`. Rows for traded markets and every parlay leg. Kalshi legs of parlays with an exit also get their full market record archived (title, final result), for parlay exit spread; other Kalshi legs are bare rows. Classification in `sharp_check/classify.py`.
- `label_yes` / `label_no`: readable side text for the dashboard. Kalshi: `yes_sub_title` (props: title with the player first; parlays: the raw title `yes A,no B`, split by `metrics.labels`). Polymarket: two named sides (teams, over/under, spread) label both; Yes/No markets use `title` (bare player names get line + stat added). `label_no` NULL means show `No: <label_yes>`.
- `yes_value`: final YES value from market data (micro-dollars), NULL until resolved. Kalshi: finalized `settlement_value_dollars`. Polymarket: `outcomePrices[0]` once resolved; a PM parlay is 0 once any leg lost, ONE once all won, else NULL. Used to judge cash-outs (wins vs expected, cash-out value). **Not cash**: `settlements` drives P&L and reconcile.
- `market_type` is one of `moneyline | spread | total | prop | future | parlay | other`; NULL for Polymarket parlay legs without a market lookup (trades carry no type). Only full-game lines are moneyline/spread/total (a 3-way Draw side is moneyline); period lines, team totals and exact score are `other` (`classify.PM_OTHER`; normalize prints any other Polymarket `other` type). BTTS is a prop on both venues.
- **Many bets are player props, not game outcomes** (2026-10-02: about 10 of 36 Kalshi singles, e.g. `KXNFLANYTD`, `KXNFLFIRSTTD`, `KXNFLPASSYDS`; 17 of 36 Polymarket singles, prefix `astatc-`). Parlay legs mix props, game lines, futures and non-sports. Props still belong to a game, so they get a start time and a live/pregame flag in phase 3. Classify by Kalshi series / Polymarket slug prefix, never assume a market is a game line.

### `parlay_legs`
- Columns: `venue, market_id, leg_market_id, leg_outcome`.
- Key: `(venue, market_id, leg_market_id)`. `leg_outcome` is `yes|no`.
- Sources: Kalshi `mve_selected_legs` from archived market records, Polymarket `comboLegDetails` from trades (every fill of a parlay repeats them).

### `fills`
- Columns: `venue, fill_id, order_id, market_id, outcome, qty, price, fee, is_taker, ts`.
- Every fill is normalized to "acquire `outcome` at `price`". `outcome` is `yes|no` on both venues (Polymarket long = yes). `price` is micro-dollars per contract of that outcome; `qty` is REAL (fractional contracts); negative `fee` = rebate.
- `qty` sums are floats: round (e.g. to 4 decimals) before testing whether a position is flat. Raw sums come out as `-0.0` or 1e-15.
- Built by `sharp_check/normalize.py` (`python -m sharp_check.normalize`), which drops and refills it from `raw_pages`.

### `settlements`
- Columns: `venue, market_id, yes_value, settled_ts`. Key: `(venue, market_id)`.
- `yes_value` = micro-dollars paid per YES contract; NO pays 1,000,000 − `yes_value`. This is a market fact; what the user is paid depends on their position, which `bets` works out.
- Kalshi: `finalized` market records (`settlement_value_dollars`, `settlement_ts`). Polymarket: resolution activities (`market.outcomePrices[0]`, long first; `updateTime`). A Polymarket `side` that disagrees with a 0/1 price fails the rebuild.
- A traded market with no row is open or was exited before settlement.

### `cash_ledger`
- Columns: `venue, txn_id, ts, kind, amount`. Key: `(venue, txn_id, kind)`.
- `kind` is one of `deposit | deposit_fee | withdrawal | bonus | perps_transfer`. `amount` is signed: + means into the account.
- Only movements reflected in the reported balance. Failed Kalshi and rejected Polymarket ones are skipped. Polymarket pending deposits count (already in `currentBalance`); pending withdrawals are skipped until completed (held as a reservation). Any other status, or an unknown Polymarket balance-change type, fails the rebuild.
- `perps_transfer`: Kalshi moves between predictions and perps (perps are out of scope, so this is cash leaving or entering). Pending transfers count, because the reported balance already includes them.
- `data/manual_cash.csv` (gitignored, columns `venue,date,kind,amount,note`) adds cash the APIs don't expose. Kind `bonus` is booked; `perps_bonus` is kept for the record and skipped. Tests never read it; only the CLI passes its path.
- Polymarket `REFERRAL_BONUS` = incentive credit released as positions change. It's cash in, not betting P&L.

### `games`
- Columns: `game_id, venue, league, title, start_time`. Per venue: `kalshi:<milestone id>` (archived `/milestones`) or `polymarket:<event slug>` (market `gameStartTime`, leg `eventStartTime`). Cross-venue identity (`odds_event_id`, Sportradar IDs) is phase 5.

### `team_aliases` and `match_overrides` (phase 5)
- `team_aliases`: `league, alias, canonical`.
- `match_overrides`: `venue, market_id, game_id`.

### `quotes`
- Columns: `venue, market_id, at_time, quote_ts, yes_bid, yes_ask, yes_mid`. Key: `(venue, market_id, at_time)`. Renamed from `closing_lines` 2026-10-03 (rebuild drops the old table).
- The venue's own YES quote at `at_time` (whole seconds): the latest point in `[at − 60 min, at]` with `0 < bid <= ask < 1`. No row if there's none.
- `at_time` is a game start (single closes and parlay-leg closes) or a parlay's placement floored to the second (parlay fair entry).
- Built from `raw_pages` endpoints `/markets/{ticker}/candlesticks` (Kalshi) and `/v1/price-history` (Polymarket), archived by `archive.sync_closes` once per (market, time); a response with no points isn't stored and is retried next run. Their body is the API response plus `market` and `start_ts` (Unix s, the `at_time`): `raw_pages` dedups on body alone and price-history doesn't name its symbol. The latest page per key wins.
- Phase 8a adds fill-time pages (`archive.spread_targets`): every single's fill and every eligible parlay exit's legs at `archive.fill_key`. Empty Kalshi exit-leg pages are stored (no candles = market closed); every other empty response is retried. `quotes` rows are only for closes and placements; fills read every page of their market merged (`normalize.price_index`).
- Phase 5 Pinnacle closes are keyed by game and line, so they'll get their own table.

### `fill_costs`
- Columns: `venue, fill_id, bet_id, role, qty, mid_before, cost, note`. Key: `(venue, fill_id, bet_id)`: a fill that flips a position has an exit row and an entry row.
- `mid_before` = mid of the fill's `outcome` just before it, `cost` = `price − mid_before` per contract (micro-dollars); both NULL when not computable, with the reason in `note` (a priced parlay exit notes "N legs decided"). Rules in pnl-rules (Spread). Built by `normalize.spread_costs` from the `derive_bets` allocations.

### `line_shop` (phase 8b)
- Columns: `venue, bet_id, other_venue, other_market, other_side, is_live, qty, yours, theirs, their_ask, their_fee, gap, note`. Key: `bet_id`. One row per eligible single (`archive.LINE_SHOP_ELIGIBLE`: game-linked moneyline/spread/total/prop). Money in micros per contract; `gap` = `yours − theirs`; `theirs` NULL with the reason in `note`. Built by `normalize.line_shop_rows` from `archive.line_shop_counterparts`. Rules in pnl-rules (Line shop).
- Archived by `archive.sync_line_shop`, once per game after it starts; each body is the API response plus `query` (its params), so empty answers are remembered: Polymarket `/v1/events` by `{sportradarGameId}` (full event, for Kalshi bets) or `{slug, marketTypes: moneyline}` then `{slug}` (for Polymarket bets' `sportradarGameId`); Kalshi `/markets` by `{event_ticker}` (the counterpart's event); Kalshi `/milestones` by `{related_event_ticker}` (the usual shape) or an empty `{sportradarGameId}` marker when Kalshi has no such game. Quote windows at the other venue's fill key go through `sync_closes` like 8a's.

### `bets`
- Columns: `bet_id, venue, market_id, outcome, is_parlay, game_id, opened_ts, is_live, entry_qty, avg_entry, stake, exit_qty, exit_proceeds, exit_fees, payout, realized_pnl, status, start_time, link, sport, market_type`.
- Derived by `normalize.derive_bets`. Rebuilt from the tables above and never edited by hand.
- A bet is one position episode: from the first entry until the position is flat or settled. `bet_id` = `venue:market_id:n`.
- An opposite-side fill is an exit at `1 - price`. If it overshoots flat, the remainder opens a new bet on the other side (fee split by qty).
- `closed_ts` (phase 6): settlement `settled_ts` for settled/void, the last exit fill for `closed_early`, NULL while open. Always `iso_us` format (Kalshi raw timestamps sometimes have 5 fractional digits).
- `exit_proceeds` is gross; `realized_pnl` = `exit_proceeds + payout - stake - exit_fees`, NULL while open.
- `status` is one of `open | closed_early | settled | void` (void = settled at a non-0/1 price).
- `close_mid, clv, clv_usd` (phase 4): your side's close mid, `close_mid − avg_entry` per contract, and `entry_qty × clv`. `clv_net` = `close_mid − stake / entry_qty` (after entry fees), `clv_move` = your side's close ask − `avg_entry` (NO ask = 1 − YES bid). `fair_entry` (parlays only) = Π leg mids at placement. `entry_spread_usd` / `exit_spread_usd` (phase 8a) = Σ qty × cost over the bet's entry / exit rows in `fill_costs`; NULL if any is unpriced (exit also NULL with no exits). For eligible parlays (`archive.parlay_clv_legs`), `close_mid` = Π leg mids at each leg's kickoff and `clv_move` stays NULL. Singles: set only for CLV-eligible bets (`archive.CLV_ELIGIBLE`) that aren't void and have a closing line.
- `link`: `game | no_start | unlinked` (`normalize.link_rule`). Futures and all-future parlays are `no_start`. Live = first fill at or after `start_time`. A parlay is live if any game leg had started; otherwise `unlinked` if any game leg has no game. `start_time` is the earliest known game leg. Parlay `sport` is the legs' shared sport or `multi`.
- Derived tables are dropped and recreated on every rebuild, so schema changes apply without migrations.
- `reconcile` rebuilds cash from bets as well as fills; both must match. Checked against 5 app screenshots 2026-10-03.
