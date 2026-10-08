# Phase 8a: Spread at Entry and Exit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Price every fill against the mid in the minute before it, sum the spread paid per bet and per venue (entry and exit, parlay cash-outs included), show it on Habits and Data health, and track the 2026-10-05 Polymarket price-history outage until it's explained.

**Architecture:** `archive.sync_closes` gains fill-time targets (reusing `fetch_close` and the `raw_pages` dedup). `normalize` merges every archived quote page per market into one point index, prices each fill (singles: two-sided quote ≤ 5 min old; parlay exits: product of leg values with pinned/closed-leg rules; parlay entries: `fair_entry`) into a new `fill_costs` table, and sums per bet into two new `bets` columns. `habits` aggregates, `app` renders.

**Tech Stack:** Python 3.12, SQLite, Plotly Dash 4, pytest (no network).

**Spec:** `docs/superpowers/specs/2026-10-05-phase8a-spread-design.md`

## Global Constraints

- Read-only: never call order create/cancel/modify endpoints. Polymarket US hosts only (`api.polymarket.us`, `gateway.polymarket.us`).
- Never print, log or commit secrets. Scripts print counts and public prices only, never balances.
- `raw_pages` is the source of truth; `fill_costs` and the new `bets` columns are rebuilt by `normalize` from it.
- Timestamps UTC. Money as integer micro-dollars (`ONE = 1_000_000`).
- Cost rule: `cost = price − mid(outcome)`, per contract, fees excluded, signed (never clipped).
- Quote key: fill timestamp floored to the minute (`m`); Kalshi key `m`, Polymarket key `m − 60`.
- Singles: latest two-sided quote with `key − 300 ≤ t ≤ key` (5 minutes); width unlimited.
- Parlay exit legs: latest point in `[key − 3600, key]`; pinned = YES bid ≥ 0.99 or YES ask ≤ 0.01; quoted legs ≤ 5¢ wide (`LEG_MAX_SPREAD`) and ≤ 300 s old.
- An uncomputable cost is NULL, never 0. A bet's spread is NULL if any of its fills in that role is NULL.
- Tests: `python -m pytest tests/` must pass with no network.
- Recurring mistake (CLAUDE.md): Kalshi fills use `outcome_side`; a sell-YES is stored as acquiring `no`.

## Review Focus

- Kalshi `created_time` fractions vary in length (`…19.123Z` vs `…19.123456Z`): fill timestamps must be compared as epochs, never as strings → Task 2 test uses a 3-digit fraction against a 6-digit `closed_ts`.
- A fill that overshoots flat flips the position: one fill, two bets → `fill_costs` gets two rows and "priced fills" counts the fill once → Task 4 flip test, Task 5 distinct-count test.
- A Polymarket point stamped exactly at the fill's minute `m` may include the fill itself → excluded (key `m − 60`) → Task 3 test.
- A partial cash-out on a still-open parlay (`closed_ts` NULL) is still an exit to price → Task 2 test.
- A decided Kalshi leg whose latest candle is pinned but has an older in-play candle before it must count as decided, not use the older price (the 0.86 bug from the probe) → Task 3 test.

## File Structure

- `scripts/pm_history_check.py` (create): read-only Polymarket outage probe, kept and re-run each session until the outage is explained.
- `sharp_check/archive.py` (modify): `fill_key`, `spread_targets`, `sync_closes` targets and kept-empty pages.
- `sharp_check/normalize.py` (modify): schema (`fill_costs`, two `bets` columns), `pick_close` window, `price_index`, `fill_quote`, `leg_value`, `parlay_exit_mid`, `derive_bets` allocations, `spread_costs`, `rebuild` wiring.
- `sharp_check/habits.py` (modify): `spreads`, `unpriced_fills`, exit spread in `cash_outs`.
- `sharp_check/app.py` (modify): Habits spread table, per-bet fold, cash-out columns and note; Data health list.
- Tests: `tests/test_archive.py`, `tests/test_normalize.py`, `tests/test_habits.py`, `tests/test_app.py`.
- Docs: `docs/pnl-rules.md`, `docs/data-model.md`, `docs/polymarket-us.md`, `docs/roadmap.md`, `CLAUDE.md`.

---

### Task 1: Polymarket outage probe

Kept script (not throwaway): the user asked that the outage be tested and researched across the next few sessions until there's an answer.

**Files:**
- Create: `scripts/pm_history_check.py`
- Modify: `docs/polymarket-us.md` (outage section with a dated log), `CLAUDE.md` (Commands line)

**Interfaces:**
- Consumes: `archive.connect`, `archive.fetch_close(venue, market, start, kalshi_get, gateway_get)`, `archive.CLOSE_ENDPOINTS`, `archive.epoch`, `clients.gateway_get`.
- Produces: a printed `VERDICT:` line used by the roadmap's session-start routine.

- [ ] **Step 1: Write the script**

```python
"""Read-only probe for the Polymarket price-history outage seen 2026-10-05 (docs/polymarket-us.md).

Resolved markets returned empty history while open markets were fine. Run once per session until explained:
    python scripts/pm_history_check.py
Prints counts and public quote flags only, never balances or keys.
"""
import json
import time

from sharp_check import archive, clients

PINNED = 10_000


def points(market, end):
    body = archive.fetch_close("polymarket", market, end, None, clients.gateway_get)
    return body.get("history") or []


def pinned(p):
    return float(p["longPrice"]) <= PINNED / 1e6 or 1 - float(p["shortPrice"]) >= 1 - PINNED / 1e6


def main():
    conn = archive.connect()
    pages = [json.loads(b) for (b,) in conn.execute(
        "SELECT body_json FROM raw_pages WHERE venue = 'polymarket' AND endpoint = ? ORDER BY id",
        (archive.CLOSE_ENDPOINTS["polymarket"],))][::20]
    resolved = [(len(b.get("history") or []), len(points(b["market"], b["start_ts"]))) for b in pages]
    back = sum(now > 0 for _, now in resolved)
    print(f"resolved markets: {back} of {len(resolved)} return history again (stored, now): {resolved}")

    now = int(time.time())
    listing = clients.gateway_get("/v1/markets", {"limit": 20, "active": "true", "closed": "false"}).json()
    opened = [m["slug"] for m in listing.get("markets") or [] if m.get("status") == "MARKET_STATUS_OPEN"][:3]
    print(f"open markets, points in the last hour: {[len(points(s, now)) for s in opened]}")

    # Only meaningful once resolved history is back: does Polymarket keep in-game points, and what does a decided
    # parlay leg look like (pinned near 0/1, or no points)? Rule 2 in the spec's parlay-exit section depends on it.
    live = conn.execute("""SELECT b.market_id, MIN(f.ts), b.start_time FROM bets b
                           JOIN fills f ON f.venue = b.venue AND f.market_id = b.market_id
                           WHERE b.venue = 'polymarket' AND b.is_parlay = 0 AND b.is_live = 1 GROUP BY b.bet_id""").fetchall()
    got = [(round((archive.epoch(ts) - archive.epoch(s)) / 3600, 1), len(points(m, archive.epoch(ts) // 60 * 60 - 60)))
           for m, ts, s in live]
    print(f"live singles (hours after start, points before first fill): {got}")
    legs = archive.parlay_clv_legs(conn)
    for bet_id, market, outcome in conn.execute(
            "SELECT bet_id, market_id, outcome FROM bets WHERE venue = 'polymarket' AND is_parlay = 1 AND exit_qty > 0"):
        reason, _, bet_legs = legs[bet_id]
        if reason:
            continue
        (ts,) = conn.execute("SELECT MIN(ts) FROM fills WHERE venue = 'polymarket' AND market_id = ? AND outcome != ?",
                             (market, outcome)).fetchone()
        key = archive.epoch(ts) // 60 * 60 - 60
        rows = []
        for _, leg, _, start in bet_legs:
            hours = round((key - archive.epoch(start)) / 3600, 1)
            pts = points(leg, key) if hours > 0 else []
            rows.append((hours, len(pts), pts and pinned(pts[-1])))
        print(f"parlay exit legs (hours after start, points, latest pinned): {rows}")

    print("VERDICT:", "history is back" if resolved and back == len(resolved)
          else "still empty for resolved markets" if not back else "partial")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it**

Run: `python scripts/pm_history_check.py`
Expected: prints the four sections and a `VERDICT:` line, no traceback. On 2026-10-05 the expected verdict is "still empty for resolved markets".

- [ ] **Step 3: Record the outage in `docs/polymarket-us.md`**

Add after the price-history bullet's sub-bullets:

```markdown
- **Outage watch (from 2026-10-05).** Until 2026-10-05 01:07 UTC, price-history served resolved markets back to 2026-06-22 (the changelog's 45-day market-data retention evidently doesn't apply to it). After that, every resolved market returned `200 {"history":[]}` while open markets were fine; no changelog entry. Probe: `python scripts/pm_history_check.py`, once per session; log each result below. Still empty after ~3 days with no changelog entry → treat as permanent and decide the capture option (spec 2026-10-05-phase8a, "If Polymarket history stays empty"). When it's back, the same run answers whether in-game points exist and how a decided leg looks.
  - 2026-10-05: <verdict from Step 2>
