# Polymarket US API

Docs: https://docs.polymarket.us (index at `/llms.txt`). Verified 2026-09-30. This is the CFTC-regulated US exchange, not global Polymarket. Ignore Gamma, CLOB and Data API material.

## Hosts and auth
- Authenticated: `https://api.polymarket.us`. Public market data: `https://gateway.polymarket.us`.
- Headers: `X-PM-Access-Key` (key ID), `X-PM-Timestamp` (ms, within 30 seconds of server time), `X-PM-Signature`.
- Signed string: `timestamp + METHOD + path`, e.g. `/v1/portfolio/activities`. Key: `Ed25519PrivateKey.from_private_bytes(b64decode(secret)[:32])`. The signature is base64-encoded.
- **Unverified:** whether the query string is part of the signed path.

## Endpoints used
- `GET /v1/portfolio/activities`
  - Filters: `types` (`ACTIVITY_TYPE_TRADE`, `_POSITION_RESOLUTION`, `_ACCOUNT_DEPOSIT`, `_ACCOUNT_WITHDRAWAL`, `_TAKER_FEE_REBATE`, ...).
  - Pagination: `cursor`/`nextCursor`/`eof` and `sortOrder`. There are no time filters.
- `GET /v1/portfolio/positions` and `/v1/account/balances`.
- Events: `startTime`, `gameId`, `sportradarGameId`, `teams[].providerIds`. Market fields: `slug`, `sportsMarketType`, `line`, `marketSides`, and `status` (`RESOLVING`/`RESOLVED`).
- `GET gateway…/v1/price-history?symbol=<slug>&fidelity=1&timestamp.startTimestamp=<s>&timestamp.endTimestamp=<s>`. **The range params need the `timestamp.` prefix**; the bare names in the docs return 400 `code 3`. Body `{"history": [{timestamp, longPrice, shortPrice}]}` with no symbol. Both prices are asks: YES bid = 1 − `shortPrice`. Coverage verified back to 2026-06-22. **Unverified:** whether a point's `timestamp` is the start or end of its minute; if the start, a quote at T can include up to 59 s after T (matters for parlay placement quotes, which land on whole minutes).
  - Public, limited to 20 requests per second, cached for 30 seconds.
  - A custom range is `startTimestamp`/`endTimestamp` in Unix seconds, up to 24 hours, and returns raw `longPrice`/`shortPrice` points.
  - Prices are book-derived (ask-based), not traded prices.
- **Price-history outage, 2026-10-05 01:07 to ~22:10 UTC (resolved).** Until 2026-10-05 01:07 UTC, price-history served resolved markets back to 2026-06-22 (the changelog's 45-day market-data retention evidently doesn't apply to it). After that, every resolved market returned `200 {"history":[]}` while open markets were fine; no changelog entry. Probe kept for reference: `python scripts/pm_history_check.py`. If it recurs and stays empty ~3 days with no changelog entry, see spec 2026-10-05-phase8a, "If Polymarket history stays empty".
  - 2026-10-05: still empty for resolved markets (0 of 10); open markets 61 points/hour.
  - 2026-10-05 17:30 UTC: still empty (0 of 10); open markets 61 points/hour.
  - 2026-10-05 ~22:10 UTC: back (13 of 13); refetched values identical to pre-outage pages (12 markets, 0 diffs). In-game points exist (1/minute). Decided legs pin within 1¢ of 0/1 rather than going empty, so the pinned-leg rule covers Polymarket and it needs no kept empty pages. Probe dropped from the session start.
- `fixedInterval` (`INTERVAL_1H|6H|1D|1W|1M|ALL|LIVE`) is an alternative to a timestamp range; don't combine them. `INTERVAL_LIVE` starts 15 minutes before the event.

## Verified 2026-09-30 by smoke test (trust `tests/fixtures/` over the docs)
- Query string is **not** signed. Sign the bare path; signing path+query returns 401.
- **The docs understate the trade payload.** Each trade includes `aggressorExecution.order` / `passiveExecution.order`. Order fields:
  - `side`: `ORDER_SIDE_BUY|SELL`.
  - `intent`: `ORDER_INTENT_BUY_LONG|SELL_LONG|BUY_SHORT|...`. Use this for direction.
  - `price`, `avgPx`.
  - `commissionNotionalTotalCollected`: the fee.
  - `marketMetadata.outcome` / `eventSlug`.
