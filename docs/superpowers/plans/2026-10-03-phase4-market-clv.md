# Phase 4 Market CLV Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-bet and aggregate market CLV (entry vs the venue's own mid at game start) for pregame singles on games.

**Architecture:** `archive.sync_closes` fetches a 1-minute price window ending at kickoff for each eligible bet (after `archive` runs `normalize.rebuild`) and stores it in `raw_pages`. `normalize` builds `closing_lines` from those pages and fills `bets.close_mid/clv/clv_usd`. `metrics.clv` aggregates; `app.py` shows a CLV section.

**Tech Stack:** Python 3.12, SQLite, requests, Plotly Dash, pytest.

**Spec:** `docs/superpowers/specs/2026-10-03-phase4-market-clv-design.md`

## Global Constraints

- Read-only. Only GET endpoints.
- Polymarket US only: `gateway.polymarket.us/v1/price-history`, params `symbol`, `fidelity=1`, `timestamp.startTimestamp`, `timestamp.endTimestamp`.
- Money as integer micro-dollars (`ONE = 1_000_000`); timestamps UTC, bets use `%Y-%m-%dT%H:%M:%S.%fZ`.
- `raw_pages` is the source of truth; `closing_lines` and bet CLV columns are rebuilt from it.
- Eligible: `is_parlay = 0 AND link = 'game' AND is_live = 0 AND market_type IN ('moneyline','spread','total','prop')`; voids get no CLV.
- Close window: `[start − 3600 s, start]`; usable quote `0 < bid <= ask < ONE`.
- Fixtures scrubbed (`scripts/smoke.py` `scrub`).

## Review Focus

- A Kalshi synthetic "latest before start" candle with null OHLC must be skipped, not crash.
- Two markets returning identical/empty bodies must both be recorded as fetched (body carries market + start_ts).
- A postponed game (new `start_time`) must be refetched, and the old page must not pair with the new start.
- NO-side bet CLV uses `ONE − yes_mid`; a positive CLV means the price moved toward your side.
- One market failing to fetch must not stop the others or the rest of archive.

---

### Task 1: `closing_lines` from archived price pages

**Files:**
- Create: `tests/fixtures/kalshi_candles_hist.json`, `tests/fixtures/kalshi_candles_live.json`, `tests/fixtures/pm_price_history.json` (last 3 points each, scrubbed)
- Modify: `sharp_check/normalize.py` (SCHEMA, DERIVED, new `pick_close`, `closing_lines`, `rebuild`)
- Modify: `sharp_check/archive.py` (constants `CLV_ELIGIBLE`, `CLOSE_ENDPOINTS`, `CLOSE_WINDOW`)
- Test: `tests/test_normalize.py`

**Interfaces:**
- Produces: `archive.CLV_ELIGIBLE: str` (SQL predicate on `bets`), `archive.CLOSE_ENDPOINTS = {"kalshi": "/markets/{ticker}/candlesticks", "polymarket": "/v1/price-history"}`, `archive.CLOSE_WINDOW = 3600`; `normalize.pick_close(start_s, points) -> (ts, bid, ask) | None`; table `closing_lines(venue, market_id, start_time, quote_ts, yes_bid, yes_ask, yes_mid)`.
- Stored page body: response JSON plus `"market"` and `"start_ts"` (Unix seconds).

- [ ] **Step 1: Save fixtures** with a one-off scratch script using the probe calls from the spec; write `scrub(points[-3:])`.
- [ ] **Step 2: Failing tests**

```python
def test_pick_close_takes_latest_usable_quote_in_window():
    s = 10_000
    pts = [(s - 4000, 400_000, 500_000),   # stale
           (s - 60, 450_000, 470_000),
           (s, None, None),                # synthetic candle
           (s, 0, 470_000),                # one-sided
           (s + 60, 460_000, 480_000)]     # after start
    assert normalize.pick_close(s, pts) == (s - 60, 450_000, 470_000)
    assert normalize.pick_close(s, pts[:1]) is None


def test_closing_lines_from_both_venues_and_both_kalshi_spellings():
    conn = archive.connect(":memory:")
    hist, live, pm = load("kalshi_candles_hist"), load("kalshi_candles_live"), load("pm_price_history")
    for name, venue, cs in (("H", "kalshi", hist), ("L", "kalshi", live)):
        archive.store(conn, venue, "/markets/{ticker}/candlesticks", {},
                      {"candlesticks": cs, "market": name, "start_ts": cs[-1]["end_period_ts"]})
    archive.store(conn, "polymarket", "/v1/price-history", {},
                  {"history": pm, "market": "P", "start_ts": pm[-1]["timestamp"]})
    normalize.rebuild(conn)
    rows = {r[0]: r[1:] for r in conn.execute("SELECT market_id, yes_bid, yes_ask, yes_mid FROM closing_lines")}
    assert set(rows) == {"H", "L", "P"}
    p = pm[-1]
    bid, ask = normalize.ONE - normalize.micros(str(p["shortPrice"])), normalize.micros(str(p["longPrice"]))
    assert rows["P"] == (bid, ask, round((bid + ask) / 2))
```

