# Novig

Read-only. Host `https://api.novig.com` (v3). Client: `clients.novig_get` / `novig_pages`. Design: `docs/superpowers/specs/2026-11-17-novig-design.md`.

## Key and auth
- Key: a `management::read` key, minted once by signing `POST /v3/keys` with the management key (made on novig.com → Profile → Settings → Novig API). Env `NOVIG_READ_KEY_ID`, `NOVIG_READ_PRIVATE_KEY`: PEM (literal `\n` ok), base64 PKCS#8 DER, or a base64 raw Ed25519 seed. The management key isn't used by sharp-check and shouldn't be in `.env`.
- Signing NOVIG-V3: six lines joined by `\n`, no trailing newline: `NOVIG-V3`, ms timestamp, METHOD, path (with `/v3`), canonical query (sorted, `%XX` uppercase, keep `A-Za-z0-9-._~`), hex sha256 of the body. Headers `Novig-Key-Id`, `Novig-Timestamp`, `Novig-Signature` (base64). Ed25519 or P-256 (DER). Skew ±30 s. Test vectors: `docs.novig.com/api-reference/spec-files/signing-vectors.json`.
- **Every signed call is location-checked.** 451 = VPN, restricted state (AZ, MI, NV), or a missing/flagged phone geolocation (`GEOLOCATION_BLOCKED_SOFTWARE`: remote desktop or spoofing on the phone). Fix: open the Novig app. A Novig 451 fails only Novig's endpoints.

## What the API can see (dev relations, 2026-11-17)
- App bets settle in the **cash wallet**, which the key reads. **Exception:** a bet placed with a trade credit runs in its own trade-credit wallet; no v3 key reads those. No v3 route lists your parlays as the bettor (RFQ routes are maker-only). v2 credentials aren't issued for new work.
- So per-bet data = straights paid fully from cash. Everything else shows only as cash in the ledger (`untracked_bet`).
- **Contracts pay 1¢**, not $1. `cost` is dollars for the whole fill; price per contract = `cost / qty`. Normalize stores `qty / 100` as $1-contract equivalents so prices and payouts use the shared model.

## Endpoints archived
- `/v3/portfolio/fills`, `/v3/history/positions` (`status=OPEN,CLOSED,SETTLED`), `/v3/account/transactions`: paged by `next` → `after`. Personal reads come keyed by wallet key ID (`{"<keyId>": {"items", "next"}}`); `novig_pages` unwraps and pages with `keyId` + `after`. More than one wallet (a subaccount) raises. Transactions and balance are bare.
- `/v3/portfolio/positions` (open, `{keyId: [..]}`, stored as `{"positions": [...]}`), `/v3/account/balance` (`{"balance": "<dollars>"}`).
- `/v3/history/markets/{id}` (`{"market": ...}`) and `/v3/history/events/{id}` (`{"event": ...}`) per traded market, re-fetched until `SETTLED` or 14 days past the event start. Markets are two-outcome (8,000+ checked 2026-11-17); `outcomes[0]` = YES. Outcome `status`: `TBD | WIN | LOSS | PUSH | <decimal>`. PUSH takes the venue's payout per contract from the user's settled position (0.5 if none, e.g. exited first). A market is refetched while any of its positions is `resettled`, or settled per positions but not per our page. An event with no `startsTs` gets no game row.
- No historical bid/ask (trades and trade candles only): no CLV, no spread.

## Ledger (`/v3/account/transactions`)
- Fields `transactionId, kind, amount` (signed dollars), `ref`, `ts` (ms). The ledger sums to the balance exactly (`reconcile.novig_ledger_gap`).
- `ref`: FILL / FEE → `fillId`; SETTLEMENT → **`marketId`** (not positionId).
- `FORWARDING` = cash into a trade-credit wallet, for the part of a credit bet the credit doesn't cover. `CREDIT` = inflows (most likely credit-wallet payouts; undocumented).
- `MAKER_CREDIT` (ref = fillId) is a rebate: subtracted from that fill's fee.
- Normalize: DEPOSIT/WITHDRAWAL → deposit/withdrawal; FILL/FEE/MAKER_CREDIT of a visible fill and SETTLEMENT of a visible market are skipped (fills and settlements carry them); other FILL/FEE/SETTLEMENT and FORWARDING/CREDIT → `untracked_bet`; any other kind fails the rebuild.

## Fees
- Taker only: `c · P(1−P) · N · 1¢`, `c` from the market. Game markets charge only while in play (`charged: WHEN_LIVE`). The fill's `fee` is absent when uncharged.

## Market types
- `classify.novig_type`: MONEY/1X2/3-way/TO_ADVANCE → moneyline, SPREAD, TOTAL, player stats and scorers → prop (`*_PROTECTED` like its base), golf finishes → future, TEAM_TOTAL/`*_1H`/CUSTOM → other. Unknown types are `other` and printed by `normalize`. A market whose event has no start is a future.
