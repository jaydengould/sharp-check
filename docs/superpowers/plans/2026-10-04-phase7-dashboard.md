# Phase 7 Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A five-tab dashboard (Overview, CLV, Performance, Habits, Data health) with readable bet labels, five themes and a phone layout, plus wins-vs-expected that judges cash-outs by how their market finished, and a cash-out hindsight metric.

**Architecture:** The archive looks up every Polymarket single and parlay leg until it resolves. Normalize adds `label_yes`, `label_no` and `yes_value` to `markets` (PM parlay results derived from legs). `metrics` gains `labels`, `recent_bets`, `open_bets`, and `headline` counts cash-outs with a known result. `app.py` regroups the existing sections into `dcc.Tabs`; `assets/style.css` + `assets/theme.js` do the look; one clientside callback themes the charts.

**Tech Stack:** Python 3.12, SQLite, Dash 4.4.1 / Plotly, pytest.

**Spec:** `docs/superpowers/specs/2026-10-04-phase7-dashboard-design.md`

## Global Constraints

- Read-only APIs (GET only). Polymarket US gateway only.
- Money as integer micro-dollars (`ONE = 1_000_000`); timestamps UTC.
- `raw_pages` stays the source of truth; every new column is rebuilt from it.
- Market results for cash-outs live on `markets.yes_value`, **never** in `settlements` (settlements drive cash reconcile; a resolved market can precede the venue's payout).
- No new dependencies. Colours only, no logos.
- Gain/loss green/red never use the theme accent.

## Decision added after spec approval (2026-10-04, user-approved)

Wins vs expected now counts every closed, non-void bet whose market has a 0/1 result: settled bets by their settlement, cash-outs by how the market finished (would it have won if held). Voids and cash-outs whose market hasn't resolved are excluded. Why: the user cashes out mostly winning positions (18 of 25 green; 12 of 23 resolved cash-outs would have won), so settled-only wins were biased low (18 vs 29.1 expected, below the CI). With final results: 30 vs 36.8 (CI 27.4–46.2). Fractional credit at the exit price was rejected: exits are at the bid, so it is biased low and assumes the market is fair.

## Review Focus

- A Polymarket market that never resolves (cancelled, postponed) must stop being re-fetched: give up 14 days after its start.
- A parlay with one lost leg and other legs pending is a known loss; a void (non-0/1) leg with no lost leg is unknown, not a win.
- A two-team Polymarket market: the NO side of "Tigers vs Rebels" is the Rebels, not "No: Tigers".
- Segment headline filters must still work after `headline` joins `markets` (ambiguous `market_type`/`sport` columns).
- An empty database (fresh clone) must render every tab without exceptions.

---

### Task 1: Polymarket lookups for parlay legs, re-fetched until resolved

**Files:**
- Modify: `sharp_check/archive.py` (`sync_pm_markets`)
- Test: `tests/test_archive.py` (`test_pm_markets_archived_for_singles_until_game_starts` → replaced)

**Interfaces:**
- Produces: `/v1/market/slug/{slug}` pages for every PM single and every `comboLegDetails[].slug`, latest page per slug = resolved state when available.

- [ ] **Step 1: Replace the test**

```python
def test_pm_markets_archived_for_singles_and_legs_until_resolved():
    conn = archive.connect(":memory:")
    single = {"marketSlug": "aec-nfl-gb-min-2026-09-13", "comboLegDetails": []}
    parlay = {"marketSlug": "caoc-1", "comboLegDetails": [{"slug": "aec-nfl-x"}]}
    archive.store(conn, "polymarket", "/v1/portfolio/activities", {}, {"activities": [{"trade": single}, {"trade": parlay}]})
    calls, status = [], {"s": "MARKET_STATUS_OPEN"}

    def get(path):
        calls.append(path)
        slug = path.rsplit("/", 1)[1]
        return Resp(200, {"market": {"slug": slug, "gameStartTime": "2026-09-14T00:20:00Z", "status": status["s"]}})

    archive.sync_pm_markets(conn, get, now="2026-09-13T12:00:00Z")
    archive.sync_pm_markets(conn, get, now="2026-09-15T00:00:00Z")  # started, unresolved: fetched again
    status["s"] = "MARKET_STATUS_RESOLVED"
    archive.sync_pm_markets(conn, get, now="2026-09-16T00:00:00Z")  # stored resolved now
    archive.sync_pm_markets(conn, get, now="2026-09-17T00:00:00Z")  # resolved: skipped
    both = ["/v1/market/slug/aec-nfl-gb-min-2026-09-13", "/v1/market/slug/aec-nfl-x"]
    assert sorted(calls) == sorted(both * 3)


def test_pm_markets_give_up_14_days_after_start_and_skip_404s():
    conn = archive.connect(":memory:")
    archive.store(conn, "polymarket", "/v1/portfolio/activities", {}, {"activities": [
        {"trade": {"marketSlug": "caoc-1", "comboLegDetails": [{"slug": "gone"}, {"slug": "stuck"}]}}]})
    archive.store(conn, "polymarket", "/v1/market/slug/{slug}", {"slug": "stuck"},
                  {"market": {"slug": "stuck", "gameStartTime": "2026-09-01T00:00:00Z", "status": "MARKET_STATUS_OPEN"}})
    calls = []

    def get(path):
        calls.append(path)
        return Resp(404)

    archive.sync_pm_markets(conn, get, now="2026-09-20T00:00:00Z")
    assert calls == ["/v1/market/slug/gone"]  # stuck is > 14 days past start; gone 404s and isn't stored
```

- [ ] **Step 2: Run, expect FAIL** — `python -m pytest tests/test_archive.py -k pm_markets -q`

- [ ] **Step 3: Implement**

```python
GIVE_UP_S = 14 * 86_400  # a market not resolved two weeks after its start (cancelled, postponed) stops being re-fetched


def sync_pm_markets(conn, get=clients.gateway_get, now=None):
    """Archive the public market record of every traded single and parlay leg: type, line, start, sides, result.

    Re-fetched until resolved (or two weeks past its start), so cash-outs get their market's final result.
    Combo (parlay) slugs 404, so a parlay's result comes from its legs. A 404 leg isn't stored and is retried.
    """
    now = now or utc_now()
    trades = [a["trade"] for a in records(conn, "polymarket", ("/v1/portfolio/activities",), "activities") if a.get("trade")]
    slugs = {t["marketSlug"] for t in trades if not t.get("comboLegDetails")}
    slugs |= {l["slug"] for t in trades for l in t.get("comboLegDetails") or []}
    stored = {m["slug"]: m for m in records(conn, "polymarket", ("/v1/market/slug/{slug}",), "market")}

    def done(slug):
        m = stored.get(slug)
        if m is None:
            return False
        start = m.get("gameStartTime")
        return m.get("status") == "MARKET_STATUS_RESOLVED" or bool(start) and epoch(now) - epoch(start) > GIVE_UP_S

    todo = sorted(s for s in slugs if not done(s))
    new = missing = 0
    for slug in todo:
        r = get(f"/v1/market/slug/{slug}")
        if r.status_code == 404:
            missing += 1
            continue
        r.raise_for_status()
        new += store(conn, "polymarket", "/v1/market/slug/{slug}", {"slug": slug}, r.json())
    conn.commit()
    print(f"{'polymarket':10} {'/v1/market/slug/{slug}':28} fetched={len(todo)} new={new} missing={missing}")
```

`Resp(404)` in the test helper must expose `status_code`; it already does.

- [ ] **Step 4: Run, expect PASS**; then full suite `python -m pytest tests/ -q`.
- [ ] **Step 5: Live backfill** — `python -m sharp_check.archive`. Note fetched/new/missing counts; if `missing` is large, report it.
- [ ] **Step 6: Commit** — `Archive: Polymarket leg lookups, re-fetched until resolved`.

### Task 2: Market labels and final results in `markets`

**Files:**
- Modify: `sharp_check/normalize.py` (schema, `kalshi_games_markets`, `pm_games_markets`, new `pm_labels`, `pm_yes_value`, `parlay_results`, `rebuild`)
- Test: `tests/test_normalize.py`

**Interfaces:**
- Produces: `markets` columns appended: `label_yes TEXT, label_no TEXT, yes_value INTEGER` (row tuples grow from 9 to 12 values).
  - `label_yes`: short text for the YES side; `label_no`: NO side when the market names it (two-team, over/under, spread), else NULL.
  - `yes_value`: final YES value in micro-dollars from market data (Kalshi finalized `settlement_value_dollars`; PM `outcomePrices[0]` once `MARKET_STATUS_RESOLVED`; PM parlay from legs), NULL until known.

- [ ] **Step 1: Tests**

```python
def pm_market_page(slug, smt, sides, title=None, line=None, status="MARKET_STATUS_OPEN", prices=None, start="2026-09-14T00:20:00Z"):
    m = {"slug": slug, "sportsMarketType": smt, "title": title, "question": f"Q {slug}?", "line": line,
         "gameStartTime": start, "status": status, "marketSides": sides}
    if prices:
        m["outcomePrices"] = json.dumps(prices)
    return {"market": m}


def test_pm_labels_name_both_sides_and_results_come_from_resolved_markets():
    side = lambda d, team=None: {"description": d, "team": team and {"name": team}}
    assert normalize.pm_labels(pm_market_page("a", "football_team_full_game_winner",
                                              [side("Tigers", "LSU"), side("Rebels", "Ole Miss")])["market"]) == ("LSU", "Ole Miss")
    assert normalize.pm_labels(pm_market_page("b", "football_team_full_game_spread",
                                              [side("-4.50", "Detroit Lions"), side("+4.50", "Carolina Panthers")])["market"]) \
        == ("Detroit Lions -4.5", "Carolina Panthers +4.5")
    assert normalize.pm_labels(pm_market_page("c", "baseball_team_full_game_total", [side("Over"), side("Under")],
                                              line=8.5)["market"]) == ("Over 8.5", "Under 8.5")
    assert normalize.pm_labels(pm_market_page("d", "football_player_touchdowns", [side("Yes"), side("No")],
                                              title="Kyle Monangai 1+ touchdowns")["market"]) == ("Kyle Monangai 1+ touchdowns", None)
    assert normalize.pm_labels(pm_market_page("e", "soccer_team_full_time_winner", [side("Yes", "France"), side("No", "France")])["market"]) \
        == ("France", None)
    assert normalize.pm_yes_value(pm_market_page("f", "x", [], status="MARKET_STATUS_RESOLVED", prices=["0", "1"])["market"]) == 0
    assert normalize.pm_yes_value(pm_market_page("g", "x", [], prices=["0.4", "0.6"])["market"]) is None  # not resolved


def test_pm_parlay_result_from_legs_and_leg_rows_use_lookups():
    conn = archive.connect(":memory:")
    leg = lambda slug, side: {"slug": slug, "outcomeSide": side, "eventSlug": f"ev-{slug}", "title": f"Game {slug}",
                              "eventStartTime": "2026-09-14T00:20:00Z"}
    t = load("pm_trades")[0]
    t["id"], t["marketSlug"] = "p1", "caoc-lost"
    t["comboLegDetails"] = [leg("aec-nfl-a", "OUTCOME_SIDE_YES"), leg("aec-nfl-b", "OUTCOME_SIDE_NO")]
    t2 = copy.deepcopy(t)
    t2["id"], t2["marketSlug"] = "p2", "caoc-pending"
    t2["comboLegDetails"] = [leg("aec-nfl-a", "OUTCOME_SIDE_YES"), leg("aec-nfl-c", "OUTCOME_SIDE_YES")]
    archive.store(conn, "polymarket", "/v1/portfolio/activities", {}, pm_page(t, t2))
    yn = [{"description": "Yes"}, {"description": "No"}]
    for slug, prices in (("aec-nfl-a", ["1", "0"]), ("aec-nfl-b", ["1", "0"])):  # a won (yes); b's NO side lost
        archive.store(conn, "polymarket", "/v1/market/slug/{slug}", {"slug": slug},
                      pm_market_page(slug, "football_player_touchdowns", yn, title=f"T {slug}",
                                     status="MARKET_STATUS_RESOLVED", prices=prices))
    normalize.rebuild(conn)
    got = dict(conn.execute("SELECT market_id, yes_value FROM markets WHERE venue = 'polymarket'"))
    assert got["caoc-lost"] == 0              # one lost leg settles it
    assert got["caoc-pending"] is None        # leg c has no lookup yet
    assert conn.execute("SELECT label_yes, market_type FROM markets WHERE market_id = 'aec-nfl-a'").fetchone() == ("T aec-nfl-a", "prop")
    assert conn.execute("SELECT label_yes, market_type FROM markets WHERE market_id = 'aec-nfl-c'").fetchone() == (None, None)


def test_kalshi_markets_carry_yes_label_and_final_value():
    conn = archive.connect(":memory:")
    archive.store(conn, "kalshi", "/markets/{ticker}", {}, {"market": {
        "ticker": "KXNFLGAME-26SEP24ATLGB-GB", "event_ticker": "KXNFLGAME-26SEP24ATLGB", "title": "Atlanta at Green Bay Winner?",
        "yes_sub_title": "Green Bay", "status": "finalized", "settlement_value_dollars": "1.0000",
        "settlement_ts": "2026-09-25T03:00:00Z", "floor_strike": None, "mve_selected_legs": None}})
    normalize.rebuild(conn)
    assert conn.execute("SELECT label_yes, label_no, yes_value FROM markets").fetchone() == ("Green Bay", None, 1_000_000)
```

`test_normalize.py` imports `json` (add it) and already has `copy`, `load`, `pm_page`.

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**

Schema: append to `markets` before the primary key:

```sql
    label_yes TEXT,             -- short text for the YES side
    label_no TEXT,              -- NO side when the market names it (two teams, over/under, spread); else NULL
    yes_value INTEGER,          -- final YES value from market data (micro-dollars), NULL until resolved. Not cash: see settlements
```

and change the `market_type` comment to `-- ...; NULL for PM parlay legs without a market lookup`.

Helpers:

```python
def pm_side_label(side, line):
    d, team = side.get("description") or "", (side.get("team") or {}).get("name")
    if d[:1] in "+-" and team:
        return f"{team} {float(d):+g}"
    if d in ("Over", "Under"):
        return f"{d} {line:g}" if line is not None else d
    return team or d


def pm_labels(m):
    """(YES label, NO label or None). Two named sides (teams, over/under, spread) label both; Yes/No markets one."""
    sides = m.get("marketSides") or []
    if len(sides) == 2 and [s.get("description") for s in sides] != ["Yes", "No"]:
        return pm_side_label(sides[0], m.get("line")), pm_side_label(sides[1], m.get("line"))
    team = sides and (sides[0].get("team") or {}).get("name")
    return m.get("title") or team or m.get("question"), None


def pm_yes_value(m):
    if m.get("status") != "MARKET_STATUS_RESOLVED":
        return None
    prices = m["outcomePrices"]
    return micros(json.loads(prices)[0] if isinstance(prices, str) else prices[0])  # long (YES) side first
```

`kalshi_games_markets`: rows gain `(m.get("yes_sub_title") or m.get("title"), None, yes)` where `yes = micros(m["settlement_value_dollars"]) if m["status"] == "finalized" else None`; leg rows gain `(None, None, None)`:

```python
    rows = [(m["ticker"], m.get("event_ticker"), m.get("title"), m.get("floor_strike"),
             m.get("yes_sub_title") or m.get("title"),
             micros(m["settlement_value_dollars"]) if m["status"] == "finalized" else None) for m in markets]
    rows += [(l["market_ticker"], l["event_ticker"], None, None, None, None)
             for m in markets for l in m.get("mve_selected_legs") or []]
    for ticker, event, title, line, label, yes in rows:
        league, sport, mtype = classify.kalshi_market(ticker)
        ms = archive.kalshi_milestone(by_event, event) if mtype not in ("future", "parlay") else None
        yield "market", ("kalshi", ticker, event, title, sport, league, mtype, line, ms and f"kalshi:{ms['id']}",
                         label, None, yes)
```

(Kalshi parlay `title` is "yes A,no B"; `metrics.labels` splits it.)

`pm_games_markets`: parlay row gains `(None, None, None)`; leg rows use the lookup when there is one:

```python
            for l in legs:
                m = stored.get(l["slug"])
                game = l.get("eventStartTime") and f"polymarket:{l['eventSlug']}"
                if game:
                    yield "game", (game, "polymarket", *classify.pm_league(l["slug"])[:1], l["eventSlug"],
                                   iso_us(l["eventStartTime"]))
                if m:
                    league, sport, mtype = classify.pm_market(l["slug"], m.get("sportsMarketType"),
                                                              m.get("gameStartTime") or l.get("eventStartTime"))
                    yield "market", ("polymarket", l["slug"], l.get("eventSlug"), m.get("question") or l.get("title"),
                                     sport, league, mtype, m.get("line"), game or None, *pm_labels(m), pm_yes_value(m))
                else:
                    league, sport = classify.pm_league(l["slug"])
                    yield "market", ("polymarket", l["slug"], l.get("eventSlug"), l.get("title"), sport, league, None,
                                     None, game or None, None, None, None)
```

(Keep the existing `game` yield exactly as it is today; only the `market` yield changes.) The single row gains `*pm_labels(m) if m else (None, None)` and `pm_yes_value(m) if m else None`.

PM parlay results, run in `rebuild` right after markets are inserted (before `link_bets`):

```python
def parlay_results(conn):
    """PM parlay yes_value from its legs: 0 once any leg lost, ONE once all won, else unknown (pending or void leg).
    Kalshi parlay markets settle themselves (finalized market record)."""
    yes = {m: y for m, y in conn.execute("SELECT market_id, yes_value FROM markets WHERE venue = 'polymarket'")}
    legs = {}
    for m, leg, side in conn.execute("SELECT market_id, leg_market_id, leg_outcome FROM parlay_legs WHERE venue = 'polymarket'"):
        y = yes.get(leg)
        legs.setdefault(m, []).append(None if y not in (0, ONE) else (y == ONE) == (side == "yes"))
    for m, won in legs.items():
        value = 0 if False in won else ONE if all(won) else None
        conn.execute("UPDATE markets SET yes_value = ? WHERE venue = 'polymarket' AND market_id = ?", (value, m))
```

- [ ] **Step 4: Run, expect PASS**; full suite.
- [ ] **Step 5: Before/after numbers on real data.** Before the rebuild, save `metrics.headline`, `metrics.clv`, `metrics.parlay_clv` (Both) to the scratchpad; rebuild; compare. Only a futures leg changing a parlay's linkage may move them; explain any difference. Print every distinct `label_yes`/`label_no` by venue and type and eyeball them; fix rules (not the UI) if one reads badly.
- [ ] **Step 6: Commit** — `Markets: labels and final results; PM legs use their lookups`.

### Task 3: Wins judge cash-outs by the market's result; labels, recent and open bets

**Files:**
- Modify: `sharp_check/metrics.py` (`headline`, new `labels`, `recent_bets`, `open_bets`)
- Modify: `docs/pnl-rules.md` (Win rate rule)
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `markets.label_yes`, `label_no`, `yes_value` (Task 2).
- Produces:
  - `headline(...)["settled"]` now = bets judged (settled 0/1 + cash-outs with a 0/1 market result); `wins`, `expected`, `ci` over those.
  - `labels(conn) -> {bet_id: (label: str, legs: list[str])}`
  - `recent_bets(conn, n=10) -> list[dict]` keys: `bet_id, venue, closed_ts, label, legs, result, is_live, stake, clv_net, pnl` (newest first; closed only).
  - `open_bets(conn) -> list[dict]` keys: `bet_id, venue, opened_ts, label, legs, start_time, stake, avg_entry` (newest first).
  - `result` strings: `won`, `lost`, `void`, `cashed out`, `cashed out · would have won`, `cashed out · would have lost`.

- [ ] **Step 1: Tests** (extend the `bet` helper with keyword overrides)

```python
def bet(conn, n, is_parlay, status, stake, pnl, avg_entry, payout, venue="kalshi", market="M", outcome="yes", closed=None):
    conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, entry_qty, avg_entry,
                    stake, exit_qty, exit_proceeds, exit_fees, payout, realized_pnl, status, closed_ts)
                    VALUES (?, ?, ?, ?, ?, 't', 1, ?, ?, 0, 0, 0, ?, ?, ?, ?)""",
                 (f"b{n}", venue, market, outcome, is_parlay, avg_entry, stake, payout, pnl, status, closed))


def market(conn, mid, label_yes=None, label_no=None, yes_value=None, title=None, venue="kalshi"):
    conn.execute("INSERT INTO markets (venue, market_id, title, label_yes, label_no, yes_value) VALUES (?, ?, ?, ?, ?, ?)",
                 (venue, mid, title, label_yes, label_no, yes_value))


def test_wins_judge_cash_outs_by_how_their_market_finished():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    market(conn, "W", yes_value=1_000_000)
    market(conn, "L", yes_value=0)
    market(conn, "P")                                                    # unresolved
    bet(conn, 1, 0, "closed_early", 300_000, 200_000, 300_000, 0, market="W")              # would have won
    bet(conn, 2, 0, "closed_early", 400_000, 100_000, 400_000, 0, market="L")              # green, would have lost
    bet(conn, 3, 0, "closed_early", 500_000, -100_000, 500_000, 0, market="L", outcome="no")  # NO side: would have won
    bet(conn, 4, 0, "closed_early", 200_000, -50_000, 200_000, 0, market="P")              # unknown: excluded
    bet(conn, 5, 0, "settled", 600_000, -600_000, 600_000, 0, market="L")                  # held, lost
    h = metrics.headline(conn)
    assert (h["settled"], h["wins"]) == (4, 2)
    assert abs(h["expected"] - 1.8) < 1e-9
    assert h["bets"] == 5 and h["pnl"] == -450_000                     # P&L still covers every closed bet


def test_headline_segment_filter_still_works_with_the_markets_join():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    market(conn, "M")
    conn.execute("UPDATE markets SET market_type = 'moneyline'")
    bet(conn, 1, 0, "settled", 100_000, -100_000, 500_000, 0)
    conn.execute("UPDATE bets SET market_type = 'prop'")
    assert metrics.headline(conn, where=("market_type", "prop"))["bets"] == 1


def test_labels_for_singles_and_parlays_on_both_venues():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    market(conn, "GAME", label_yes="LSU", label_no="Ole Miss", venue="polymarket")
    market(conn, "TD", label_yes="Kyle Monangai 1+ touchdowns", venue="polymarket")
    market(conn, "caoc-1", venue="polymarket")
    market(conn, "leg-nolookup", title="NY Jets vs. CHI Bears", venue="polymarket")
    market(conn, "KXMVE-1", label_yes="yes Joey Cantillo: 8+,no Purdue wins by over 10.5 Points")
    conn.executemany("INSERT INTO parlay_legs VALUES (?, ?, ?, ?)", [
        ("polymarket", "caoc-1", "TD", "no"), ("polymarket", "caoc-1", "leg-nolookup", "yes"),
        ("polymarket", "caoc-1", "GAME", "no"),
        ("kalshi", "KXMVE-1", "A", "yes"), ("kalshi", "KXMVE-1", "B", "no")])
    bet(conn, 1, 0, "settled", 1, 0, 1, 0, venue="polymarket", market="GAME", outcome="no")
    bet(conn, 2, 0, "settled", 1, 0, 1, 0, venue="polymarket", market="TD", outcome="no")
    bet(conn, 3, 1, "settled", 1, 0, 1, 0, venue="polymarket", market="caoc-1")
    bet(conn, 4, 1, "settled", 1, 0, 1, 0, market="KXMVE-1")
    got = metrics.labels(conn)
    assert got["b1"] == ("Ole Miss", [])
    assert got["b2"] == ("No: Kyle Monangai 1+ touchdowns", [])
    assert got["b3"] == ("3-leg parlay", ["No: Kyle Monangai 1+ touchdowns", "NY Jets vs. CHI Bears", "Ole Miss"])
    assert got["b4"] == ("2-leg parlay", ["Joey Cantillo: 8+", "No: Purdue wins by over 10.5 Points"])


def test_recent_and_open_bets():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    market(conn, "W", label_yes="A", yes_value=1_000_000)
    for i in range(12):
        bet(conn, i, 0, "settled", 100_000, 900_000 if i % 2 else -100_000, 100_000, 1_000_000 if i % 2 else 0,
            market="W", closed=f"2026-09-{10 + i:02d}T00:00:00.000000Z")
    bet(conn, 20, 0, "closed_early", 100_000, 50_000, 100_000, 0, market="W", closed="2026-09-30T00:00:00.000000Z")
    bet(conn, 21, 0, "open", 100_000, None, 100_000, 0, market="W")
    recent = metrics.recent_bets(conn)
    assert len(recent) == 10 and recent[0]["bet_id"] == "b20"
    assert recent[0]["result"] == "cashed out · would have won" and recent[1]["result"] == "won"
    assert [b["bet_id"] for b in metrics.open_bets(conn)] == ["b21"]
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**

`headline`: join markets and judge cash-outs.

```python
    if where:
        assert where[0] in SEGMENTS
        extra, args = f" AND b.{where[0]} = ?", (where[1],)
    rows = conn.execute(f"""
        SELECT b.status, b.stake, b.realized_pnl, b.avg_entry, b.payout, b.outcome, m.yes_value
        FROM bets b LEFT JOIN markets m USING (venue, market_id)
        WHERE b.venue IN ({','.join('?' * len(venues))}) AND b.is_parlay IN ({','.join('?' * len(SPLITS[split]))}){extra}
        """, (*venues, *SPLITS[split], *args)).fetchall()
    closed = [r for r in rows if r[0] != "open"]
    # Judged = held to a 0/1 settlement, or cashed out with a 0/1 market result (would it have won if held).
    # Settled-only would be biased: cash-outs are mostly winning positions. Rules: docs/pnl-rules.md.
    held = [(r[3], r[4] > 0) for r in rows if r[0] == "settled"]
    held += [(r[3], (r[6] == ONE) == (r[5] == "yes")) for r in rows if r[0] == "closed_early" and r[6] in (0, ONE)]
    ...
    probs = [p / ONE for p, _ in held]
    ... "settled": len(held), "wins": sum(w for _, w in held), ...
```

Labels, recent, open:

```python
def labels(conn):
    """{bet_id: (label, legs)}: readable market text, never IDs unless nothing else exists. NO sides read "No: ..."
    unless the market names its NO side (a team, Under, the other spread)."""
    text = {(v, m): (ly, ln, t) for v, m, ly, ln, t in
            conn.execute("SELECT venue, market_id, label_yes, label_no, title FROM markets")}
    legs = {}
    for v, m, leg, side in conn.execute("SELECT venue, market_id, leg_market_id, leg_outcome FROM parlay_legs ORDER BY rowid"):
        legs.setdefault((v, m), []).append((leg, side))

    def side_label(v, m, side):
        ly, ln, title = text.get((v, m), (None, None, None))
        if side == "no" and ln:
            return ln
        t = ly or title or m
        return f"No: {t}" if side == "no" else t

    out = {}
    for bet_id, v, m, outcome, is_parlay in conn.execute("SELECT bet_id, venue, market_id, outcome, is_parlay FROM bets"):
        if not is_parlay:
            out[bet_id] = (side_label(v, m, outcome), [])
            continue
        kalshi_title = v == "kalshi" and text.get((v, m), (None,))[0]
        if kalshi_title:  # "yes A,no B": the parlay title lists its legs
            ls = [p[4:] if p.startswith("yes ") else f"No: {p[3:]}" if p.startswith("no ") else p
                  for p in (s.strip() for s in kalshi_title.split(","))]
        else:
            ls = [side_label(v, leg, side) for leg, side in legs.get((v, m), [])]
        name = f"{len(ls)}-leg parlay" if ls else "Parlay"
        out[bet_id] = (f"No: {name}" if outcome == "no" else name, ls)
    return out


def bet_result(status, outcome, payout, yes_value):
    if status == "settled":
        return "won" if payout > 0 else "lost"
    if status == "void":
        return "void"
    if yes_value in (0, ONE):
        return f"cashed out · would have {'won' if (yes_value == ONE) == (outcome == 'yes') else 'lost'}"
    return "cashed out"


def recent_bets(conn, n=10):
    names = labels(conn)
    rows = conn.execute("""SELECT b.bet_id, b.venue, b.closed_ts, b.status, b.outcome, b.payout, m.yes_value, b.is_live,
                                  b.stake, b.clv_net, b.realized_pnl
                           FROM bets b LEFT JOIN markets m USING (venue, market_id)
                           WHERE b.status != 'open' ORDER BY b.closed_ts DESC LIMIT ?""", (n,)).fetchall()
    return [{"bet_id": r[0], "venue": r[1], "closed_ts": r[2], "label": names[r[0]][0], "legs": names[r[0]][1],
             "result": bet_result(r[3], r[4], r[5], r[6]), "is_live": r[7], "stake": r[8], "clv_net": r[9], "pnl": r[10]}
            for r in rows]


def open_bets(conn):
    names = labels(conn)
    rows = conn.execute("""SELECT bet_id, venue, opened_ts, start_time, stake, avg_entry FROM bets
                           WHERE status = 'open' ORDER BY opened_ts DESC""").fetchall()
    return [{"bet_id": r[0], "venue": r[1], "opened_ts": r[2], "label": names[r[0]][0], "legs": names[r[0]][1],
             "start_time": r[3], "stake": r[4], "avg_entry": r[5]} for r in rows]
```

`clv_bets` and `parlay_bets` keep their row shapes; the app swaps IDs for `labels(conn)[bet_id]`. `clv_bets` must therefore also return `bet_id` (prepend it to its SELECT; update its one caller in `app.py`).

`docs/pnl-rules.md` "Win rate" bullet becomes: *Win rate is always shown against expected wins (the sum of entry prices), with a confidence interval, never on its own. A bet counts when its outcome is 0/1: held to settlement (a win = payout > 0), or cashed out early and judged by how its market finished (would it have won if held). Voids and cash-outs whose market hasn't resolved (or never resolves) don't count. Settled-only would be biased low: cash-outs are mostly winning positions (2026-10-04: 12 of 23 resolved cash-outs would have won).*

- [ ] **Step 4: Run, expect PASS**; full suite; `python -m sharp_check.normalize` then print `metrics.headline(conn)` (expect wins ≈ 30, expected ≈ 36.8 if all lookups landed).
- [ ] **Step 5: Commit** — `Wins vs expected judges cash-outs by market result; bet labels, recent and open bets`.

### Task 4: Cash-out hindsight metric

User-approved 2026-10-04 (option A; B = spread cost at exit, later with entry spread). Question answered: has cashing out made or cost money versus holding, so far.

**Files:**
- Modify: `sharp_check/habits.py` (new `cash_outs`)
- Modify: `docs/pnl-rules.md` (Habits section: cash-out rule)
- Test: `tests/test_habits.py`

**Interfaces:**
- Consumes: `bets.exit_qty/exit_proceeds/exit_fees/status`, `markets.yes_value` (Task 2), `metrics.labels`, `metrics.mean_ci`.
- Produces: `habits.cash_outs(conn) -> {"rows": [...], "summary": {...}}`
  - row dict: `bet_id, closed_ts, label, is_parlay, partial (bool), exit_qty, avg_entry, exit_price, got, held_value, value, outcome` where `got = exit_proceeds - exit_fees`, `held_value = exit_qty × final value of your side` (None when unknown), `value = got - held_value` (positive = cashing out helped), `outcome` in `would have won | would have lost | pending`.
  - summary per split `all | singles | parlays`: `n` (cash-outs), `known` (with a result), `green` (full cash-outs with P&L > 0), `would_win`, `value` (sum over known), `mean`, `ci` (95%, per cash-out), `fees` (exit fees, all cash-outs).

Rules: a cash-out is any bet with `exit_qty > 0` (full: `closed_early`; partial: sold some, held the rest). Final value of your side = `yes_value` for YES, `ONE − yes_value` for NO, only when `yes_value` is 0 or ONE (a void market or one not yet resolved is `pending` and stays out of value). Hindsight is noisy at this n; the dashboard always shows the CI next to the total.

- [ ] **Step 1: Test**

```python
def test_cash_outs_compare_what_you_got_with_holding():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    conn.executemany("INSERT INTO markets (venue, market_id, yes_value) VALUES ('kalshi', ?, ?)",
                     [("W", 1_000_000), ("L", 0), ("P", None)])
    rows = [  # bet_id, market, outcome, status, entry_qty, exit_qty, exit_proceeds, exit_fees, pnl
        ("c1", "W", "yes", "closed_early", 10, 10, 6_000_000, 100_000, 1_900_000),   # sold 10 for 5.9 net; holding paid 10
        ("c2", "L", "yes", "closed_early", 10, 10, 3_000_000, 100_000, -1_100_000),  # holding paid 0: helped 2.9
        ("c3", "L", "no", "settled", 10, 4, 2_000_000, 0, 5_000_000),               # partial; NO side won: held 4
        ("c4", "P", "yes", "closed_early", 10, 10, 1_000_000, 0, 500_000),           # pending
        ("c5", "W", "yes", "settled", 10, 0, 0, 0, 5_000_000),                       # never sold: not a cash-out
    ]
    for b, m, o, s, eq, xq, xp, xf, pnl in rows:
        conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, entry_qty, avg_entry,
                        stake, exit_qty, exit_proceeds, exit_fees, payout, realized_pnl, status, closed_ts)
                        VALUES (?, 'kalshi', ?, ?, 0, 't', ?, 500_000, 5_000_000, ?, ?, ?, 0, ?, ?, ?)""",
                     (b, m, o, eq, xq, xp, xf, pnl, s, f"2026-09-0{b[1]}T00:00:00.000000Z"))
    got = {r["bet_id"]: r for r in habits.cash_outs(conn)["rows"]}
    assert set(got) == {"c1", "c2", "c3", "c4"}
    assert got["c1"]["value"] == 5_900_000 - 10_000_000 and got["c1"]["outcome"] == "would have won"
    assert got["c2"]["value"] == 2_900_000 and got["c3"]["partial"] and got["c3"]["value"] == 2_000_000 - 4_000_000
    assert got["c4"]["value"] is None and got["c4"]["outcome"] == "pending"
    s = habits.cash_outs(conn)["summary"]["all"]
    assert (s["n"], s["known"], s["green"], s["would_win"]) == (4, 3, 2, 2)
    assert s["value"] == -4_100_000 + 2_900_000 - 2_000_000 and s["fees"] == 200_000
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement**

```python
def cash_outs(conn):
    """Every bet with an exit (full or partial): what you got vs what holding the sold part would have paid.

    value = (exit proceeds − exit fees) − exit_qty × final value of your side; positive = cashing out helped.
    Unknown (void or unresolved market) rows are listed as pending and left out of the totals. Rules: docs/pnl-rules.md.
    """
    names = metrics.labels(conn)
    rows = []
    for r in conn.execute("""SELECT b.bet_id, b.closed_ts, b.is_parlay, b.status, b.outcome, b.exit_qty, b.avg_entry,
                                    b.exit_proceeds, b.exit_fees, b.realized_pnl, m.yes_value
                             FROM bets b LEFT JOIN markets m USING (venue, market_id)
                             WHERE b.exit_qty > 0 ORDER BY b.closed_ts DESC""").fetchall():
        bet_id, closed, is_parlay, status, outcome, qty, entry, proceeds, fees, pnl, yes = r
        side = None if yes not in (0, ONE) else yes if outcome == "yes" else ONE - yes
        got = proceeds - fees
        held = None if side is None else round(qty * side)
        rows.append({"bet_id": bet_id, "closed_ts": closed, "label": names[bet_id][0], "is_parlay": is_parlay,
                     "partial": status != "closed_early", "exit_qty": qty, "avg_entry": entry,
                     "exit_price": round(proceeds / qty), "got": got, "held_value": held,
                     "value": None if held is None else got - held, "fees": fees,
                     "green": status == "closed_early" and pnl > 0,
                     "outcome": "pending" if side is None else f"would have {'won' if side == ONE else 'lost'}"})

    def summary(rs):
        known = [x for x in rs if x["value"] is not None]
        mean, half = metrics.mean_ci([x["value"] / ONE for x in known])
        return {"n": len(rs), "known": len(known), "green": sum(x["green"] for x in rs),
                "would_win": sum(x["outcome"] == "would have won" for x in rs),
                "value": sum(x["value"] for x in known), "mean": mean,
                "ci": None if half is None else (mean - half, mean + half), "fees": sum(x["fees"] for x in rs)}

    return {"rows": rows, "summary": {"all": summary(rows), "singles": summary([x for x in rows if not x["is_parlay"]]),
                                      "parlays": summary([x for x in rows if x["is_parlay"]])}}
```

(`habits.py` imports `metrics`; check for a circular import — `metrics` must not import `habits`.)

`docs/pnl-rules.md`, Habits: *Cash-out value (hindsight) = what you got for the part you sold (proceeds − exit fees) minus what holding that part would have paid, judged by the market's final result. Positive = cashing out helped. Covers full and partial exits; void or unresolved markets are pending and excluded. Luck-dominated at small n: always shown with its CI. Fair-price cashing out is break-even in expectation; its true cost is exit fees plus the spread (spread at exit: later, with entry spread).*

- [ ] **Step 4: Run, expect PASS**; full suite; print the real summary.
- [ ] **Step 5: Commit** — `Habits: cash-out hindsight value`.

The Habits tab (Task 5) gets a **Cash-outs** section: a summary table (rows All / Singles / Parlays: cash-outs, green, would have won if held, hindsight value with CI, exit fees), one sentence on how to read it ("Positive = cashing out helped. Mostly luck at this sample size; the real cost of cashing out is fees plus the spread, which isn't measured yet."), and the per-cash-out table (Closed | Bet | Entry | Exit | Result if held | Got | Holding paid | Value) in `fold("Show all N cash-outs", ...)`.

### Task 5: Tabbed layout, header, headline tiles with venue switch

**Files:**
- Modify: `sharp_check/app.py`
- Create: `tests/test_app.py`

**Interfaces:**
- Consumes: `metrics.labels/recent_bets/open_bets/headline/clv/parlay_clv/own_money/segments/linkage`, `habits.*`, `reconcile.*`.
- Produces: `layout(conn=None)`, `tiles(conn, venues) -> list`, `header(conn)`, `overview(conn)`, `clv_tab(conn)`, `performance_tab(conn)`, `habits_tab(conn)`, `data_tab(conn)`; element ids `scope` (venue RadioItems), `tiles`, `theme` (Dropdown), `bankroll-overview`, `bankroll-habits` (Graphs).

- [ ] **Step 1: Smoke test**

```python
"""Dashboard renders. Run: python -m pytest tests/  (no network)."""
from sharp_check import app, archive, normalize


def ids(node, found=None):
    found = set() if found is None else found
    if getattr(node, "id", None):
        found.add(node.id)
    kids = getattr(node, "children", None)
    for k in kids if isinstance(kids, (list, tuple)) else [kids] if kids is not None else []:
        if hasattr(k, "to_plotly_json"):
            ids(k, found)
    return found


def test_empty_db_renders_every_tab_and_the_tiles():
    conn = archive.connect(":memory:")
    normalize.rebuild(conn)
    page = app.layout(conn)
    assert {"scope", "tiles", "theme", "tabs"} <= ids(page)
    tabs = [t for t in page.children if getattr(t, "id", None) == "tabs"][0].children
    assert [t.label for t in tabs] == ["Overview", "CLV", "Performance", "Habits", "Data health"]
    for venues in app.SCOPES.values():
        assert len(app.tiles(conn, venues)) == 5
```

- [ ] **Step 2: Run, expect FAIL.**

- [ ] **Step 3: Implement** — restructure `app.py`. Keep `usd`, `cents`, `table`, `headline_cells`, `betting_rows`, `segment_sections`, `linkage_section`, `clv_section`, `parlay_clv_section`, `habits_section`, `own_money_rows` and their explanatory texts; changes:
  - `table(header, rows, minor=())`: column indexes in `minor` get `className="minor"` on `th`/`td`; numbers right-align via a `num` class when the cell is a str starting with `$`, `-$`, `+`, `−`, a digit, or ending in `¢`/`%`.
  - `fold(summary, *children)` = `html.Details([html.Summary(summary), *children])`.
  - Per-bet CLV table, parlay table and stake-% top 5 go inside `fold(...)`; bet columns show `labels` (parlay legs joined with ` · ` in a `sub` span); the "No close", exclusions and linkage blocks move to Data health.
  - `tiles(conn, venues)`:

```python
def tile(key, value, sub, sign=None):
    cls = "tile-v" + ("" if sign is None else " pos" if sign > 0 else " neg" if sign < 0 else "")
    return html.Div([html.Div(key, className="tile-k"), html.Div(value, className=cls), html.Div(sub, className="tile-s")],
                    className="tile")


def clv_tile(key, c, n_key):
    if c["net_cents"] is None:
        return tile(key, "–", "no bets with CLV yet")
    lo, hi = c["net_ci"] or (None, None)
    ci = "" if lo is None else f"CI {lo:+.1f} to {hi:+.1f} · "
    return tile(key, f"{c['net_cents']:+.1f}¢", f"after fees · {ci}n {c[n_key]}", c["net_cents"])


def tiles(conn, venues):
    h = metrics.headline(conn, venues)
    lo, hi = h["ci"]
    roi = "–" if h["roi"] is None else f"{h['roi']:+.1%}"
    return [
        tile("Betting P&L", usd(h["pnl"]), f"ROI {roi} · {h['bets']} closed bets", h["pnl"]),
        tile("ROI", roi, f"on {usd(h['stake'])} staked", h["roi"]),
        clv_tile("CLV · singles", metrics.clv(conn, venues), "with_close"),
        clv_tile("CLV · parlays", metrics.parlay_clv(conn, venues), "with_clv"),
        tile("Wins vs expected", f"{h['wins']} / {h['expected']:.1f}",
             f"CI {max(lo, 0):.1f}–{hi:.1f} · {h['settled']} judged"),
    ]
```

   The ROI tile has class `tile roi` so the phone CSS can hide it (the P&L subtitle already carries ROI).
  - `header(conn)`: brand; `Synced <newest fetched_at[:16]> UTC` (or `never`); `✓ reconciled` / `✗ cash mismatch` (an `html.A(href="#data")`) from

```python
def reconciled(conn):
    """True when both venues' fills and bets cash match the reported balance; None if no balance snapshot yet."""
    try:
        for venue in metrics.VENUES:
            reported = reconcile.reported_cash(conn, venue)
            for computed in (reconcile.computed_cash(conn, venue)[0], reconcile.bets_cash(conn, venue)):
                if abs(computed - reported) > reconcile.TOLERANCE:
                    return False
    except TypeError:  # no balance snapshot: reported_cash reads row None
        return None
    return True