- `fixedInterval` (`INTERVAL_1H|6H|1D|1W|1M|ALL|LIVE`) is an alternative to a timestamp range; don't combine them. `INTERVAL_LIVE` starts 15 minutes before the event.
```

Replace `<verdict from Step 2>` with the actual printed verdict.

- [ ] **Step 4: Add the command to `CLAUDE.md`**

In the Commands bullet, after the smoke sentence, append: ` Polymarket outage probe: \`python scripts/pm_history_check.py\` (once per session until docs/polymarket-us.md says it's resolved).`

- [ ] **Step 5: Commit**

```bash
git add scripts/pm_history_check.py docs/polymarket-us.md CLAUDE.md
git commit -m "Polymarket price-history outage probe and watch log"
```

---

### Task 2: Archive quotes at every fill

**Files:**
- Modify: `sharp_check/archive.py` (add `fill_key`, `spread_targets` after `parlay_clv_legs`; change `sync_closes`)
- Test: `tests/test_archive.py`

**Interfaces:**
- Consumes: `parlay_clv_legs(conn)` → `{bet_id: (reason, opened_ts, [(venue, leg, leg_outcome, leg_start)])}`; `fetch_close`; `store`.
- Produces: `fill_key(venue: str, ts: str) -> int` (Unix seconds); `spread_targets(conn) -> (set[(venue, market, key)], set[(venue, market, key)])` (wanted, kept-if-empty).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_archive.py`:

```python
def add_fill(conn, venue, fill_id, market, outcome, ts):
    conn.execute("INSERT INTO fills VALUES (?, ?, 'o', ?, ?, 1, 500000, 0, 1, ?)", (venue, fill_id, market, outcome, ts))


def test_fill_key_is_the_minute_before_the_fill_and_one_more_on_polymarket():
    e = archive.epoch
    assert archive.fill_key("kalshi", "2026-09-01T10:00:45.123Z") == e("2026-09-01T10:00:00Z")
    assert archive.fill_key("kalshi", "2026-09-01T10:00:00.000000Z") == e("2026-09-01T10:00:00Z")
    assert archive.fill_key("polymarket", "2026-09-01T10:00:45.123456Z") == e("2026-09-01T09:59:00Z")


def test_sync_closes_fetches_each_fill_minute_once_and_keeps_empty_kalshi_exit_legs():
    conn = parlay_db()
    e = archive.epoch
    add_fill(conn, "kalshi", "s1", "KXS-1", "yes", "2026-08-31T10:00:15.000000Z")
    add_fill(conn, "kalshi", "s2", "KXS-1", "no", "2026-08-31T10:00:45.000000Z")         # same minute: one fetch
    add_fill(conn, "polymarket", "p1", "pm-s", "yes", "2026-08-31T10:00:30.000000Z")
    # P1 (eligible) partly cashed out and still open; the exit's fraction is shorter than closed_ts' would be
    conn.execute("UPDATE bets SET exit_qty = 1 WHERE bet_id = 'P1'")
    add_fill(conn, "kalshi", "x1", "P1", "yes", "2026-08-31T10:00:00.123456Z")            # entry: nothing new
    add_fill(conn, "kalshi", "x2", "P1", "no", "2026-09-01T02:00:30.5Z")                  # exit: every leg
    conn.execute("UPDATE bets SET exit_qty = 1, closed_ts = '2026-09-01T03:00:00.000000Z' WHERE bet_id = 'SG'")
    add_fill(conn, "kalshi", "x3", "SG", "no", "2026-09-01T02:30:00Z")                    # same-game: not priced
    exit_key = e("2026-09-01T02:00:00Z")
    calls = []

    def kget(path, params):
        calls.append((path.split("/")[-2], params["end_ts"]))
        empty = params["end_ts"] == exit_key or "KXS-1" in path
        return Resp(200, {"candlesticks": [] if empty else [{"end_period_ts": 1}]})

    def gget(path, params):
        calls.append((params["symbol"], params["timestamp.endTimestamp"]))
        return Resp(200, {"history": [{"timestamp": 1}]})

    archive.sync_closes(conn, kget, gget, "2026-10-01T00:00:00Z")
    assert calls.count(("KXS-1", e("2026-08-31T10:00:00Z"))) == 1
    assert ("pm-s", e("2026-08-31T09:59:00Z")) in calls
    assert ("KXA-1", exit_key) in calls and ("KXB-1", exit_key) in calls
    assert not [c for c in calls if c[1] == e("2026-09-01T02:30:00Z")]
    calls.clear()
    archive.sync_closes(conn, kget, gget, "2026-10-01T00:00:00Z")
    assert ("KXS-1", e("2026-08-31T10:00:00Z")) in calls     # empty single: retried
    assert ("KXA-1", exit_key) not in calls                  # empty Kalshi exit leg: kept, done
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_archive.py -k "fill_key or fill_minute" -v`
Expected: FAIL with `AttributeError: module 'sharp_check.archive' has no attribute 'fill_key'`.

- [ ] **Step 3: Implement**

In `sharp_check/archive.py`, after `parlay_clv_legs`:

```python
def fill_key(venue, ts):
    """Quote time for pricing a fill: the end of the minute before it, so the quote can't include the fill itself.

    Kalshi candles are stamped at their minute's end; a Polymarket point may mark either end, so its key is a minute
    earlier (docs/pnl-rules.md, spread).
    """
    return epoch(ts) // 60 * 60 - (60 if venue == "polymarket" else 0)


