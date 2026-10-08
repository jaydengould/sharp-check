# Phase 4: market CLV (design, 2026-10-03)

## Goal

For every pregame single on a game, compare the entry price with the same venue's mid price at game start, and show it per bet and in aggregate. Exit check: CLV shown for pregame moneylines, spreads/totals and props on both venues; the "no close" list is short and explained.

No hosting: closes are pulled after the fact on each `archive` run.

## Decisions

- **Close = mid at kickoff**: (yes_bid + yes_ask) / 2 from the last usable quote at or before `bets.start_time`. Bid and ask are stored too.
- **Headline = CLV $**: Σ `entry_qty × (close_mid − avg_entry)` over bets with a close, shown next to realized P&L on the same bets, mean CLV in ¢ with a 95% CI, and the share of bets beating the close.
- **Fetch order**: `archive` runs `normalize.rebuild` before fetching closes, so eligibility is defined once (in `bets`) and a new bet gets its close in the same run.
- Volume is small (44 eligible bets on 2026-10-03), so no per-segment CLV slices; they'd all be hidden under the n < 30 rule.

## Findings (probed 2026-10-03)

- **Kalshi:** `GET /series/{series}/markets/{ticker}/candlesticks?start_ts&end_ts&period_interval=1&include_latest_before_start=true`. Series = ticker prefix before the first `-`. A 404 means the market is past the historical cutoff: use `GET /historical/markets/{ticker}/candlesticks` with the same params. 20 of 20 eligible bets have candles (18 historical, 2 live), back to 2025-11-01.
  - Field names differ by tier: historical uses `yes_bid.close`, `yes_ask.close`; live uses `yes_bid.close_dollars`, `yes_ask.close_dollars`. Both are dollar strings.
  - `end_period_ts` is the inclusive end of the minute. The synthetic `include_latest_before_start` candle has null OHLC and is ignored.
  - Spreads can be wide at kickoff (0.42 / 0.49 on a college spread).
- **Polymarket:** `GET gateway/v1/price-history?symbol=<slug>&fidelity=1&timestamp.startTimestamp=<s>&timestamp.endTimestamp=<s>`. The nested `timestamp.` param names are required; the bare names in the docs return 400 `code 3`. Body is `{"history": [{"timestamp", "longPrice", "shortPrice"}]}`, both ask prices. YES bid = 1 − `shortPrice`, YES ask = `longPrice`. 24 of 24 eligible bets have points, back to 2026-06-22. The body doesn't name the symbol.

## Data flow

### Archive (`archive.py`): `sync_closes(conn, kalshi_get, gateway_get, now)`

Runs last in `__main__`, after `normalize.rebuild(conn, manual_path)`.

- Eligible: `bets` with `is_parlay = 0`, `link = 'game'`, `is_live = 0`, `market_type IN ('moneyline','spread','total','prop')`, `start_time <= now − 5 min`.
- One fetch per distinct `(venue, market_id, start_time)`, window `[start − 3600 s, start]`, 1-minute resolution.
- Stored endpoints: Kalshi `/markets/{ticker}/candlesticks`, Polymarket `/v1/price-history`. Params `{"market": id, "start_ts": s}`.
- `raw_pages` dedups on body hash alone, so the stored body is the response plus `{"market": id, "start_ts": s}`. Otherwise two empty or identical responses collide and the second is refetched forever.
- Skip when a page with that market and `start_ts` exists. A postponed game has a new `start_ts`, so it's fetched again. An empty response is still stored (and skipped next time).
- Errors: one failing market is reported and skipped; the others continue (same as `sync`).

### Normalize (`normalize.py`)

New table `closing_lines`: `venue, market_id, start_time, quote_ts, yes_bid, yes_ask, yes_mid` (micros), key `(venue, market_id, start_time)`.

- Quote = the latest point with `quote_ts <= start_time` and `quote_ts >= start_time − 3600`, where `0 < yes_bid <= yes_ask < 1`. Points failing that (one-sided, crossed, null) are skipped. No such point → no row.
- If several pages exist for one key, the latest page wins.

New `bets` columns:

- `close_mid`: your side's mid, `yes_mid` for YES, `ONE − yes_mid` for NO.
- `clv` = `close_mid − avg_entry`, micros per contract.
- `clv_usd` = `round(entry_qty × clv)`, micros.
- All NULL unless the bet is eligible (above), not `void`, and has a closing line.

Closed-early and open bets keep their CLV: it's about the entry, not the exit.

### Metrics (`metrics.py`): `clv(conn, venues)`

Returns: `eligible` (eligible, non-void), `with_close`, `clv_usd` (sum), `pnl` (realized P&L of those with a close and not open), `mean_cents` and its 95% CI (`1.96 × sd / √n`), `beat` (share with `clv > 0`), and `missing` (eligible bets with no close: `bet_id, start_time`).

### Dashboard (`app.py`): "CLV" section

- Table rows Kalshi / Polymarket / Both: with close / eligible, CLV $, P&L on same bets, mean CLV ¢ (95% CI), beat close %.
- Per-bet table, newest first: date, market, type, side, entry ¢, close ¢, CLV ¢, CLV $, P&L.
- "No close" list when non-empty.
- Note: "Close = mid at game start on the same venue. Live bets, parlays, futures, other markets and voids have no CLV."

## Docs to update

- `docs/data-model.md`: replace the planned `closing_lines` schema (game/line keyed, for Pinnacle) with the venue-market one above; add the `bets` columns and the new `raw_pages` endpoints. Phase 5 designs its own Pinnacle storage.
- `docs/kalshi.md`: candlestick endpoints, tier fallback, field-name difference.
- `docs/polymarket-us.md`: the `timestamp.`-prefixed params and body shape.
- `docs/pnl-rules.md`: CLV definition (mid, your side, CLV $).
- `docs/roadmap.md`: phase 4 done when exit check passes.

## Testing (offline)

- Fixtures: one scrubbed real response each for Kalshi live candlesticks, Kalshi historical candlesticks, Polymarket price-history.
- Close picking: last valid quote wins; stale (> 60 min before start) → none; one-sided / crossed skipped; both Kalshi field spellings; Polymarket bid from `shortPrice`.
- Bets: NO-side `close_mid` and sign of `clv`; void and ineligible bets get NULL.
- `sync_closes` with fake getters: Kalshi 404 → historical; already archived skipped; changed `start_time` refetched; empty response stored once.
- `metrics.clv` on a small in-memory DB.

## Out of scope

Live CLV, parlay CLV (deferred past v1), Pinnacle/sharp CLV (phase 5), per-segment CLV slices.