- [ ] **Step 3: Run** `python -m pytest tests/test_normalize.py -k close -v` → FAIL.
- [ ] **Step 4: Implement**

```python
# archive.py
# Bets that get market CLV (docs/pnl-rules.md). Voids are dropped later, once settled.
CLV_ELIGIBLE = ("is_parlay = 0 AND link = 'game' AND is_live = 0"
                " AND market_type IN ('moneyline', 'spread', 'total', 'prop')")
CLOSE_ENDPOINTS = {"kalshi": "/markets/{ticker}/candlesticks", "polymarket": "/v1/price-history"}
CLOSE_WINDOW = 3600  # seconds before start; an older quote is stale
```

```python
# normalize.py SCHEMA
CREATE TABLE IF NOT EXISTS closing_lines (
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    start_time TEXT NOT NULL,   -- the bet's game start this close is for
    quote_ts TEXT NOT NULL,
    yes_bid INTEGER NOT NULL,
    yes_ask INTEGER NOT NULL,
    yes_mid INTEGER NOT NULL,
    PRIMARY KEY (venue, market_id, start_time)
);

def pick_close(start_s, points):
    """Latest two-sided quote in [start - CLOSE_WINDOW, start]. points: (unix_s, yes_bid, yes_ask), micros or None."""
    ok = [p for p in points if start_s - archive.CLOSE_WINDOW <= p[0] <= start_s
          and p[1] is not None and p[2] is not None and 0 < p[1] <= p[2] < ONE]
    return max(ok, default=None)


def kalshi_close(side):
    """Candle bid/ask close. Historical tier says `close`, live `close_dollars`; null on the synthetic candle."""
    v = (side or {}).get("close_dollars", (side or {}).get("close"))
    return None if v is None else micros(v)


def price_points(venue, body):
    if venue == "kalshi":
        return [(c["end_period_ts"], kalshi_close(c.get("yes_bid")), kalshi_close(c.get("yes_ask")))
                for c in body.get("candlesticks") or []]
    # Polymarket points are asks on both sides: YES bid = 1 - short ask.
    return [(p["timestamp"], None if p.get("shortPrice") is None else ONE - micros(str(p["shortPrice"])),
             None if p.get("longPrice") is None else micros(str(p["longPrice"]))) for p in body.get("history") or []]


def closing_lines(conn):
    latest = {}
    for venue, endpoint in archive.CLOSE_ENDPOINTS.items():
        for (body,) in conn.execute("SELECT body_json FROM raw_pages WHERE venue = ? AND endpoint = ? ORDER BY id",
                                    (venue, endpoint)):
            b = json.loads(body)
            latest[(venue, b["market"], iso_epoch(b["start_ts"]))] = pick_close(b["start_ts"], price_points(venue, b))
    for (venue, market, start), q in latest.items():  # latest page wins, even if it has no usable quote
        if q:
            yield venue, market, start, iso_epoch(q[0]), q[1], q[2], round((q[1] + q[2]) / 2)
```

Add `"closing_lines"` to `DERIVED`; in `rebuild` after `link_bets(conn)`:
`conn.executemany("INSERT INTO closing_lines VALUES (?, ?, ?, ?, ?, ?, ?)", list(closing_lines(conn)))`.

- [ ] **Step 5: Run** full `python -m pytest tests/` → PASS. **Step 6: Commit** "Phase 4: closing lines from archived price pages".

### Task 2: CLV columns on `bets`

**Files:** Modify `sharp_check/normalize.py` (bets SCHEMA + insert width, new `clv_bets`); Test `tests/test_normalize.py`.

**Interfaces:** Consumes `closing_lines`, `archive.CLV_ELIGIBLE`. Produces `bets.close_mid`, `bets.clv` (micros/contract), `bets.clv_usd` (micros), NULL when no CLV.

- [ ] **Step 1: Failing test**