def spread_targets(conn):
    """(wanted, keep_empty): (venue, market, key) for every single's fill and, per leg, every exit fill of an eligible
    parlay. keep_empty = the Kalshi exit-leg ones: no candles there means the leg's market had closed (decided)."""
    want = {(v, m, fill_key(v, ts)) for v, m, ts in conn.execute(
        "SELECT venue, market_id, ts FROM fills WHERE (venue, market_id) NOT IN"
        " (SELECT venue, market_id FROM parlay_legs)")}
    keep, legs = set(), parlay_clv_legs(conn)
    for bet_id, venue, market, outcome, opened, closed in conn.execute(
            "SELECT bet_id, venue, market_id, outcome, opened_ts, closed_ts FROM bets"
            " WHERE is_parlay = 1 AND exit_qty > 0"):
        reason, _, bet_legs = legs[bet_id]
        if reason:
            continue
        lo, hi = epoch(opened), closed and epoch(closed)
        for (ts,) in conn.execute("SELECT ts FROM fills WHERE venue = ? AND market_id = ? AND outcome != ?",
                                  (venue, market, outcome)):
            if epoch(ts) < lo or (hi and epoch(ts) > hi):  # epochs: fill and closed_ts fractions differ in length
                continue
            for leg_venue, leg, _, _ in bet_legs:
                t = (leg_venue, leg, fill_key(leg_venue, ts))
                want.add(t)
                if leg_venue == "kalshi":
                    keep.add(t)
    return want, keep
```

In `sync_closes`, update the docstring's second paragraph and add the targets. Replace:

```python
    for reason, opened, legs in parlay_clv_legs(conn).values():
        if reason is None:
            want |= {(v, leg, epoch(t)) for v, leg, _, start in legs for t in (start, opened)}
```

with:

```python
    for reason, opened, legs in parlay_clv_legs(conn).values():
        if reason is None:
            want |= {(v, leg, epoch(t)) for v, leg, _, start in legs for t in (start, opened)}
    fills, keep = spread_targets(conn)
    want |= fills
```

and replace:

```python
            if not (body.get("candlesticks") or body.get("history")):
                continue  # venue data can lag; not stored, so the next run retries
```

with:

```python
            if not (body.get("candlesticks") or body.get("history")) and (venue, market, start) not in keep:
                continue  # venue data can lag; not stored, so the next run retries
```

Docstring addition (after "Singles: at game start. ..."): `Fills: every single's fill and every eligible parlay exit's legs, at fill_key (phase 8a spread).`

- [ ] **Step 4: Run the archive tests**

Run: `python -m pytest tests/test_archive.py -v`
Expected: all PASS (existing `sync_closes` tests have no fills, so their call lists are unchanged).

- [ ] **Step 5: Commit**

```bash
git add sharp_check/archive.py tests/test_archive.py
git commit -m "Archive quotes at every fill for spread pricing"
```

---

### Task 3: Quote and leg pricing helpers

**Files:**
- Modify: `sharp_check/normalize.py` (`pick_close` signature; new `price_index`, `fill_quote`, `leg_value` after `quotes`; constants beside `LEG_MAX_SPREAD`)
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: `price_points(venue, body)`, `archive.CLOSE_ENDPOINTS`, `archive.CLOSE_WINDOW`, `LEG_MAX_SPREAD`.
- Produces:
  - `pick_close(start_s, points, window=archive.CLOSE_WINDOW)` (unchanged default behaviour).
  - `price_index(conn) -> (dict[(venue, market)] -> sorted list[(unix_s, bid|None, ask|None)], set[(venue, market, start_ts)])`.
  - `fill_quote(points, key) -> (yes_mid | None, reason | None)`; reasons `"no quote"`, `"stale quote"`.
  - `leg_value(points, key, fetched: bool, started: bool, yes_value) -> (yes_value_micros, decided: bool) | None`.
  - Constants `SPREAD_MAX_AGE = 300`, `PINNED = 10_000`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_normalize.py`:

```python
def test_fill_quote_needs_a_two_sided_quote_within_5_minutes_before_the_key():
    k = 100_000
    assert normalize.fill_quote([(k, 380_000, 400_000)], k) == (390_000, None)            # ends exactly at the key
    assert normalize.fill_quote([(k + 60, 380_000, 400_000)], k) == (None, "no quote")    # the fill's own minute
    assert normalize.fill_quote([(k - 301, 380_000, 400_000)], k) == (None, "stale quote")
    assert normalize.fill_quote([(k - 4000, 380_000, 400_000)], k) == (None, "no quote")  # outside the hour
    assert normalize.fill_quote([], k) == (None, "no quote")


def test_polymarket_point_at_the_fill_minute_is_excluded():
    m = archive.epoch("2026-09-01T10:00:00Z")
    key = archive.fill_key("polymarket", "2026-09-01T10:00:20Z")
    assert normalize.fill_quote([(m, 380_000, 400_000)], key) == (None, "no quote")
    assert normalize.fill_quote([(m - 60, 380_000, 400_000)], key) == (390_000, None)


def test_price_index_merges_pages_and_records_empty_ones():
    conn = archive.connect(":memory:")
    c = lambda t, b, a: {"end_period_ts": t, "yes_bid": {"close_dollars": b}, "yes_ask": {"close_dollars": a}}
    archive.store(conn, "kalshi", "/markets/{ticker}/candlesticks", {},
                  {"candlesticks": [c(60, "0.40", "0.42"), c(120, "0.41", "0.43")], "market": "K", "start_ts": 120})
    archive.store(conn, "kalshi", "/markets/{ticker}/candlesticks", {},
                  {"candlesticks": [c(120, "0.45", "0.47"), {"end_period_ts": 180}], "market": "K", "start_ts": 180})
    archive.store(conn, "kalshi", "/markets/{ticker}/candlesticks", {}, {"candlesticks": [], "market": "E", "start_ts": 60})
    points, pages = normalize.price_index(conn)
    assert points[("kalshi", "K")] == [(60, 400_000, 420_000), (120, 450_000, 470_000)]   # later page wins; null skipped
    assert pages == {("kalshi", "K", 120), ("kalshi", "K", 180), ("kalshi", "E", 60)}


def test_leg_value_rules():
    k, ONE = 100_000, normalize.ONE
    inplay = (k - 120, 850_000, 870_000)
    won = [inplay, (k, 990_000, ONE)]
    lost = [(k, 0, 10_000)]
    assert normalize.leg_value(won, k, True, True, ONE) == (ONE, True)        # pinned beats the older in-play quote
    assert normalize.leg_value(won, k, True, True, 0) is None                 # pinned disagrees with the result
    assert normalize.leg_value(won, k, True, True, None) is None              # no result yet
    assert normalize.leg_value(lost, k, True, True, 0) == (0, True)
    assert normalize.leg_value([], k, True, True, ONE) == (ONE, True)         # kept empty page after kickoff: closed
    assert normalize.leg_value([], k, False, True, ONE) is None               # never fetched
    assert normalize.leg_value([], k, True, False, ONE) is None               # before kickoff
    assert normalize.leg_value([(k - 60, 290_000, 310_000)], k, True, True, None) == (300_000, False)
    assert normalize.leg_value([(k - 60, 200_000, 260_000)], k, True, True, None) is None   # 6c wide
    assert normalize.leg_value([(k - 301, 290_000, 310_000)], k, True, True, None) is None  # stale
    assert normalize.leg_value([(k - 60, None, 310_000)], k, True, True, None) is None      # one-sided
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_normalize.py -k "fill_quote or fill_minute or price_index or leg_value" -v`
Expected: FAIL with `AttributeError: ... has no attribute 'fill_quote'` (and `price_index`, `leg_value`).