```

   and the theme picker `dcc.Dropdown(id="theme", options=[{"label": t.title(), "value": t} for t in THEMES], value="system", clearable=False, searchable=False, persistence=True, persistence_type="local", className="theme-pick")` with `THEMES = ("system", "light", "dark", "blue", "bears", "giants")`.
  - `overview(conn)`: `dcc.RadioItems(id="scope", options=list(SCOPES), value="Both", inline=True, className="seg")`, `html.Div(tiles(conn, metrics.VENUES), id="tiles", className="tiles")`, open positions (`h3` with count and cost; table Placed | Bet | Venue | Starts | Stake | Entry, minor = Placed, Venue, Entry), recent bets (table Closed | Bet | Venue | Result | Stake | CLV after fees | P&L, minor = Closed, Venue, Result, Stake; the Bet cell carries legs and, on phone, the date/result line in a `sub` span), bankroll chart `id="bankroll-overview"`.
  - CLV after fees cell: `f"{clv_net / 10_000:+.1f}¢"`; empty with `sub` "live" when `is_live`, "–" otherwise. P&L and CLV cells get `pos`/`neg`.
  - `layout(conn=None)`: `conn = conn or archive.connect()`; returns `html.Div([header(conn), dcc.Tabs(id="tabs", value="overview", children=[dcc.Tab(label=..., value=..., children=..., className="tab", selected_className="tab--on") ...], className="tabs"), dcc.Store(id="theme-applied")], className="page")`. Tab values: `overview`, `clv`, `performance`, `habits`, `data`.
  - Callback:

```python
@app.callback(Output("tiles", "children"), Input("scope", "value"), prevent_initial_call=True)
def scope_tiles(scope):
    return tiles(archive.connect(), SCOPES[scope])