```python
def test_clv_uses_your_side_mid_and_skips_ineligible_and_void():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    def bet(bid, outcome, status="settled", is_live=0):
        conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, is_live, entry_qty,
                        avg_entry, stake, exit_qty, exit_proceeds, exit_fees, payout, status, start_time, link,
                        market_type) VALUES (?, 'kalshi', ?, ?, 0, 't', ?, 10, 400000, 0, 0, 0, 0, 0, ?, 'S', 'game',
                        'moneyline')""", (bid, bid, outcome, is_live, status))
        conn.execute("INSERT INTO closing_lines VALUES ('kalshi', ?, 'S', 'q', 440000, 460000, 450000)", (bid,))
    bet("Y", "yes"); bet("N", "no"); bet("V", "yes", "void"); bet("L", "yes", is_live=1)
    normalize.clv_bets(conn)
    got = {r[0]: r[1:] for r in conn.execute("SELECT bet_id, close_mid, clv, clv_usd FROM bets")}
    assert got["Y"] == (450_000, 50_000, 500_000)
    assert got["N"] == (550_000, 150_000, 1_500_000)
    assert got["V"] == got["L"] == (None, None, None)
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement**

bets SCHEMA, after `market_type TEXT`:
```sql
    market_type TEXT,
    close_mid INTEGER,          -- phase 4: venue mid at start_time on your side
    clv INTEGER,                -- close_mid - avg_entry, micros per contract
    clv_usd INTEGER             -- entry_qty * clv, micros
```
Insert: `({', '.join('?' * 24)})` with `b + (None,) * 7`.

```python
def clv_bets(conn):
    """Market CLV: entry vs the venue's own mid at start, your side. NULL when not eligible, void, or no close."""
    conn.execute(f"""UPDATE bets SET close_mid = (
                         SELECT CASE bets.outcome WHEN 'yes' THEN c.yes_mid ELSE {ONE} - c.yes_mid END
                         FROM closing_lines c WHERE (c.venue, c.market_id, c.start_time)
                                                    = (bets.venue, bets.market_id, bets.start_time))
                     WHERE {archive.CLV_ELIGIBLE} AND status != 'void'""")
    conn.execute("UPDATE bets SET clv = close_mid - avg_entry,"
                 " clv_usd = CAST(ROUND(entry_qty * (close_mid - avg_entry)) AS INTEGER) WHERE close_mid IS NOT NULL")