- [ ] **Step 3: Implement**

Change `pick_close`:

```python
def pick_close(start_s, points, window=archive.CLOSE_WINDOW):
    """Latest two-sided quote in [start - window, start]. points: (unix_s, yes_bid, yes_ask), micros or None."""
    ok = [p for p in points if start_s - window <= p[0] <= start_s
          and p[1] is not None and p[2] is not None and 0 < p[1] <= p[2] < ONE]
    return max(ok, default=None)
```

After the `LEG_MAX_AGE` line add:

```python
SPREAD_MAX_AGE = 5 * 60  # seconds: an older quote says nothing about a fill (phase 8a)
PINNED = 10_000          # a leg quoted within 1c of 0 or 1 is decided
```

After `parlay_close_mids` add:

```python
def price_index(conn):
    """Every archived quote page merged per market, and the archived (venue, market, start_ts) pages (empty included).

    points: {(venue, market): [(unix_s, yes_bid, yes_ask)]} sorted by time; a later page wins on the same timestamp.
    Points with neither side quoted (Kalshi's synthetic candle) are skipped.
    """
    merged, pages = {}, set()
    for venue, endpoint in archive.CLOSE_ENDPOINTS.items():
        for (body,) in conn.execute("SELECT body_json FROM raw_pages WHERE venue = ? AND endpoint = ? ORDER BY id",
                                    (venue, endpoint)):
            b = json.loads(body)
            pages.add((venue, b["market"], b["start_ts"]))
            pts = merged.setdefault((venue, b["market"]), {})
            for t, bid, ask in price_points(venue, b):
                if bid is not None or ask is not None:
                    pts[t] = (bid, ask)
    return {k: sorted((t, *q) for t, q in v.items()) for k, v in merged.items()}, pages


def fill_quote(points, key):
    """A single's YES mid just before a fill: (mid, None), or (None, why). Width isn't limited: it's what we measure."""
    q = pick_close(key, points, SPREAD_MAX_AGE)
    if q:
        return round((q[1] + q[2]) / 2), None
    return None, "stale quote" if pick_close(key, points) else "no quote"


def leg_value(points, key, fetched, started, yes_value):
    """A parlay leg's YES value at an exit fill: (value, decided) or None. Rules: docs/pnl-rules.md (parlay exits).

    The latest point in the hour decides: pinned within 1c of 0/1 -> the final result, if known and agreeing (never
    an older in-play quote); none at all on a fetched (kept empty) page after kickoff -> the final result (market
    closed); otherwise it must be two-sided, at most 5c wide and 5 minutes old.
    """
    final = yes_value in (0, ONE)
    recent = [p for p in points if key - archive.CLOSE_WINDOW <= p[0] <= key]
    if not recent:
        return (yes_value, True) if fetched and started and final else None
    t, bid, ask = recent[-1]
    high, low = bid is not None and bid >= ONE - PINNED, ask is not None and ask <= PINNED
    if high or low:
        return (yes_value, True) if final and yes_value == (ONE if high else 0) else None
    if (bid is None or ask is None or not 0 < bid <= ask < ONE or ask - bid > LEG_MAX_SPREAD
            or key - t > SPREAD_MAX_AGE):
        return None
    return round((bid + ask) / 2), False
```

- [ ] **Step 4: Run the normalize tests**

Run: `python -m pytest tests/test_normalize.py -v`
Expected: all PASS (`test_pick_close_takes_latest_usable_quote_in_window` still passes with the default window).

- [ ] **Step 5: Commit**

```bash
git add sharp_check/normalize.py tests/test_normalize.py
git commit -m "Quote index and fill/leg pricing helpers"
```

---

### Task 4: Fill costs and per-bet spread

**Files:**
- Modify: `sharp_check/normalize.py` (SCHEMA, `DERIVED`, `derive_bets`, new `parlay_exit_mid` and `spread_costs`, `rebuild`)
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: Task 2 `archive.fill_key`; Task 3 `price_index`, `fill_quote`, `leg_value`; `archive.parlay_clv_legs`; `bets.fair_entry` (set by `clv_bets`).
- Produces:
  - Table `fill_costs(venue, fill_id, bet_id, role, qty, mid_before, cost, note)`, PK `(venue, fill_id, bet_id)`.
  - `bets.entry_spread_usd INTEGER`, `bets.exit_spread_usd INTEGER` (last two columns, after `fair_entry`).
  - `derive_bets(conn, allocs=None)`: appends `(venue, fill_id, bet_id, role, qty)` to `allocs` when given; return value unchanged.
  - `spread_costs(conn, allocs)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_normalize.py`:

```python
def candle(t, bid, ask):
    return {"end_period_ts": t, "yes_bid": {"close_dollars": bid}, "yes_ask": {"close_dollars": ask}}


def spread_db(fills, candles):
    """fills: (fill_id, market, outcome, qty, price, ts); candles: {market: [candle, ...]}. Runs derive + spread."""
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    for f, m, o, q, p, ts in fills:
        bet_fill(conn, "kalshi", f, m, o, q, p, 0, ts)
    for m, cs in candles.items():
        archive.store(conn, "kalshi", "/markets/{ticker}/candlesticks", {},
                      {"candlesticks": cs, "market": m, "start_ts": cs[-1]["end_period_ts"]})
    allocs = []
    conn.executemany(f"INSERT INTO bets VALUES ({', '.join('?' * 30)})",
                     (b + (None,) * 12 for b in normalize.derive_bets(conn, allocs)))
    normalize.spread_costs(conn, allocs)
    return conn


def test_spread_sums_entries_and_exits_per_bet_signed_and_null_when_unpriced():
    e = archive.epoch
    t = lambda hm: e(f"2026-09-01T{hm}:00Z")
    conn = spread_db(
        [("a", "K", "yes", 10, 400_000, "2026-09-01T10:00:30Z"),    # mid .39: paid 1c
         ("b", "K", "yes", 10, 420_000, "2026-09-01T10:05:10Z"),    # mid .41: paid 1c
         ("c", "K", "no", 20, 450_000, "2026-09-01T11:00:20Z"),     # sold YES at .55 vs mid .57: 2c
         ("d", "N", "no", 5, 600_000, "2026-09-01T10:00:30Z"),      # NO mid .61: -1c, kept signed
         ("u", "U", "yes", 1, 500_000, "2026-09-01T10:00:30Z")],    # no quote
        {"K": [candle(t("10:00"), "0.38", "0.40"), candle(t("10:05"), "0.40", "0.42"), candle(t("11:00"), "0.56", "0.58")],
         "N": [candle(t("10:00"), "0.38", "0.40")]})
    got = {r[0]: r[1:] for r in conn.execute("SELECT market_id, entry_spread_usd, exit_spread_usd FROM bets")}
    assert got == {"K": (200_000, 400_000), "N": (-50_000, None), "U": (None, None)}
    rows = {r[0]: r[1:] for r in conn.execute("SELECT fill_id, role, mid_before, cost, note FROM fill_costs")}
    assert rows["c"] == ("exit", 430_000, 20_000, None)
    assert rows["u"] == ("entry", None, None, "no quote")


def test_flip_fill_is_an_exit_and_a_new_entry():
    t = archive.epoch("2026-09-01T10:00:00Z")
    conn = spread_db([("a", "K", "yes", 10, 400_000, "2026-09-01T09:59:30Z"),
                      ("b", "K", "no", 15, 600_000, "2026-09-01T10:00:30Z")],
                     {"K": [candle(t - 60, "0.38", "0.40"), candle(t, "0.38", "0.40")]})
    rows = conn.execute("SELECT fill_id, bet_id, role, qty, cost FROM fill_costs ORDER BY bet_id, role").fetchall()
    assert rows == [("a", "kalshi:K:1", "entry", 10.0, 10_000), ("b", "kalshi:K:1", "exit", 10.0, -10_000),
                    ("b", "kalshi:K:2", "entry", 5.0, -10_000)]


def test_parlay_exit_prices_legs_and_entry_uses_fair_entry():
    from test_archive import parlay_db
    ONE, e = normalize.ONE, archive.epoch
    conn = parlay_db()
    key = e("2026-09-01T02:00:00Z")
    conn.execute("UPDATE bets SET fair_entry = 300000 WHERE bet_id = 'P1'")
    bet_fill(conn, "kalshi", "in", "P1", "yes", 1, 250_000, 0, "2026-08-31T10:00:00Z")
    bet_fill(conn, "kalshi", "out", "P1", "no", 1, 400_000, 0, "2026-09-01T02:00:30Z")   # sold the parlay at .60
    bet_fill(conn, "kalshi", "sg", "SG", "no", 1, 400_000, 0, "2026-09-01T02:00:30Z")
    store = lambda m, cs: archive.store(conn, "kalshi", "/markets/{ticker}/candlesticks", {},
                                        {"candlesticks": cs, "market": m, "start_ts": key})
    store("KXA-1", [candle(key - 120, "0.85", "0.87"), candle(key, "0.99", "1.00")])   # YES leg, decided won
    store("KXB-1", [candle(key, "0.29", "0.31")])                                      # NO leg: .70 our side
    conn.execute("UPDATE markets SET yes_value = ? WHERE market_id = 'KXA-1'", (ONE,))
    allocs = [("kalshi", "in", "P1", "entry", 1.0), ("kalshi", "out", "P1", "exit", 1.0),
              ("kalshi", "sg", "SG", "exit", 1.0)]
    normalize.spread_costs(conn, allocs)
    rows = {r[0]: r[1:] for r in conn.execute("SELECT fill_id, mid_before, cost, note FROM fill_costs")}
    assert rows["in"] == (300_000, -50_000, None)
    assert rows["out"] == (300_000, 100_000, "1 legs decided")            # fair .70, exit mid (NO side) .30
    assert rows["sg"] == (None, None, "parlay not eligible: same-game legs")
    assert conn.execute("SELECT entry_spread_usd, exit_spread_usd FROM bets WHERE bet_id = 'P1'").fetchone() == (
        -50_000, 100_000)
    conn.execute("UPDATE markets SET yes_value = 0 WHERE market_id = 'KXA-1'")   # pinned high but lost: unpriced
    conn.execute("DELETE FROM fill_costs")
    normalize.spread_costs(conn, allocs)
    assert conn.execute("SELECT cost, note FROM fill_costs WHERE fill_id = 'out'").fetchone() == (
        None, "parlay leg unpriced")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_normalize.py -k "spread or flip or parlay_exit" -v`
Expected: FAIL (`spread_costs` missing; bets has 28 columns).

- [ ] **Step 3: Implement**

SCHEMA: change the end of `bets`:

```sql
    fair_entry INTEGER,         -- parlays: product of leg mids at placement, your side of each leg
    entry_spread_usd INTEGER,   -- phase 8a: Σ qty × (price − mid before the fill) over entry fills; NULL if any unpriced
    exit_spread_usd INTEGER     -- same over exit fills; NULL if any unpriced or no exits
);
```

Add after the `quotes` table:

```sql
CREATE TABLE IF NOT EXISTS fill_costs (
    venue TEXT NOT NULL,
    fill_id TEXT NOT NULL,
    bet_id TEXT NOT NULL,
    role TEXT NOT NULL,         -- entry | exit; a fill that flips a position has a row per bet
    qty REAL NOT NULL,          -- contracts of this fill in this role
    mid_before INTEGER,         -- mid of the fill's outcome just before it, micro-dollars; NULL when not computable
    cost INTEGER,               -- price - mid_before per contract, fees excluded; positive = paid over mid
    note TEXT,                  -- why cost is NULL, or how many parlay legs were priced as decided
    PRIMARY KEY (venue, fill_id, bet_id)
);
```

`DERIVED`: add `"fill_costs"` before `"closing_lines"`.

`derive_bets`: signature `def derive_bets(conn, allocs=None):`; add to the docstring `allocs, if given, gets (venue, fill_id, bet_id, entry|exit, qty) per fill per bet it touches.`; at the top of the body `allocs = [] if allocs is None else allocs`. Change the query and loop header:

```python
    rows = conn.execute("SELECT venue, fill_id, market_id, outcome, qty, price, fee, ts FROM fills"
                        " ORDER BY venue, market_id, ts")
    for venue, fill_id, market, outcome, qty, price, fee, ts in rows:
```

Inside the opposite-side branch, right after `out = min(qty, held)`:

```python
            allocs.append((venue, fill_id, f"{venue}:{market}:{cur['n']}", "exit", out))
```

After the `if cur is None:` block (before `cur["entry_qty"] += qty`):

```python
        allocs.append((venue, fill_id, f"{venue}:{market}:{cur['n']}", "entry", qty))
```

Add after `leg_value`:

```python
def parlay_exit_mid(clv_legs, ts, points, pages, final):
    """Mid of the side an exit fill acquires (the parlay's opposite): ONE − Π leg values on the parlay's side."""
    reason, _, legs = clv_legs
    if reason:
        return None, f"parlay not eligible: {reason}"
    fair, decided = 1.0, 0
    for venue, leg, side, start in legs:
        key = archive.fill_key(venue, ts)
        r = leg_value(points.get((venue, leg), []), key, (venue, leg, key) in pages, archive.epoch(start) < key,
                      final.get((venue, leg)))
        if r is None:
            return None, "parlay leg unpriced"
        fair *= (r[0] if side == "yes" else ONE - r[0]) / ONE
        decided += r[1]
    return round((1 - fair) * ONE), f"{decided} legs decided" if decided else None


def spread_costs(conn, allocs):
    """Price each fill against the mid just before it and sum per bet (docs/pnl-rules.md, spread). Needs clv_bets
    first: a parlay entry's mid is its fair_entry."""
    points, pages = price_index(conn)
    fills = {(v, f): (m, o, p, ts) for v, f, m, o, p, ts in conn.execute(
        "SELECT venue, fill_id, market_id, outcome, price, ts FROM fills")}
    bets = {b: (p, fe) for b, p, fe in conn.execute("SELECT bet_id, is_parlay, fair_entry FROM bets")}
    final = {(v, m): y for v, m, y in conn.execute("SELECT venue, market_id, yes_value FROM markets")}
    clv_legs = archive.parlay_clv_legs(conn)
    rows = []
    for venue, fill_id, bet_id, role, qty in allocs:
        market, outcome, price, ts = fills[(venue, fill_id)]
        is_parlay, fair_entry = bets[bet_id]
        if not is_parlay:
            mid, note = fill_quote(points.get((venue, market), []), archive.fill_key(venue, ts))
            mid = mid if mid is None or outcome == "yes" else ONE - mid
        elif role == "entry":
            mid, note = (fair_entry, None) if fair_entry is not None else (None, "no fair entry")
        else:
            mid, note = parlay_exit_mid(clv_legs[bet_id], ts, points, pages, final)
        rows.append((venue, fill_id, bet_id, role, qty, mid, None if mid is None else price - mid, note))
    conn.executemany("INSERT INTO fill_costs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    sums = {}
    for _, _, bet_id, role, qty, _, cost, _ in rows:
        s = sums.setdefault((bet_id, role), [0.0, True])
        s[0] += 0 if cost is None else qty * cost
        s[1] &= cost is not None
    total = lambda b, r: round(sums[(b, r)][0]) if (b, r) in sums and sums[(b, r)][1] else None
    conn.executemany("UPDATE bets SET entry_spread_usd = ?, exit_spread_usd = ? WHERE bet_id = ?",
                     [(total(b, "entry"), total(b, "exit"), b) for b in {b for b, _ in sums}])
```

`rebuild`: replace the bets insert with

```python
        allocs = []
        conn.executemany(f"INSERT INTO bets VALUES ({', '.join('?' * 30)})",
                         (b + (None,) * 12 for b in derive_bets(conn, allocs)))
```

and after `clv_bets(conn)` add `spread_costs(conn, allocs)`.

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all PASS. If `test_derive_bets_matches_known_app_pnl` or `test_closed_ts_is_settlement_or_last_exit_fill` break, the loop header change mis-ordered a tuple: they call `derive_bets(conn)` with no `allocs`.

- [ ] **Step 5: Commit**

```bash
git add sharp_check/normalize.py tests/test_normalize.py
git commit -m "Spread paid per fill and per bet (fill_costs)"
```

---

### Task 5: Habits aggregates

**Files:**
- Modify: `sharp_check/habits.py` (new `spreads`, `unpriced_fills` after `maker_fills`; `cash_outs`)
- Test: `tests/test_habits.py`

**Interfaces:**
- Consumes: `fill_costs`, `bets.entry_spread_usd`, `bets.exit_spread_usd`, `bets.is_live`, `metrics.labels(conn)`.
- Produces:
  - `spreads(conn) -> {"rows": [(venue, "singles"|"parlays", entry_usd, exit_usd, cents_per_contract|None, priced_fills, all_fills)], "timing": {"pregame": cents|None, "live": cents|None}, "bets": [{"bet_id", "label", "venue", "entry", "exit", "has_exit", "note"}]}`.
  - `unpriced_fills(conn) -> [(bet_id, ts, role, note)]`, newest first.
  - `cash_outs` rows gain `"spread"` (micro-dollars or None) and `"fair_exit"` (per contract or None); summaries gain `"spread"` (sum over priced) and `"spread_known"` (count).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_habits.py`:

```python
def cost(conn, venue, fill_id, bet_id, role, qty, c, note=None):
    conn.execute("INSERT INTO fill_costs VALUES (?, ?, ?, ?, ?, NULL, ?, ?)", (venue, fill_id, bet_id, role, qty, c, note))


def test_spreads_per_venue_type_and_timing_count_each_fill_once():
    conn = db()
    bet(conn, "S", "t", 1, start="s")                       # pregame single (is_live 0)
    bet(conn, "L", "t", 1, start="s")
    conn.execute("UPDATE bets SET is_live = 1 WHERE bet_id = 'L'")
    bet(conn, "X", "t", 1, is_parlay=1, venue="polymarket")
    fill(conn, "kalshi", "3", 1, 500_000, 0)                # fill_id kalshi3, ts 't'
    cost(conn, "kalshi", "f1", "S", "entry", 10, 10_000)
    cost(conn, "kalshi", "f2", "S", "exit", 10, 20_000)
    cost(conn, "kalshi", "f2", "L", "entry", 5, 40_000)     # flip: f2 counted once
    cost(conn, "kalshi", "kalshi3", "S", "entry", 1, None, "no quote")
    cost(conn, "polymarket", "p1", "X", "entry", 4, 50_000)
    got = habits.spreads(conn)
    rows = {(r[0], r[1]): r[2:] for r in got["rows"]}
    assert rows[("kalshi", "singles")] == (300_000, 200_000, 2.0, 2, 3)
    assert rows[("polymarket", "parlays")] == (200_000, 0, 5.0, 1, 1)
    assert got["timing"] == {"pregame": 1.5, "live": 4.0}
    assert habits.unpriced_fills(conn) == [("S", "t", "entry", "no quote")]