```

   `app = Dash(__name__, title="sharp-check")` must be defined before the decorator; `app.layout = layout` (Dash calls it with no args; the smoke test passes a conn).

- [ ] **Step 4: Run, expect PASS**; full suite. Start the app, open http://127.0.0.1:8050, check every tab renders on real data (unstyled is fine here).
- [ ] **Step 5: Commit** — `Dashboard: five tabs, header, headline tiles with venue switch`.

### Task 6: Quiet-ledger styles, five themes, themed charts, phone layout

**Files:**
- Create: `sharp_check/assets/style.css`, `sharp_check/assets/theme.js`
- Modify: `sharp_check/app.py` (chart colours via CSS variables, clientside callback)

**Interfaces:**
- Consumes: ids `theme`, `theme-applied`, `bankroll-overview`, `bankroll-habits`; classes from Task 5 (`page`, `hdr`, `tiles`, `tile`, `tile-k/v/s`, `roi`, `pos`, `neg`, `num`, `minor`, `sub`, `seg`, `tab`, `tab--on`, `theme-pick`).

- [ ] **Step 1: `theme.js`** — runs before Dash renders (Dash loads `assets/*.js` in `<head>`... it loads them at the end of `<body>` before the renderer; either way it runs before the React tree mounts):

```js
// Theme: applied before Dash renders to avoid a flash; the picker's clientside callback keeps it in sync.
(function () {
  try {
    var t = localStorage.getItem("sharp-theme");
    if (t && t !== "system") document.documentElement.dataset.theme = t;
  } catch (e) { /* storage blocked: System theme */ }
})();