- The top-level trade has `price`, `qtyDecimal`, `cost`, `state`, `comboLegDetails` and `isAggressor`.
- **Prices are always the long (YES) price**, even on `BUY_SHORT`/`SELL_LONG`; the short price is `1 - price`. `cost` includes the fee: buys = qty × side price + fee, sells = proceeds − fee. Verified on all 73 trades 2026-10-02.
- Use the execution-level fee `commissionNotionalCollected`. The order-level `commissionNotionalTotalCollected` is cumulative across that order's fills. The other side's execution is the counterparty's order; pick yours by `isAggressor`.
- Resolution `beforePosition` carries `cost`, `avgPx`, `fees` and `realized`.
- **Combos (parlays):** slug prefix `caoc-`. `comboLegDetails[]` lists each leg's slug, eventSlug and team.
  - Leg `settlement.settlementPrice` is the leg market's YES price. `state` (`COMBO_LEG_STATE_WON|LOST|INDETERMINATE`) is from the user's side, so a NO leg is WON at 0.
  - `INDETERMINATE` = settled at a non-0/1 price (one leg at 0.40 as of 2026-10-02, probably last-traded-price settlement after a cancellation). Checked 2026-10-03: that parlay lost on another leg first and settled at 0. A combo that settles at a non-0/1 price becomes a `void` bet. Combo slugs 404 on `/v1/market/slug`.
- Single-market lookup: `gateway/v1/market/slug/{slug}`, singular `market`. It has `gameStartTime` (deprecated) and `sportsMarketType`, e.g. `drawable_outcome` (the Draw side of a 3-way moneyline).
- Lookup body also has `status` (`MARKET_STATUS_RESOLVED` once final), `outcomePrices` (a JSON **string**, YES/long first, `["1","0"]` when resolved) and `marketSides[]` (`description`: team nickname, `Yes`/`No`, `Over`/`Under` or a spread like `-4.50`; `team.name`). The YES side is `marketSides[0]`. `title` is a short label for props (sometimes just the player name) and None for game lines. Parlay legs are looked up one by one (combo slugs 404); verified 2026-10-04 that every leg resolves.
- Phase 3 takes start time and type from this market lookup, not the event lookup: an event page lists every market (~1,100 for an NFL game, ~5 MB). `gameStartTime` matched kickoff on PHI @ CHI. Futures slugs have segment `f` (`tec-f-wc-…`).
- Event lookup: `gateway/v1/events/slug/{eventSlug}`. It has `startTime`, `gameId`, `sportradarGameId` and `seriesSlug`.
- The institutional `report/executions/search` endpoint uses a different host and JWT auth. It's not usable with a retail key and isn't needed.
- History: trades from 2026-06-22 onward. 163 activities total.

## Line shop (phase 8b, verified 2026-10-05)
- `GET gateway/v1/events` filters: `tagSlug` (`nfl`, `cfb`, `mlb`), `startTimeMin/Max`, `marketTypes` (`moneyline|spreads|totals|props`; trims the embedded markets), `sportradarGameId`, `slug`, `limit`, `offset`. `sportsMarketTypes=moneyline` returns nothing. Without `marketTypes` every event embeds all its markets (28 MB per 100 events; ~3 MB per NFL game). Group-stage soccer events have no `moneyline`-type market.
- Event market objects carry the live top of book: `bestBidQuote` / `bestAskQuote` (`{value}`, YES prices; `outcomePrices` = [bid, ask]) and `feeCoefficient` (0.0695 on every NFL market, 2026-10-05). No sizes.
- Sizes: `GET gateway/v1/markets/{slug}/book` → `marketData.bids[]` / `offers[]` of `{px: {value}, qty}`. **The gateway throttles after ~5 quick calls** (429, `Retry-After` 1–4 s) despite the documented 25 req/s, so the checker fetches books only where Polymarket is cheaper (max 10 per game), and Refresh doesn't reload the game list while a game is shown.
- Event slugs list away team first (`nfl-nyj-chi-2026-10-04`); team abbreviations are on `teams[]` and `marketSides[].team`. Prop titles: live `Braelon Allen 1+ touchdowns` / `… scores the first touchdown`; archived market lookups have the bare name. Per-team soccer winner markets are `soccer_team_full_time_winner` (Yes/No, team in the slug suffix).

## Bonuses (checked 2026-10-02)
- Bonus cash can't be withdrawn, and winnings from bonus bets may stay locked. The release rules are unknown and we don't model them.
- `REFERRAL_BONUS` activities are releases ("Releasing N in incentives due to ..."). `metadata.incentive_type` is `REFEREE | REFERRAL | PROMOTION_DEPOSIT_BONUS`. Descriptions can contain an account ID, so scrub them.
- Trades carry **no** funding-source field, so bonus-funded bets can't be identified.
- `/v1/account/balances` has `displayedBonus`, `bonusHold`, `bonusReservation`, `availableToWithdraw` and `displayedCash`. Provisionally using `displayedBonus` as the locked bonus. On 2026-10-02 the app showed $0 bonus and all three bonus fields were 0, so this isn't confirmed yet. Re-check the next time the app shows a non-zero bonus.
- Balances are snapshots only. The archive saves one per run (since 2026-10-02); nothing before that can be recovered.