def test_cash_outs_carry_exit_spread_and_fair_exit():
    conn = db()
    conn.execute("INSERT INTO markets (venue, market_id, yes_value) VALUES ('kalshi', 'W', 1000000)")
    conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, entry_qty, avg_entry,
                    stake, exit_qty, exit_proceeds, exit_fees, payout, realized_pnl, status, closed_ts,
                    exit_spread_usd) VALUES ('c1', 'kalshi', 'W', 'yes', 0, 't', 10, 500000, 5000000, 10, 6000000,
                    0, 0, 1000000, 'closed_early', '2026-09-01T00:00:00.000000Z', 200000)""")
    c = habits.cash_outs(conn)
    r = c["rows"][0]
    assert (r["spread"], r["fair_exit"]) == (200_000, 620_000)    # sold at .60, 2c under mid
    assert (c["summary"]["all"]["spread"], c["summary"]["all"]["spread_known"]) == (200_000, 1)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `python -m pytest tests/test_habits.py -k "spreads or exit_spread" -v`
Expected: FAIL (`habits.spreads` missing; `KeyError: 'spread'`).

- [ ] **Step 3: Implement**

In `sharp_check/habits.py`, after `maker_fills`:

```python
def spreads(conn):
    """Spread paid (docs/pnl-rules.md, spread): per (venue, singles|parlays) entry and exit $, ¢ per priced contract and
    priced fills x of y; ¢ per contract pregame vs live; and each bet's entry and exit spread."""
    groups, timing = {}, {}
    for venue, is_parlay, is_live, fill_id, role, qty, c in conn.execute(
            "SELECT f.venue, b.is_parlay, b.is_live, f.fill_id, f.role, f.qty, f.cost"
            " FROM fill_costs f JOIN bets b USING (bet_id)"):
        g = groups.setdefault((venue, "parlays" if is_parlay else "singles"),
                              {"entry": 0.0, "exit": 0.0, "qty": 0.0, "priced": set(), "all": set()})
        g["all"].add(fill_id)
        if c is None:
            continue
        g["priced"].add(fill_id)
        g[role] += qty * c
        g["qty"] += qty
        if is_live in (0, 1):
            t = timing.setdefault("live" if is_live else "pregame", [0.0, 0.0])
            t[0] += qty * c
            t[1] += qty
    cents = lambda total, qty: round(total / qty / 10_000, 2) if qty else None
    names = metrics.labels(conn)
    return {
        "rows": [(v, split, round(g["entry"]), round(g["exit"]), cents(g["entry"] + g["exit"], g["qty"]),
                  len(g["priced"]), len(g["all"])) for (v, split), g in sorted(groups.items())],
        "timing": {k: cents(*timing[k]) if k in timing else None for k in ("pregame", "live")},
        "bets": [{"bet_id": b, "label": names[b][0], "venue": v, "entry": e, "exit": x, "has_exit": bool(hx),
                  "note": n} for b, v, e, x, hx, n in conn.execute(
            """SELECT b.bet_id, b.venue, b.entry_spread_usd, b.exit_spread_usd, b.exit_qty > 0,
                      (SELECT group_concat(note, '; ') FROM fill_costs c
                       WHERE c.bet_id = b.bet_id AND c.cost IS NOT NULL AND c.note IS NOT NULL)
               FROM bets b WHERE b.bet_id IN (SELECT bet_id FROM fill_costs) ORDER BY b.opened_ts DESC""")],
    }


def unpriced_fills(conn):
    """[(bet_id, fill ts, role, reason)] for every fill without a spread, newest first."""
    return conn.execute("""SELECT c.bet_id, f.ts, c.role, c.note FROM fill_costs c JOIN fills f USING (venue, fill_id)
                           WHERE c.cost IS NULL ORDER BY f.ts DESC""").fetchall()
```

In `cash_outs`: add `b.exit_spread_usd` to the SELECT (after `b.realized_pnl`), unpack it as `spread` (`bet_id, closed, is_parlay, status, outcome, qty, entry, proceeds, fees, pnl, spread, yes = r`), and add to the row dict:

```python
                     "spread": spread,
                     "fair_exit": None if spread is None else round((proceeds + spread) / qty),
```

(`spread = Σ qty × (mid − exit price)` on your side, so mid per contract = (proceeds + spread) / qty.) In `summary(rs)` add:

```python
                "spread": sum(x["spread"] for x in rs if x["spread"] is not None),
                "spread_known": sum(x["spread"] is not None for x in rs),
```

- [ ] **Step 4: Run the habits tests**

Run: `python -m pytest tests/test_habits.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sharp_check/habits.py tests/test_habits.py
git commit -m "Habits: spread aggregates, unpriced fills, exit spread on cash-outs"
```

---

### Task 6: Dashboard

**Files:**
- Modify: `sharp_check/app.py` (`cash_out_section`, `habits_tab`, `data_tab`)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: Task 5 `habits.spreads`, `habits.unpriced_fills`, `cash_outs` `"spread"`/`"fair_exit"`/`"spread_known"`; app helpers `table`, `fold`, `usd`, `cents`, `tone`, `when`.
- Produces: rendered sections only.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_app.py`:

```python
def text(node):
    if node is None:
        return ""
    if isinstance(node, (list, tuple)):
        return " ".join(text(n) for n in node)
    if not hasattr(node, "children"):
        return str(node)
    return text(node.children)


def test_habits_and_data_health_show_spread():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, is_live, entry_qty,
                    avg_entry, stake, exit_qty, exit_proceeds, exit_fees, payout, status, entry_spread_usd)
                    VALUES ('kalshi:K:1', 'kalshi', 'K', 'yes', 0, '2026-09-01T10:00:00Z', 0, 10, 400000, 4000000,
                    0, 0, 0, 0, 'open', 100000)""")
    conn.execute("INSERT INTO fills VALUES ('kalshi', 'f1', 'o', 'K', 'yes', 10, 400000, 0, 1, '2026-09-01T10:00:00Z')")
    conn.execute("INSERT INTO fills VALUES ('kalshi', 'f2', 'o', 'K', 'yes', 1, 400000, 0, 1, '2026-09-01T10:01:00Z')")
    conn.execute("INSERT INTO fill_costs VALUES ('kalshi', 'f1', 'kalshi:K:1', 'entry', 10, 390000, 10000, NULL)")
    conn.execute("INSERT INTO fill_costs VALUES ('kalshi', 'f2', 'kalshi:K:1', 'entry', 1, NULL, NULL, 'stale quote')")
    habits_text = text(app.habits_tab(conn))
    assert "Spread at entry" in habits_text and "1 of 2" in habits_text
    assert "Fills without a spread" in text(app.data_tab(conn)) and "stale quote" in text(app.data_tab(conn))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python -m pytest tests/test_app.py -k spread -v`
Expected: FAIL on `"Spread at entry" in habits_text`.

- [ ] **Step 3: Implement**

In `habits_tab`, after the maker-saving note `html.P(...)` (the last element of the returned list), add:

```python
        *spread_section(conn),
```

and add above `habits_tab`:

```python
def spread_section(conn):
    s = habits.spreads(conn)
    t = s["timing"]
    per = lambda c: "–" if c is None else f"{c:+.2f}¢"
    return [
        table(("Venue", "Type", "Spread at entry", "Spread at exit", "Per contract", "Priced fills"),
              [(v, split, usd(e), usd(x), per(c), f"{n} of {total}")
               for v, split, e, x, c, n, total in s["rows"]], minor=(4, 5)),
        html.P(f"Spread = fill price minus the mid in the minute before the fill, your side, fees excluded; positive ="
               f" paid over mid, so it adds to the fees above. Parlay entries use the legs' fair price (the markup on"
               f" the CLV tab). Per contract: pregame {per(t['pregame'])}, live {per(t['live'])} (live quotes move"
               f" fast, so noisier). Unpriced fills are listed on Data health.", className="note"),
        fold(f"Show all {len(s['bets'])} bets", table(
            ("Bet", "Entry spread", "Exit spread", "Note"),
            [(b["label"], "–" if b["entry"] is None else usd(b["entry"]),
              ("–" if b["exit"] is None else usd(b["exit"])) if b["has_exit"] else "", b["note"] or "")
             for b in s["bets"]])),
    ]
```