window.dash_clientside = Object.assign({}, window.dash_clientside, {
  sharp: {
    // Sets data-theme, remembers it, and restyles both charts from the computed CSS variables.
    theme: function (theme, figA, figB) {
      var root = document.documentElement;
      if (theme && theme !== "system") root.dataset.theme = theme; else delete root.dataset.theme;
      try { localStorage.setItem("sharp-theme", theme || "system"); } catch (e) {}
      var css = getComputedStyle(root), v = function (n) { return css.getPropertyValue(n).trim(); };
      function paint(fig) {
        if (!fig) return window.dash_clientside.no_update;
        var f = JSON.parse(JSON.stringify(fig));
        f.layout = Object.assign({}, f.layout, {
          paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
          font: Object.assign({}, f.layout.font, { color: v("--fg") }),
          xaxis: Object.assign({}, f.layout.xaxis, { gridcolor: v("--line"), linecolor: v("--line") }),
          yaxis: Object.assign({}, f.layout.yaxis, { gridcolor: v("--line"), zerolinecolor: v("--muted") })
        });
        (f.data || []).forEach(function (tr, i) { tr.line = Object.assign({}, tr.line, { color: v("--series-" + (i + 1)) }); });
        return f;
      }
      return [theme, paint(figA), paint(figB)];
    }
  }
});
```

- [ ] **Step 2: Clientside callback in `app.py`**

```python
app.clientside_callback(
    ClientsideFunction("sharp", "theme"),
    Output("theme-applied", "data"), Output("bankroll-overview", "figure"), Output("bankroll-habits", "figure"),
    Input("theme", "value"), State("bankroll-overview", "figure"), State("bankroll-habits", "figure"),
)
```

When there's no bankroll series (empty DB) render the `dcc.Graph`s with an empty `go.Figure()` so these ids always exist. `bankroll_chart` drops its hard-coded colours (`SERIES`, `plot_bgcolor`, `paper_bgcolor`, font colour); keeps layout, hover, legend.

- [ ] **Step 3: `style.css`** — tokens per theme (values from the spec table, plus `--series-1/2`), System = Light with Dark under `prefers-color-scheme`:

```css
:root, [data-theme="light"] {
  --bg:#fbfaf8; --card:#fff; --fg:#1c1b19; --muted:#77736b; --line:#e8e5df; --accent:#1c1b19; --on-accent:#fbfaf8;
  --pos:#2f7a4b; --neg:#b4372f; --series-1:#2a78d6; --series-2:#7a6f5c;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme]) {
    --bg:#141413; --card:#1c1c1a; --fg:#ecebe7; --muted:#9a978f; --line:#2b2a27; --accent:#ecebe7; --on-accent:#141413;
    --pos:#5cc58a; --neg:#f07a6e; --series-1:#6aa6f0; --series-2:#b8ad98;
  }
}
[data-theme="dark"] { /* same values as the dark block above */ }
[data-theme="blue"] { --bg:#0e1a2b; --card:#132339; --fg:#e7eef8; --muted:#8fa3bd; --line:#1f3350; --accent:#4b8ff0;
  --on-accent:#fff; --pos:#5fd39a; --neg:#ff8577; --series-1:#4b8ff0; --series-2:#c9d6e8; }
[data-theme="bears"] { --bg:#0b162a; --card:#111f38; --fg:#eef1f6; --muted:#93a0b5; --line:#1c2c4a; --accent:#c83803;
  --on-accent:#fff; --pos:#5fd39a; --neg:#ff8a8a; --series-1:#7fa7e0; --series-2:#c9d1de; }
[data-theme="giants"] { --bg:#121212; --card:#1b1a19; --fg:#efe6d6; --muted:#a39a8b; --line:#2c2a27; --accent:#fd5a1e;
  --on-accent:#121212; --pos:#5fd39a; --neg:#ff8a8a; --series-1:#efe6d6; --series-2:#a39a8b; }
```

Then base and components (full file written in this step): `body` background/colour from tokens, `system-ui`, `font-variant-numeric: tabular-nums`; `.page` max-width 1100px, 16px side gutter; header flex row; `.tabs` (override Dash's inline tab styles: transparent background, no borders, `--muted` text, active tab `--fg` with a 2px `--accent` underline; the tab bar may scroll horizontally inside itself); `.seg` segmented control (labels as pills, hidden radio inputs, checked pill `--accent` on `--on-accent`); `.tiles` grid `repeat(5, 1fr)`; `.tile` card; `.tile-v` 22px/600; `table` full width, hairline row borders, `th` muted 500, `.num` right-aligned; tables wrapped in `.scroll { overflow-x: auto }`; `.pos`/`.neg` colours; `.sub` 11px muted; `details > summary` styled as a muted link; `.theme-pick` sized to ~120px and themed (Dash 4 dropdown: inspect its class names in the browser and theme the control, menu and options). Phone `@media (max-width: 600px)`: `.tiles` 2 columns, first tile spans both, `.tile.roi` hidden; `.minor` hidden; smaller tab padding.

- [ ] **Step 4: Run** the full suite and the app; quick look in all six theme choices.
- [ ] **Step 5: Validate colours** with the `dataviz` skill's validator (load the skill): pos/neg and series against each theme's `--bg`/`--card`; adjust failing values.
- [ ] **Step 6: Commit** — `Dashboard: quiet-ledger styles, five themes, themed charts, phone layout`.

### Task 7: Exit check, review, docs

- [ ] **Step 1: Exit check in Chrome** (claude-in-chrome skill; screenshots saved):
  1. 1280×800: five tiles visible without scrolling.
  2. Each tab one click.
  3. 375px wide, every tab × every theme: `document.documentElement.scrollWidth <= innerWidth` via the JS tool.
  4. Switch every theme: green/red readable; chart restyles.
- [ ] **Step 2: Logic review** — dispatch a read-only general-purpose subagent with the spec, this plan, the commit range and the Review Focus list plus: label rules on real data, yes_value vs settlements consistency (every settled bet whose market has `yes_value` agrees with its settlement), wins numbers, recent-bets ordering with Kalshi 5-digit timestamps. Fix real findings with tests; record latent ones in the roadmap.
- [ ] **Step 3: Docs** (same commit as the last code change they describe, or a final docs commit):
  - `docs/roadmap.md`: current phase → 7 done; Phase 7 summary line; resume point; remove "PM parlay legs have NULL market_type" from latent issues; add the wins-vs-expected decision.
  - `docs/data-model.md`: `markets` columns `label_yes`, `label_no`, `yes_value`; PM market lookups include legs and are re-fetched until resolved.
  - `docs/polymarket-us.md`: resolution fields (`status = MARKET_STATUS_RESOLVED`, `outcomePrices` JSON string, `marketSides[].description/team`), legs looked up individually, 14-day give-up.
  - `docs/pnl-rules.md`: done in Task 3.
- [ ] **Step 4: Commit** — `Phase 7: docs and resume point`.