```
Call `clv_bets(conn)` in `rebuild` after inserting `closing_lines`.

- [ ] **Step 4: Run** full suite → PASS. **Step 5: Commit** "Phase 4: CLV columns on bets".

### Task 3: `archive.sync_closes`

**Files:** Modify `sharp_check/archive.py` (`epoch`, `fetch_close`, `sync_closes`, `__main__`); Test `tests/test_archive.py`.

**Interfaces:** Consumes `bets` table, `CLV_ELIGIBLE`, `CLOSE_ENDPOINTS`. Produces pages with body `{..., "market", "start_ts"}`; `sync_closes(conn, kalshi_get, gateway_get, now=None) -> bool`.

- [ ] **Step 1: Failing test**

```python
def test_sync_closes_fetches_once_per_start_with_fallback_and_survives_errors():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    def bet(venue, market, start, mtype="prop"):
        conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, is_live, entry_qty,
                        avg_entry, stake, exit_qty, exit_proceeds, exit_fees, payout, status, start_time, link,
                        market_type) VALUES (?, ?, ?, 'yes', 0, 't', 0, 1, 1, 1, 0, 0, 0, 0, 'settled', ?, 'game', ?)""",
                     (f"{venue}:{market}", venue, market, start, mtype))
    bet("kalshi", "KXA-1", "2026-09-01T00:00:00.000000Z")
    bet("kalshi", "KXB-1", "2026-09-01T00:00:00.000000Z")      # fails
    bet("kalshi", "KXF-1", "2026-09-01T00:00:00.000000Z", "future")
    bet("polymarket", "p1", "2026-09-01T00:00:00.000000Z")
    bet("polymarket", "p2", "2026-09-01T00:00:00.000000Z")      # same empty body as p1
    bet("polymarket", "p3", "2026-10-03T12:00:00.000000Z")      # not started
    calls = []
    def kget(path, params):
        calls.append(path)
        if "KXB" in path:
            return Resp(500)
        return Resp(404) if path.startswith("/series/") else Resp(200, {"candlesticks": []})
    def gget(path, params):
        calls.append(params["symbol"])
        return Resp(200, {"history": []})

    now = "2026-10-03T11:58:00Z"
    assert not archive.sync_closes(conn, kget, gget, now)
    assert calls == ["/series/KXA/markets/KXA-1/candlesticks", "/historical/markets/KXA-1/candlesticks",
                     "/series/KXB/markets/KXB-1/candlesticks", "p1", "p2"]
    calls.clear()
    archive.sync_closes(conn, kget, gget, now)
    assert calls == ["/series/KXB/markets/KXB-1/candlesticks"]   # only the failed one retried
    conn.execute("UPDATE bets SET start_time = '2026-09-02T00:00:00.000000Z' WHERE market_id = 'p1'")
    calls.clear()
    archive.sync_closes(conn, kget, gget, now)
    assert "p1" in calls                                         # postponed: new start refetched
```
(Add `from sharp_check import normalize` and give `Resp` default body.)

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement**

```python
def epoch(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def fetch_close(venue, market, start, kalshi_get, gateway_get):
    """1-minute prices for [start - CLOSE_WINDOW, start]. Kalshi markets past the cutoff 404 on the live tier."""
    lo = start - CLOSE_WINDOW
    if venue == "kalshi":
        p = {"start_ts": lo, "end_ts": start, "period_interval": 1, "include_latest_before_start": "true"}
        r = kalshi_get(f"/series/{market.split('-')[0]}/markets/{market}/candlesticks", p)
        if r.status_code == 404:
            r = kalshi_get(f"/historical/markets/{market}/candlesticks", p)
    else:  # the docs' bare startTimestamp/endTimestamp return 400
        r = gateway_get("/v1/price-history", {"symbol": market, "fidelity": 1,
                                              "timestamp.startTimestamp": lo, "timestamp.endTimestamp": start})
    r.raise_for_status()
    return r.json()


def sync_closes(conn, kalshi_get=clients.kalshi_get, gateway_get=clients.gateway_get, now=None):
    """Archive prices at game start for every CLV-eligible bet, once per (market, start). Needs a fresh `bets`.

    The body carries market and start_ts: raw_pages dedups on body alone, and price-history doesn't name its symbol.
    """
    cutoff = epoch(now or utc_now()) - 300  # let the last pre-start minute close
    done = set(conn.execute("SELECT venue, json_extract(body_json, '$.market'), json_extract(body_json, '$.start_ts')"
                            f" FROM raw_pages WHERE endpoint IN ({','.join('?' * len(CLOSE_ENDPOINTS))})",
                            tuple(CLOSE_ENDPOINTS.values())))
    todo = sorted({(v, m, epoch(s)) for v, m, s in conn.execute(
        f"SELECT venue, market_id, start_time FROM bets WHERE {CLV_ELIGIBLE}")} - done)
    ok, new = True, 0
    for venue, market, start in todo:
        if start > cutoff:
            continue
        try:
            body = fetch_close(venue, market, start, kalshi_get, gateway_get)
            new += store(conn, venue, CLOSE_ENDPOINTS[venue], {"market": market, "start_ts": start},
                         {**body, "market": market, "start_ts": start})
            conn.commit()
        except Exception as e:  # never print response headers
            ok = False
            print(f"{venue:10} close {market} FAILED: {type(e).__name__}: {str(e)[:200]}")
    print(f"{'both':10} {'closes':28} fetched={sum(s <= cutoff for _, _, s in todo)} new={new}")
    return ok
```

`__main__` after `sync_pm_markets(conn)`:
```python
    from sharp_check import normalize  # late: normalize imports archive
    normalize.rebuild(conn, normalize.MANUAL_CASH)  # closes need fresh bets (start times, eligibility)
    ok = sync_closes(conn) and ok
```

- [ ] **Step 4: Run** full suite → PASS. **Step 5: Commit** "Phase 4: archive prices at game start".

### Task 4: `metrics.clv` and dashboard section

**Files:** Modify `sharp_check/metrics.py`, `sharp_check/app.py`; Test `tests/test_metrics.py`.

**Interfaces:** `metrics.clv(conn, venues=VENUES) -> dict` with keys `eligible, with_close, clv_usd, pnl, mean_cents, ci, beat, missing`; `metrics.clv_bets(conn) -> list[tuple]`.

- [ ] **Step 1: Failing test**

```python
def test_clv_aggregates_bets_with_a_close_and_lists_missing():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    for n, (clv, status, pnl) in enumerate([(20_000, "settled", 600_000), (-10_000, "settled", -400_000),
                                            (None, "settled", 100_000), (30_000, "open", None)]):
        bet(conn, n, 0, status, 400_000, pnl, 400_000, 0)
        conn.execute("UPDATE bets SET link = 'game', is_live = 0, market_type = 'prop', start_time = 'S',"
                     " clv = ?, clv_usd = ? WHERE bet_id = ?", (clv, None if clv is None else clv * 10, f"b{n}"))
    c = metrics.clv(conn)
    assert (c["eligible"], c["with_close"], c["clv_usd"], c["pnl"]) == (4, 3, 400_000, 200_000)
    assert abs(c["mean_cents"] - 4/3) < 1e-9 and abs(c["beat"] - 2/3) < 1e-9
    assert c["ci"][0] < c["mean_cents"] < c["ci"][1]
    assert c["missing"] == [("b2", "S")]
    assert metrics.clv(conn, ("polymarket",))["mean_cents"] is None
```

- [ ] **Step 2: Run** → FAIL. **Step 3: Implement**

```python
def clv(conn, venues=VENUES):
    """Market CLV over eligible non-void bets. CLV $ = Σ qty × (close − entry); P&L is over the same bets, closed."""
    rows = conn.execute(f"""SELECT bet_id, start_time, status, clv, clv_usd, realized_pnl FROM bets
                            WHERE venue IN ({','.join('?' * len(venues))}) AND {archive.CLV_ELIGIBLE}
                            AND status != 'void' ORDER BY start_time""", venues).fetchall()
    have = [r for r in rows if r[3] is not None]
    cents = [r[3] / 10_000 for r in have]
    n = len(cents)
    mean = sum(cents) / n if n else None
    half = 1.96 * statistics.stdev(cents) / math.sqrt(n) if n > 1 else None
    return {
        "eligible": len(rows),
        "with_close": n,
        "clv_usd": sum(r[4] for r in have),
        "pnl": sum(r[5] for r in have if r[2] != "open"),
        "mean_cents": mean,
        "ci": None if half is None else (mean - half, mean + half),
        "beat": sum(c > 0 for c in cents) / n if n else None,
        "missing": [(r[0], r[1]) for r in rows if r[3] is None],
    }


def clv_bets(conn):
    return conn.execute("""SELECT b.start_time, COALESCE(m.title, b.market_id), b.market_type, b.outcome,
                                  b.avg_entry, b.close_mid, b.clv, b.clv_usd, b.realized_pnl
                           FROM bets b LEFT JOIN markets m USING (venue, market_id)
                           WHERE b.clv IS NOT NULL ORDER BY b.start_time DESC""").fetchall()
```
(`import statistics`, `from sharp_check import archive`.)

app.py: `cents(m)` helper, `clv_section(conn)` yielding a scope table, per-bet table, "No close" list and note; inserted before "Game linkage".

```python
def cents(micros):
    return f"{micros / 10_000:.1f}¢"


def clv_section(conn):
    rows = []
    for scope, venues in SCOPES.items():
        c = metrics.clv(conn, venues)
        ci = "" if c["ci"] is None else f" ({c['ci'][0]:+.1f}–{c['ci'][1]:+.1f})"
        rows.append((scope, f"{c['with_close']} / {c['eligible']}", usd(c["clv_usd"]), usd(c["pnl"]),
                     "" if c["mean_cents"] is None else f"{c['mean_cents']:+.1f}¢{ci}",
                     "" if c["beat"] is None else f"{c['beat']:.0%}"))
    yield table(("Venue", "With close / eligible", "CLV $", "P&L (same bets)", "Mean CLV (95% CI)", "Beat close"), rows)
    yield html.P("Close = mid at game start on the same venue. Live bets, parlays, futures, other markets and voids"
                 " have no CLV. P&L counts closed bets only.")
    yield table(("Start (UTC)", "Market", "Type", "Side", "Entry", "Close", "CLV", "CLV $", "P&L"),
                [(s[:16], t, mt, o, cents(e), cents(c), f"{v / 10_000:+.1f}¢", usd(u), "" if p is None else usd(p))
                 for s, t, mt, o, e, c, v, u, p in metrics.clv_bets(conn)])
    missing = metrics.clv(conn)["missing"]
    if missing:
        yield html.H3("No close")
        yield table(("Bet", "Start (UTC)"), [(b, s[:16]) for b, s in missing])
```

- [ ] **Step 4: Run** full suite → PASS. **Step 5: Commit** "Phase 4: CLV metrics and dashboard section".

### Task 5: Real run, exit check, docs

- [ ] Run `python -m sharp_check.archive && python -m sharp_check.normalize && python -m sharp_check.reconcile`; expect 44 closes fetched, reconcile OK.
- [ ] Query `SELECT venue, market_type, COUNT(clv), COUNT(*) FROM bets WHERE <eligible> AND status != 'void' GROUP BY 1, 2`: every type has CLV; investigate any missing.
- [ ] Load dashboard `http://127.0.0.1:8050`, confirm CLV section renders.
- [ ] Update docs listed in the spec (`data-model.md`, `kalshi.md`, `polymarket-us.md`, `pnl-rules.md`, `roadmap.md` current phase → 5 if exit check passes) and commit "Phase 4 done".