Spread cells are deliberately uncoloured: `tone` (app.py:33) colours positive green, and a positive spread is a cost.

In `cash_out_section`:
- summary rows: add a cell after `value_cell(s)`:
  `html.Div([usd(s["spread"]), html.Div(f"{s['spread_known']} of {s['n']} priced", className="sub")])`
  and add `"Exit spread"` to the header after `"Value vs holding"`; shift `minor=(1, 2, 5)` to `minor=(1, 2, 6)`.
- per-bet fold: add headers `"Fair exit", "Spread"` after `"Exit"`, cells `"–" if r["fair_exit"] is None else cents(r["fair_exit"])` and `"–" if r["spread"] is None else usd(r["spread"])` after `cents(r["exit_price"])`; change `minor=(0, 2, 3, 5, 6)` to `minor=(0, 2, 3, 4, 7, 8)`.
- note: replace `" breaks even on average, and its real cost is fees plus the spread, which isn't measured yet."` with `" breaks even on average; its real cost is exit fees plus the exit spread (fair exit = your side's mid in the minute before the sale)."`

In `data_tab`, before the `other = ...` unclassified-codes block:

```python
    unpriced = habits.unpriced_fills(conn)
    if unpriced:
        out += [html.H3("Fills without a spread"),
                table(("Bet", "Fill (UTC)", "Role", "Reason"),
                      [(names.get(b, (b,))[0], when(ts), role, note) for b, ts, role, note in unpriced])]
```

- [ ] **Step 4: Run the full suite**

Run: `python -m pytest tests/ -q`
Expected: all PASS (including `test_empty_db_renders_every_tab_and_the_tiles`).

- [ ] **Step 5: Commit**

```bash
git add sharp_check/app.py tests/test_app.py
git commit -m "Habits and Data health: spread at entry and exit"
```

---

### Task 7: Real data, docs, roadmap

**Files:**
- Modify: `docs/pnl-rules.md`, `docs/data-model.md`, `docs/roadmap.md`, `docs/polymarket-us.md` (log line)

- [ ] **Step 1: Run on real data**

Run: `python -m sharp_check.archive && python -m sharp_check.reconcile && python scripts/pm_history_check.py`
Expected: archive prints `closes fetched=N` with N > 0 on the first run (fill targets); reconcile prints only OK lines.

- [ ] **Step 2: Check coverage and sanity**

Run:

```bash
sqlite3 data/sharp_check.db "SELECT c.venue, b.is_parlay, c.role, COUNT(*), SUM(c.cost IS NOT NULL), ROUND(SUM(c.qty*c.cost)/1e6,2) FROM fill_costs c JOIN bets b USING (bet_id) GROUP BY 1,2,3;"
sqlite3 data/sharp_check.db "SELECT note, COUNT(*) FROM fill_costs WHERE cost IS NULL GROUP BY 1;"
sqlite3 data/sharp_check.db "SELECT MIN(cost), MAX(cost) FROM fill_costs;"
```

Expected: every Kalshi single fill priced except genuinely missing quotes; Polymarket singles mostly "no quote" during the outage (about 10 of 48 priced from close pages); per-contract costs mostly between −2¢ and +5¢. Any |cost| > 20¢ on a single → inspect that fill's quote before going on (likely a wrong side or key).

- [ ] **Step 3: Look at the dashboard**

Run `python -m sharp_check.app`, open http://127.0.0.1:8050, check Habits (spread table, fold, cash-out columns) and Data health at desktop and phone width (Chrome devtools, 390 px): no page-level horizontal scroll.

- [ ] **Step 4: Update docs**

`docs/pnl-rules.md`: add a "Spread (phase 8a)" section: the cost rule (`price − mid(outcome)`, signed, fees excluded), quote key (Kalshi `m`, Polymarket `m − 60`), 5-minute staleness, merged pages, parlay entries = `fair_entry`, parlay exit leg rules (pinned → final result if agreeing; no candles on a kept Kalshi page after kickoff → final result; else ≤ 5¢, ≤ 5 min), NULL rules.

`docs/data-model.md`: `fill_costs` (columns and PK, one row per fill per bet) and `bets.entry_spread_usd` / `exit_spread_usd`.

`docs/roadmap.md`:
- Phase table: split row 8 into `8a | Spread at entry and exit` (exit check: spread at entry and exit shown per bet and summed per venue with coverage; unpriced fills listed) and `8b | Cross-venue line shopping` (the original line-shopping exit check).
- Current phase: `8a in progress (Kalshi done YYYY-MM-DD; Polymarket waits on the price-history outage)`.
- Decisions: phase 8 split (2026-10-05) with the reason; no Habits venue switch (2026-10-05), possible follow-up.
- Next up: replace "Resume here" with: run archive → reconcile → `python scripts/pm_history_check.py`, log the verdict in `docs/polymarket-us.md`; when history is back, check the probe's live-singles and decided-leg lines against spec rule 2 (Polymarket decided legs) and adjust `leg_value`/`spread_targets` if Polymarket shows "no points" for decided legs (it would then need kept empty pages too); then run the exit check and mark 8a done. Still empty after ~3 days → decide the capture option. Then 8b.
- Record today's real-data numbers (Kalshi entry/exit spread totals and ¢ per contract, coverage).

`docs/polymarket-us.md`: append the dated verdict line from Step 1.

- [ ] **Step 5: Run the tests and commit**

Run: `python -m pytest tests/ -q`
Expected: all PASS.

```bash
git add docs/
git commit -m "Phase 8a docs: spread rules, fill_costs, roadmap split and Polymarket watch"
```

---

### Task 8 (later session, once Polymarket history is back): finish 8a

Not run in the same session as Tasks 1–7 unless the probe already says "history is back".

- [ ] **Step 1:** `python scripts/pm_history_check.py`. Continue only on `VERDICT: history is back`.
- [ ] **Step 2:** Read the "live singles" line: points > 0 → in-game quotes exist (no change). Read the "parlay exit legs" line for legs > ~4 h after start: pinned → rule 1 already covers Polymarket; 0 points → add Polymarket to `keep` in `archive.spread_targets` (drop the `if leg_venue == "kalshi"` condition), with a test mirroring the Kalshi kept-empty one.
- [ ] **Step 3:** `python -m sharp_check.archive`, re-run Task 7 Step 2 queries: Polymarket fills now priced.
- [ ] **Step 4:** Exit check: Habits shows spread at entry and exit per bet and summed per venue with priced x of y; Data health lists the rest. Update roadmap (8a done, numbers, cash-out conclusion) and the outage log; mark the outage resolved and remove the probe from the session start (roadmap "Every session starts with" and the CLAUDE.md Commands sentence); commit.
- [ ] **Step 5:** Read-only subagent logic review of the 8a diff, then fix findings (standing practice after each phase).
