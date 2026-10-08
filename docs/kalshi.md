# Kalshi API

Docs: https://docs.kalshi.com (index at `/llms.txt`). Verified 2026-09-30.

## Auth
- Base URL: `https://external-api.kalshi.com/trade-api/v2`. The legacy `api.elections.kalshi.com` still works.
- Headers: `KALSHI-ACCESS-KEY` (key ID), `KALSHI-ACCESS-TIMESTAMP` (ms), `KALSHI-ACCESS-SIGNATURE`.
- Signed string: `timestamp + METHOD + path`, with no separators. The path includes `/trade-api/v2` and excludes the query string.
- Our key is **Ed25519**. It's stored in `.env` as `KALSHI_PRIVATE_KEY`: the base64 DER body with the PEM header and line breaks removed, loaded with `load_der_private_key`. Ed25519 signs the message directly. Confirmed working 2026-09-30.
- If the key is ever replaced with an RSA key, sign with RSA-PSS SHA-256, MGF1 SHA-256 and `salt_length=PSS.DIGEST_LENGTH`. Branch on the parsed key type.

## Endpoints used
- `GET /portfolio/fills`: cursor pagination, `min_ts`/`max_ts`, `limit` up to 1000.
- `GET /portfolio/settlements` and `GET /portfolio/positions` (`settlement_status=settled`).
- `GET /markets/{ticker}`, `/events/{event_ticker}`, `/series/{series_ticker}`.
- `GET /series/{series}/markets/{ticker}/candlesticks` (`start_ts`, `end_ts`, `period_interval=1`, `include_latest_before_start`): 1-minute `yes_bid`/`yes_ask` OHLC, for the close at game start. Series = ticker prefix before the first `-`. A 404 means the market is past the cutoff: use `/historical/markets/{ticker}/candlesticks`. The historical tier spells closes `close`, the live tier `close_dollars`. The synthetic before-start candle has null prices. `end_period_ts` is the inclusive end of the minute. Coverage verified back to 2025-11-01.
- `GET /historical/cutoff`, then `/historical/fills|orders|positions|markets|...` for records older than the cutoff.

## Historical tier
- Data older than a per-type cutoff timestamp is only served by `/historical/*`. The cutoff moves forward over time, so always fetch it and merge both tiers.
- `/portfolio/settlements` has **no historical equivalent**. Archive raw settlement pages as soon as possible. The fallback is market `result` from `/historical/markets`.

## Fill normalization
- Prices and fees are fixed-point dollar strings (`yes_price_dollars`, `no_price_dollars`, `fee_cost`). Counts are `count_fp` strings with 2 decimals, and fractional contracts are allowed.
- `side`/`action` are deprecated. Use `outcome_side`: buy-YES and sell-NO both give `yes`. Normalize every fill to "acquire `outcome_side` at that side's price". Exits then net against the position.

## Verified 2026-09-30 by smoke test
- History goes back to 2025-10-31 via `/historical/fills`. Cutoffs were at 2026-08-01.
- `/portfolio/settlements` returned only 5 recent records. **Older settlements are already gone**, so use market `result`/`settlement_value_dollars` from `/historical/markets`.
- Deposits and withdrawals: `/portfolio/deposits` and `/portfolio/withdrawals`, fields `amount_cents`, `fee_cents`, `created_ts` (Unix seconds), `status` (`applied|failed`). `finalized_ts` is missing on failed ones.
- Debit deposits carry `fee_cents`. `amount_cents` is gross, so the fee is booked as a separate debit (confirmed by reconciliation 2026-10-02).
- **Rewards have no API endpoint.** They're hand-entered in `data/manual_cash.csv` from the app's rewards page (part of them can go to the perps account).
- **Perps** (`margined` exchange instance) have their own cash pool (`GET /margin/balance`, `settled_funds`). They're out of scope. `GET /portfolio/intra_exchange_instance_transfers` lists moves: `event_contract`↔`margined` are cash in or out; `event_contract`→`event_contract` is shard rebalancing (ignore it). Two `margined→event_contract` transfers have been `pending` for weeks, but the balance already includes them.
- `/portfolio/balance`: `balance_dollars` = sum of `balance_breakdown` across exchange indexes. All fills are on subaccount 0.
- `/portfolio/positions` (needs `settlement_status=all`, otherwise settled positions are hidden) and `/historical/positions`: per-market `realized_pnl_dollars` **excludes** fees (`fees_paid_dollars`). Both are archived for reconciliation.
- **Game start time: milestones**, not market `occurrence_datetime` (that is ~3h after kickoff, about the expected end, and null on some games). `GET /milestones?related_event_ticker=<event>` (public) returns the game with `start_date` (kickoff; verified on ATL @ GB, LAR @ CHI, Super Bowl LX), `related_event_tickers` and Sportradar `source_ids`. The `event_ticker` filter is silently ignored and returns unrelated milestones. Player-prop events often aren't linked; look up their game event instead (`KXNFLTD-26SEP24ATLGB` → `KXNFLGAME-26SEP24ATLGB`). Milestone `type` is unreliable (`soccer_tournament_multi_leg` for single World Cup matches, `hockey_tournament` for an NHL game), so game vs future comes from the ticker: a game event's second segment is a date+teams code like `26SEP24ATLGB`.
- Market records for all traded tickers are archived (83 on 2026-10-02: 82 `finalized` with `result` `yes|no`, no voids). A 404 on `/markets/{ticker}` means it's past the cutoff, so use `/historical/markets/{ticker}`.
- **Combos (parlays):** tickers start `KXMVE`. Market `mve_selected_legs[]` gives each leg's `market_ticker` and `side`. About half of all fills are combos.

## Line shop (phase 8b, verified 2026-10-05)
- `/milestones?competition=NFL|NCAAFB|MLB&minimum_start_date=&maximum_start_date=` filters by league (without it a date window pages through every category). `source_ids` holds the Sportradar game ID that Polymarket events carry as `sportradarGameId` (`source_3_id` UUID on US sports, `sr:sport_event:N` on soccer).
- `/markets?event_ticker=` rows carry `yes_bid_size_fp` / `yes_ask_size_fp`, so depth needs no order-book call. `/series/{series}` has `fee_type` and `fee_multiplier` (current only). `/series/fee_changes?show_historical=true` (one unpaged list, archived each run) has every change with `scheduled_ts`: all MLB series went 1 → 0.5 on 2026-08-07; no other line-shop series has differed from 1. A 2026 guide says MLB is 0.5 before the game and 1 live; the API shows one value, unverified.
- Game events list away team first (`KXNFLGAME-26OCT04NYJCHI` = NYJ at CHI), like Polymarket slugs, except some neutral-site games (IND–OSU 2025-12-06 is `INDOSU` on Kalshi, `ohiost-ind` on Polymarket). World Cup knockout games' main event is `KXWCADVANCE-…`.
- NFL props: anytime TD = `KXNFLTD` ladder (`N+`, `floor_strike` N − 0.5) from 2026; `KXNFLANYTD` (no threshold) in 2025 and empty now. `KXNFLFIRSTTD`, `KXNFLRSHYDS`, `KXNFLRECYDS`, `KXNFLRRYDS` (rush + receiving = Polymarket scrimmage yards). Player name = `yes_sub_title` before `:`.
- MLB per-game player props (classified as `prop`): `KXMLBHR`, `HIT`, `TB`, `HRR`, `RBI`, `SB`, `KS`, `WALK`/`WA`, `HA`, `ERA`, `OUTS`. `KXMLBTEAMTOTAL` stays `other` (team total). Not in `match.PROPS`, so no line shopping yet.
