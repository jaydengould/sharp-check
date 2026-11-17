# Phase 9: Novig as a third venue (design, 2026-11-17)

## Goal

Add Novig to the dashboard, refreshed by the same on-demand sync, using only Novig's read-only v3 API. Two levels of detail:

1. **Money (all Novig activity):** bankroll, cumulative P&L, own-money P&L and cash reconcile. These cover every bet, including the trade-credit bets and parlays the API can't itemize.
2. **Cash straights (per bet):** straight bets paid fully from the cash wallet get the same treatment as Kalshi and Polymarket singles: open positions, recent bets, Bets tab, betting P&L, ROI, wins vs expected, segments, Habits stake and timing views, and fees.

Done when:
- the venue switch reads All / Kalshi / Polymarket / Novig;
- the Novig position shows the user's cash straight(s) and an own-money P&L that matches `balance + open cash-straight cost + withdrawals − deposits`;
- the header's reconcile check covers Novig (ledger total = reported balance, and fills-based cash = reported balance);
- every Novig gap is labelled where it shows.

## Why

The user bets mainly on Kalshi and Polymarket and will use Novig when its price is better. Their referral trade credits are almost used up, so future Novig bets will mostly be cash straights, which the API sees in full. They want this automated: Novig's per-period statements (live chat, emailed) are rejected because they'd need a manual request each time.

## Findings (2026-11-17, throwaway probe, read-only)

- Auth: NOVIG-V3 signing (docs.novig.com/api/signing.md). Six lines joined by LF: `NOVIG-V3`, ms timestamp, METHOD, path, canonical query, hex sha256(body). Ed25519 or P-256 (DER). Headers `Novig-Key-Id`, `Novig-Timestamp`, `Novig-Signature` (base64). ±30 s skew. Production host `https://api.novig.com`. The probe's signer reproduces the published test vectors.
- Key: a `management::read` key (created 2026-11-17 by signing `POST /v3/keys` with the management key once). It reads every wallet and cannot place orders or move money. Env: `NOVIG_READ_KEY_ID`, `NOVIG_READ_PRIVATE_KEY` (base64 PKCS#8 DER, or a PEM with `\n`). The management key is not used by sharp-check.
- Responses from a management-family key are keyed by key ID: `{"<keyId>": {"items": [...], "next": ...}}`. The cash wallet is under the signing management key's ID. The user's account has one wallet (no subaccounts). Paging needs `keyId` with `after`. `/v3/account/transactions` and `/v3/account/balance` are bare (primary wallet only).
- Coverage (Novig dev relations, 2026-11-17 email): app bets settle in the cash wallet, **except a bet placed with a trade credit**. Each of those runs in its own trade-credit wallet, which no v3 key reads. Most of the user's bets since 2026-09-16 used credits. No v3 route lists the user's parlays as the bettor. v2 credentials aren't issued for new work. `FORWARDING` moves cash from the cash wallet into a trade-credit wallet, for the part of a bet the credit doesn't cover.
- Since the first deposit (2026-09-15): `/v3/portfolio/fills` 1 fill, `/v3/history/positions` 1 settled position, `/v3/account/transactions` 30 rows (DEPOSIT 5, WITHDRAWAL 6, FILL 3, FEE 2, SETTLEMENT 2, FORWARDING 4, CREDIT 8). **The ledger sums to the balance exactly**, and the running total never goes negative.
- Ledger `ref`: a visible FILL's `ref` = its `fillId`; a visible SETTLEMENT's `ref` = its `positionId`. Two FILL rows, two FEE rows and one SETTLEMENT have refs that match no visible fill or position (credit-funded bets). Every CREDIT row is an inflow. Credits are undocumented; most likely credit-wallet payouts returning to cash.
- Market lookup: `GET /v3/history/markets/{id}` works for any market (fields `marketId, eventId, description, marketType, status, strike, voids, fee, outcomes[], settledTs`). Each outcome has `outcomeId, name, status, subjectId`, where `status` is `TBD | WIN | LOSS | PUSH | <decimal price>`. Example: SPREAD, outcomes `CHI -3.5` (WIN), `NYJ +3.5` (LOSS). `GET /v3/history/events/{id}` gives `description` ("New York Jets @ Chicago Bears"), `sport`, `league`, `status`, `startsTs` (ms).
- Market types (`/v3/types/markets`): `MONEY, SPREAD, TOTAL, 1X2, MONEYLINE_3_WAY_*, TEAM_TOTAL, *_1H`, many player-stat props (`PASSING_YARDS`, `ANYTIME_TOUCHDOWN_SCORER`, ...), futures-like awards (`OPOTY`, ...), `CUSTOM`, `TO_ADVANCE`.
- Every open market is two-outcome: 8,000+ catalog markets checked on 2026-11-17, none with more or fewer than two outcomes. 3-way soccer lines are split into separate two-outcome markets (`MONEYLINE_3_WAY_WIN` / `_DRAW`). Many prop types also come in a `*_PROTECTED` variant.
- Response shapes seen by the probe (no amounts recorded):
  - fill: `fillId, orderId, marketId, outcomeId, qty` (int), `cost` (dollar string, whole fill), `taker` (bool), `ts` (int ms), `fee` (string, absent when uncharged);
  - history position: `positionId, marketId, outcomeId, qty, cost, status, result, payout, pnl` (dollar strings), `settledTs` (ms), `resettled`;
  - ledger row: `transactionId, kind, amount` (signed dollar string), `ref` (uuid), `ts` (ms);
  - balance: `{"balance": "<dollars>"}`;
  - paged bodies: `items` plus `next` (absent on the last page).
- Fees: taker only, `c · P(1−P) · N · 1¢`. Game markets charge only while the event is in play (`charged: WHEN_LIVE`). The actual fee is on the fill (`fee`, absent when uncharged).
- No historical bid/ask: only trades and trade-based candles. So there is no mid at close (CLV) and no mid before a fill (spread).
- Every signed call is location-checked: 451 for a VPN, a restricted state (AZ, MI, NV), or a missing or flagged phone geolocation. `GEOLOCATION_BLOCKED_SOFTWARE` was hit once and cleared after the user fixed their phone.

## Decisions (2026-11-17)

- Scope is option 1 of three: money plus cash straights. Rejected: money only (future cash straights would never show as bets); also doing CLV and the 3-way line shop (no mid history makes Novig CLV not comparable; the line shop needs a new game matcher since Novig has no Sportradar IDs, so it gets its own later phase if Novig is often cheaper).
- Novig gets its own position in the venue switch, and "Both" is renamed "All".
- The Overview keeps **Betting P&L** and adds an **Own-money P&L** tile next to it, for every switch position (the user wants both, not one replacing the other).
- Cumulative P&L for Novig is cash basis: balance + withdrawals − deposits, plus open cash-straight cost (as Kalshi's own-money adds open cost).
- Read-only key only. Never call order, transfer or key endpoints, and never load the management key.
- Novig work lives on the local `novig` branch and is published only when the phase is complete.

## Design

### Client (`clients.py`)
- `novig_get(path, params=None)`: NOVIG-V3 signing with the read key from env. Detect Ed25519 vs P-256 from the loaded key. Build the canonical query from the same params dict that is sent, sorted, `quote(safe="-._~")`. The private key never appears in errors or logs (as with the other venues: never print response headers).
- `novig_pages(path, params=None, limit=500)`: unwraps the key-ID envelope. First call without `keyId`; for each wallet, follow `next` with `keyId` + `after`. Yields `(params, body)` like the other pagers. Bare-bodied routes (transactions, balance) page on `next` directly.
- A 451 is reported as "Novig location check failed (open the Novig app; no VPN)". It fails only Novig's endpoints; Kalshi and Polymarket sync carry on (the existing per-endpoint try/except does this).
- Test: signer against Novig's published Ed25519 vectors (strings in the test, no network).

### Archive (`archive.py`)
- `ENDPOINTS` add (venue `novig`): `/v3/portfolio/fills`, `/v3/history/positions` (status `OPEN,CLOSED,SETTLED`), `/v3/portfolio/positions` (open positions; cost is what bankroll and own-money need), `/v3/account/transactions`, `/v3/account/balance`.
- `sync_novig_markets`: for every `marketId` in fills, store `/v3/history/markets/{id}` and its `/v3/history/events/{eventId}`, re-fetched each run until the market is `SETTLED` (or `GIVE_UP_S` past the event start, like Polymarket).
- `raw_pages` stores bodies as returned (key-ID envelopes included; the DB is local and gitignored).
- First build step: extend `scripts/smoke.py` to Novig (read-only) and capture fixtures. The user must be present, because every call passes the location check. Shapes are in Findings, so parsers can be written test-first against them.
- Fixtures (`tests/fixtures/novig_*.json`): scrubbed. Replace key IDs, transaction IDs and every amount with fakes (public repo rule).

### Normalize (`normalize.py`)
- Markets must have exactly two outcomes. A Novig market with any other count fails the rebuild loudly (findings: none exist today), since the yes/no model would misbook it.
- `markets`: `market_id` = marketId, `event_id`, `title` = event description, `sport`/`league` from the event, `line` = `strike`, `label_yes` = `outcomes[0].name`, `label_no` = `outcomes[1].name`, `yes_value` from `outcomes[0].status` (WIN → ONE, LOSS → 0, decimal → that price, PUSH → per the `voids` rule; builder to confirm with the docs). `market_type`: MONEY / 1X2 / MONEYLINE_3_WAY_* → moneyline; SPREAD → spread; TOTAL → total; player-stat and scorer types → prop, with `*_PROTECTED` mapped like its base type; award types and tournament-finish types (`WINNER`, `TOP_*_FINISH`, `TO_MAKE_THE_CUT`) → future; TEAM_TOTAL, *_1H → other (matching `classify.PM_OTHER`); CUSTOM / TO_ADVANCE → other. The table lives in `classify.py`, and unknown types are printed like other unclassified codes.
- `games`: `novig:<eventId>`, league, title, start from event `startsTs`.
- `fills`: outcome = `yes` if `outcomeId == outcomes[0].outcomeId` else `no`. `price` = `cost / qty` in micros (cost is dollars for the whole fill). `fee` from the fill (0 if absent). `is_taker` = `taker`. An exit on Novig is buying the other outcome, which the existing "opposite-side fill is an exit at 1 − price" rule already handles.
- `settlements`: from history markets with status SETTLED: `yes_value` as above, `settled_ts` = `settledTs`. Cross-check: each derived settled bet's `realized_pnl` must equal `/v3/history/positions` `pnl` within a cent, else the rebuild fails loudly. `resettled: true` takes the latest page (latest-page-wins rule).
- `cash_ledger` from `/v3/account/transactions`:
  - DEPOSIT → `deposit`; WITHDRAWAL → `withdrawal`.
  - FILL / FEE whose `ref` is a visible fillId, and SETTLEMENT whose `ref` is a visible positionId: skipped (fills and settlements already carry that cash).
  - Every other FILL / FEE / SETTLEMENT, plus FORWARDING and CREDIT → new kind **`untracked_bet`**: cash moving in and out of bets the API can't itemize.
  - MAKER_CREDIT on a visible fill is skipped like FEE (a negative fee). ADJUSTMENT, TRANSFER_IN/OUT, CREDIT_RECLAIM and any unknown kind fail the rebuild, so a new kind gets a decision instead of a silent guess.
- `bets`: unchanged `derive_bets`. Novig bets get no CLV (not `CLV_ELIGIBLE`: the eligibility check adds `venue != 'novig'`), no spread and no line shop.

### Metrics, habits, reconcile
- `metrics.VENUES` becomes `("kalshi", "polymarket", "novig")`. Everything that loops venues picks Novig up.
- `reconcile.reported_cash('novig')` = latest `/v3/account/balance` `balance`. Both reconcile checks (fills-based and bets-based) then include `untracked_bet` rows via `cash_ledger`, so they must match.
- `metrics.locked_bonus('novig')` = 0. The leftover trade credit lives in its own wallet, isn't in the balance and isn't the user's cash.
- `own_money('novig')` uses the existing formula (reported cash + open bet cost − deposits − withdrawals). This equals the agreed `balance + open cash-straight cost + withdrawals − deposits`.
- `habits.bankroll_series`: `untracked_bet` rows move **both** bankroll and the cumulative P&L line (they are bet cash, not money put in). The chart line is renamed "Cumulative P&L". Known effect: while a credit bet is open its cash shows as lost, and it comes back at payout; exact whenever none is open. With no credits left this fades.
- Execution cost (Habits): Novig fees per venue × type come from fills. Total fees paid on untracked bets (ledger FEE rows) are shown as one extra line, "Novig fees on bets the API can't itemize". The maker-saving estimate for Novig = the taker fee paid (makers pay 0).
- Spread (8a) and line shop (8b) exclude Novig. Data health lists them once with "Novig has no quote history" / "Novig not in line shop", not per fill.

### Dashboard (`app.py`)
- `SCOPES`: `All` (all three), `Kalshi`, `Polymarket`, `Novig`. `VENUE` label `"novig": "Novig"`.
- Overview: add tile **Own-money P&L** (sum of `own_money` over the scope's venues; sub "account value − net deposits"). Betting P&L stays. On the Novig scope, the CLV tiles read "– · not available on Novig", and Betting P&L's sub says "cash bets only".
- Bankroll chart: line renamed "Cumulative P&L". Note under it: "Novig: includes bets the API can't itemize, at their cash."
- CLV tab switch: All / Kalshi / Polymarket only (Novig has no CLV). Novig bets aren't CLV-eligible, so they never appear in its lists.
- Performance: the per-venue loops gain a Novig row automatically. The own-money table's "Bonus contribution" column is renamed "Bonus / untracked" for every venue (Kalshi and Polymarket untracked = 0). For Novig, own money − betting P&L is the cash result of credit-funded bets and parlays the API can't itemize. That is mostly referral-credit value, but cash-funded parlays are in it too, so plain "bonus" would mislead. Note: "Novig: credit-funded bets and parlays count only at their cash."
- Bets tab: Novig in the switch. A note on the Novig scope: "Cash-funded straights only; credit-funded bets and parlays aren't available from Novig's API."
- Data health: Novig in the cash reconcile table, plus a "ledger total vs balance" row (a mismatch means a ledger page is missing).
- Habits: stays switchless (decided 2026-10-05). Its venue rows pick Novig up.

### Docs (same commit as the code)
- New `docs/novig.md`: auth, envelope, endpoints, coverage limits (the dev-relations answers), location check, ledger kinds and the `ref` matching, market-type map.
- CLAUDE.md: hard rule "Novig: read-only `management::read` key only; never call order, transfer or key endpoints", plus docs index line.
- `data-model.md`: Novig sources per table, `untracked_bet`. `pnl-rules.md`: Novig own-money and cumulative-P&L rules, no CLV or spread. `roadmap.md`: phase 9 row and exit check.

## Non-goals
- Novig CLV, spread at entry and exit, and the Novig line shop (possible later phase 10: a 3-way line shop with a name + start-time matcher; the order book is readable without signing).
- Parlays and credit-funded bets as itemized bets (the API can't see them).
- Novig statements or any manual import.
- The websocket (management keys can't use it; polling is enough).

## Anti-goals
- A Novig view that looks like a full record when it isn't. Every per-bet Novig number says "cash bets only".
- Sync failures caused by Novig's location check taking Kalshi or Polymarket down with it.
- The management key anywhere in sharp-check.

## Left to the builder
- PUSH and decimal-price outcome statuses → `yes_value` (check docs `voids` semantics: `PUSH` vs `FMV`).
- Whether `/v3/portfolio/positions` or `/v3/history/positions?status=OPEN` is the better open-position source (they should agree; pick one and cross-check the other in a test).
- Exact Novig signer test vectors to embed (two Ed25519 ones are in the probe; see `docs.novig.com/api-reference/spec-files/signing-vectors.json`).
- Ordering of `untracked_bet` and visible-fill rows at equal timestamps in the bankroll series (net them, as deposits and fees are today).
