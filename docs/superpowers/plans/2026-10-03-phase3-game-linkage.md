# Phase 3 Game Linkage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every bet gets a start time (or a `no_start` / `unlinked` flag), a live/pregame flag, sport and market type; the dashboard shows segments with n ≥ 30 and a linkage panel.

**Architecture:** A new pure `classify.py` maps tickers/slugs to league, sport and market type. `archive.py` adds two read-only public syncs (Kalshi milestones, Polymarket market records) into `raw_pages`. `normalize.py` builds `games` and `markets` from them and fills the new `bets` columns via one pure rule function. `metrics.py`/`app.py` gain a column filter, segments and a linkage panel.

**Tech Stack:** Python 3.12, SQLite, Dash, pytest.

**Spec:** `docs/superpowers/specs/2026-10-03-phase3-game-linkage-design.md`

**Deviations from spec (found while planning):**
- Polymarket: use `gateway/v1/market/slug/{slug}` (small; has `sportsMarketType`, `line`, `gameStartTime`) instead of the event endpoint (5 MB per event, ~1,100 markets). `gameStartTime` is marked deprecated; verified equal to kickoff on PHI @ CHI. Futures slugs have segment `f` (`tec-f-wc-…-winner-fra`).
- Polymarket parlay legs carry no `sportsMarketType`, so their `markets.market_type` is NULL. Only their sport and start time are used.
- Normalize prints `other` markets grouped by Kalshi series / Polymarket slug prefix (the raw type code isn't kept per row).

## Global Constraints

- Read-only. Only GET calls; milestones and gateway are public.
- Every table rebuildable from `raw_pages` (plus `data/manual_cash.csv`). No manual game file.
- Timestamps UTC, compared as `iso_us` strings (`YYYY-MM-DDTHH:MM:SS.ffffffZ`). Money as integer micro-dollars.
- Fixtures from real responses: scrub account IDs and balances (market data needs none).
- Tests: `python -m pytest tests/`, no network.

## Review Focus

1. Start time `…00:15:00Z` vs fill `…00:15:00.5Z` compared as raw strings sorts wrong (`.` < `Z`). Both must go through `iso_us`. Test in Task 3.
2. Kalshi `/milestones` returns unrelated milestones if the filter is wrong; linking must only use `related_event_tickers`. Test in Task 2.
3. A parlay leg missing from `markets` (record not archived) must make the bet `unlinked`, not crash. Test in Task 3.
4. Re-running archive must not refetch games that already started. Test in Task 2.
5. Rebuild on the live DB, whose `bets` table has the old 17-column schema: derived tables must be dropped and recreated. Test in Task 3.

---

### Task 1: Classification

**Files:**
- Create: `sharp_check/classify.py`
- Test: `tests/test_classify.py`

**Interfaces:**
- Produces: `is_kalshi_game(ticker) -> bool`, `kalshi_market(ticker) -> (league|None, sport, market_type)`, `game_event(event_ticker) -> str|None`, `pm_market(slug, sports_market_type, start) -> (league|None, sport|None, market_type|None)`, `pm_league(slug) -> (league, sport)`.

- [ ] **Step 1: Write the failing test** (`tests/test_classify.py`)

```python
"""Market classification from tickers and slugs. Run: python -m pytest tests/  (no network)."""
from sharp_check import classify


def test_kalshi_market_types():
    cases = {
        "KXNFLGAME-26JAN18LACHI-CHI": ("NFL", "football", "moneyline"),
        "KXNFLSPREAD-26SEP24ATLGB-GB6": ("NFL", "football", "spread"),
        "KXNHLTOTAL-25NOV29SJVGK-6": ("NHL", "hockey", "total"),
        "KXNFLTD-26SEP24ATLGB-ATLBROBINSON7": ("NFL", "football", "prop"),
        "KXMLBKS-26JUL011310TEXCLE-CLEJCANTILLO54": ("MLB", "baseball", "prop"),
        "KXWCADVANCE-26JUL01USABIH-USA": ("WC", "soccer", "moneyline"),
        "KXNCAAMBGAME-25NOV24ASUTEX-ASU": ("NCAAMB", "basketball", "moneyline"),
        "KXNFLNFCCHAMP-27-CHI": ("NFL", "football", "future"),
        "KXPGATOUR-MAST26-SCHEFFLER": ("PGA", "golf", "future"),
        "KXOSCARPIC-26-X": (None, "other", "future"),
        "KXNFLWEIRD-26SEP24ATLGB-X": ("NFL", "football", "other"),  # unknown kind on a game
        "KXMVESPORTSMULTIGAMEEXTENDED-S2026-ABC": (None, None, "parlay"),
    }
    for ticker, want in cases.items():
        assert classify.kalshi_market(ticker) == want, ticker


def test_game_event_maps_prop_event_to_game_event():
    assert classify.game_event("KXNFLTD-26SEP24ATLGB") == "KXNFLGAME-26SEP24ATLGB"
    assert classify.game_event("KXMLBKS-26JUL011310TEXCLE") == "KXMLBGAME-26JUL011310TEXCLE"
    assert classify.game_event("KXNFLNFCCHAMP-27") is None
    assert classify.game_event("KXOSCARPIC-26") is None


def test_pm_market_types():
    start = "2026-09-29T00:15:00Z"
    cases = {
        ("astatc-nfl-phi-chi-2026-09-28-recyd-dswi-gte25", "football_player_receiving_yards"): ("NFL", "football", "prop"),
        ("aec-nfl-gb-min-2026-09-13", "football_team_full_game_winner"): ("NFL", "football", "moneyline"),
        ("atc-fwc-esp-arg-2026-07-19-esp", "soccer_team_full_time_winner"): ("WC", "soccer", "moneyline"),
        ("aadc-fwc-esp-arg-2026-07-19-to-advance", "soccer_game_to_advance"): ("WC", "soccer", "moneyline"),
        ("asc-nfl-ne-sea-2026-09-09-ne-3pt5", "football_team_full_game_spread"): ("NFL", "football", "spread"),
        ("tsc-mlb-bal-hou-2026-07-18-8pt5", "baseball_team_full_game_total"): ("MLB", "baseball", "total"),
        ("tsc-nfl-ne-sea-2026-09-09-ne-pts", "football_team_points_full_game_total"): ("NFL", "football", "other"),
        ("asc-nfl-ne-sea-2026-09-09-1h", "football_team_first_half_spread"): ("NFL", "football", "other"),
        ("caoc-208bcbdddb50f018", None): (None, None, "parlay"),
    }
    for (slug, smt), want in cases.items():
        assert classify.pm_market(slug, smt, start) == want, slug
    assert classify.pm_market("tec-f-wc-2026-07-19-winner-fra", None, None) == ("WC", "soccer", "future")
    assert classify.pm_market("aec-nfl-gb-min-2026-09-13", "football_team_full_game_winner", None)[2] == "future"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_classify.py -v` — Expected: FAIL, `cannot import name 'classify'`.

- [ ] **Step 3: Implement** (`sharp_check/classify.py`)

```python
"""Phase 3: league, sport and market type from Kalshi tickers and Polymarket slugs. Never from titles."""
import re

GAME_CODE = re.compile(r"\d{2}[A-Z]{3}\d{2}")  # 26SEP24ATLGB: yymmmdd, then teams. Futures have 27, MAST26, ...

# Kalshi series = "KX" + league + kind, e.g. KXNFLSPREAD.
KALSHI_LEAGUES = {"NCAAMB": "basketball", "NCAAF": "football", "NFL": "football", "NBA": "basketball",
                  "MLB": "baseball", "NHL": "hockey", "WC": "soccer", "PGA": "golf", "UFC": "mma"}
KALSHI_KINDS = {"GAME": "moneyline", "ADVANCE": "moneyline", "SPREAD": "spread", "TOTAL": "total",
                **dict.fromkeys(("ANYTD", "FIRSTTD", "TD", "REC", "PASSYDS", "RECYDS", "RSHYDS", "KS", "PTS", "GOAL",
                                 "FIRSTGOAL", "BTTS"), "prop")}
PM_LEAGUES = {"nfl": ("NFL", "football"), "cfb": ("NCAAF", "football"), "mlb": ("MLB", "baseball"),
              "nba": ("NBA", "basketball"), "nhl": ("NHL", "hockey"), "fwc": ("WC", "soccer"), "wc": ("WC", "soccer")}


def kalshi_league(ticker):
    """(league, kind) from the series: KXNFLSPREAD -> ("NFL", "SPREAD"). (None, whole body) if no known league."""
    body = ticker.split("-")[0][2:]
    for league in sorted(KALSHI_LEAGUES, key=len, reverse=True):
        if body.startswith(league):
            return league, body[len(league):]
    return None, body


def is_kalshi_game(ticker):
    parts = ticker.split("-")
    return len(parts) > 1 and bool(GAME_CODE.match(parts[1]))


def kalshi_market(ticker):
    if ticker.startswith("KXMVE"):
        return None, None, "parlay"
    league, kind = kalshi_league(ticker)
    sport = KALSHI_LEAGUES.get(league, "other")
    if not is_kalshi_game(ticker):
        return league, sport, "future"
    return league, sport, KALSHI_KINDS.get(kind, "other")


def game_event(event_ticker):
    """A prop event's game event, which is what milestones link: KXNFLTD-26SEP24ATLGB -> KXNFLGAME-26SEP24ATLGB."""
    league, _ = kalshi_league(event_ticker)
    if league is None or not is_kalshi_game(event_ticker):
        return None
    return f"KX{league}GAME-{event_ticker.split('-')[1]}"


def pm_league(slug):
    parts = slug.split("-")
    key = parts[2] if parts[1] == "f" else parts[1]  # futures: tec-f-wc-...
    return PM_LEAGUES.get(key, (key.upper(), "other"))


def pm_type(smt):
    smt = smt or ""
    if "_player_" in smt:
        return "prop"
    if smt.endswith(("_full_game_winner", "_full_time_winner", "_to_advance")):
        return "moneyline"
    if smt.endswith("_full_game_spread"):
        return "spread"
    if smt.endswith(("_team_full_game_total", "_game_full_game_total")):  # team_points_full_game_total = team total
        return "total"
    return "other"


def pm_market(slug, sports_market_type, start):
    if slug.startswith("caoc-"):
        return None, None, "parlay"
    league, sport = pm_league(slug)
    if slug.split("-")[1] == "f" or start is None:
        return league, sport, "future"
    return league, sport, pm_type(sports_market_type)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_classify.py -v` — Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add sharp_check/classify.py tests/test_classify.py
git commit -m "Phase 3: classify markets by league, sport and type"
```

---

### Task 2: Archive milestones and Polymarket market records

**Files:**
- Modify: `sharp_check/archive.py` (new functions after `sync_kalshi_markets`; `__main__`)
- Test: `tests/test_archive.py`

**Interfaces:**
- Consumes: `classify.is_kalshi_game`, `classify.game_event`.
- Produces: `milestones_by_event(conn) -> {event_ticker: milestone}`, `kalshi_milestone(by_event, event) -> milestone|None`, `sync_kalshi_milestones(conn, get=clients.kalshi_get, now=None)`, `sync_pm_markets(conn, get=clients.gateway_get, now=None)`. Raw endpoints: `"/milestones"` body `{"milestones": [...]}`; `"/v1/market/slug/{slug}"` body `{"market": {...}}`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_archive.py`; `Resp` already exists there)

```python
def market_page(ticker, event, legs=None):
    return {"market": {"ticker": ticker, "event_ticker": event, "status": "finalized", "mve_selected_legs": legs}}


def milestone(mid, events, start):
    return {"id": mid, "related_event_tickers": events, "start_date": start, "title": "A at B", "details": {}}


def test_milestones_fall_back_to_game_event_and_skip_started_games():
    conn = archive.connect(":memory:")
    archive.store(conn, "kalshi", "/markets/{ticker}", {}, market_page("KXNFLTD-26SEP24ATLGB-X", "KXNFLTD-26SEP24ATLGB"))
    archive.store(conn, "kalshi", "/markets/{ticker}", {}, market_page("KXMVE-1", "KXMVE", [
        {"event_ticker": "KXNFLGAME-26SEP27KCMIA", "market_ticker": "KXNFLGAME-26SEP27KCMIA-KC", "side": "yes"}]))
    archive.store(conn, "kalshi", "/markets/{ticker}", {}, market_page("KXNFLNFCCHAMP-27-CHI", "KXNFLNFCCHAMP-27"))
    calls = []

    def get(path, params):
        calls.append(params["related_event_ticker"])
        hits = {"KXNFLGAME-26SEP24ATLGB": [milestone("m1", ["KXNFLGAME-26SEP24ATLGB"], "2026-09-25T00:15:00Z")],
                "KXNFLGAME-26SEP27KCMIA": [milestone("m2", ["KXNFLGAME-26SEP27KCMIA"], "2026-09-27T17:00:00Z")]}
        # A wrong filter returns unrelated milestones; they must never link.
        return Resp(200, {"milestones": hits.get(params["related_event_ticker"], [milestone("x", ["OTHER"], "2000-01-01T00:00:00Z")])})

    archive.sync_kalshi_milestones(conn, get, now="2026-10-01T00:00:00Z")
    assert sorted(calls) == ["KXNFLGAME-26SEP24ATLGB", "KXNFLGAME-26SEP27KCMIA", "KXNFLTD-26SEP24ATLGB"]  # no future
    by_event = archive.milestones_by_event(conn)
    assert archive.kalshi_milestone(by_event, "KXNFLTD-26SEP24ATLGB")["id"] == "m1"
    assert archive.kalshi_milestone(by_event, "KXNFLNFCCHAMP-27") is None

    calls.clear()
    archive.sync_kalshi_milestones(conn, get, now="2026-10-01T00:00:00Z")
    assert calls == []  # both games started: not refetched


def test_pm_markets_archived_for_singles_until_game_starts():
    conn = archive.connect(":memory:")
    single = {"marketSlug": "aec-nfl-gb-min-2026-09-13", "comboLegDetails": []}
    parlay = {"marketSlug": "caoc-1", "comboLegDetails": [{"slug": "aec-nfl-x"}]}
    archive.store(conn, "polymarket", "/v1/portfolio/activities", {}, {"activities": [{"trade": single}, {"trade": parlay}]})
    calls = []

    def get(path):
        calls.append(path)
        return Resp(200, {"market": {"slug": "aec-nfl-gb-min-2026-09-13", "gameStartTime": "2026-09-14T00:20:00Z"}})

    archive.sync_pm_markets(conn, get, now="2026-09-13T12:00:00Z")
    archive.sync_pm_markets(conn, get, now="2026-09-13T12:00:00Z")  # not started yet: fetched again
    archive.sync_pm_markets(conn, get, now="2026-09-15T00:00:00Z")  # started: skipped
    assert calls == ["/v1/market/slug/aec-nfl-gb-min-2026-09-13"] * 2
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_archive.py -v` — Expected: FAIL, `has no attribute 'sync_kalshi_milestones'`.

- [ ] **Step 3: Implement** (in `sharp_check/archive.py`: add `from sharp_check import classify, clients`; insert after `sync_kalshi_markets`)

```python
def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def milestones_by_event(conn):
    """Kalshi event ticker -> milestone (the game, with start_date). Latest page wins.

    Indexed only by each milestone's own related_event_tickers, so unrelated results can't link.
    """
    return {e: m for m in records(conn, "kalshi", ("/milestones",), "milestones") for e in m["related_event_tickers"]}


def kalshi_milestone(by_event, event):
    """Props aren't linked to milestones, but their game event is (same game code)."""
    return by_event.get(event) or by_event.get(classify.game_event(event))


def sync_kalshi_milestones(conn, get=clients.kalshi_get, now=None):
    """Archive the milestone for every traded Kalshi game event, parlay legs included.

    Stored as "/milestones" with params {"related_event_ticker": ...}. Only that filter works: `event_ticker` is
    silently ignored. Re-fetched until the game has started, so postponements are caught.
    """
    now = now or utc_now()
    markets = list(records(conn, "kalshi", ("/markets/{ticker}",), "market"))
    events = {m["event_ticker"] for m in markets if not m.get("mve_selected_legs")}
    events |= {leg["event_ticker"] for m in markets for leg in m.get("mve_selected_legs") or []}
    by_event = milestones_by_event(conn)

    def started(e):
        m = kalshi_milestone(by_event, e)
        return m is not None and m["start_date"] <= now

    todo = sorted(e for e in events if classify.is_kalshi_game(e) and not started(e))
    new = 0
    for e in todo:
        for q in filter(None, (e, classify.game_event(e))):
            r = get("/milestones", {"related_event_ticker": q, "limit": 5})
            r.raise_for_status()
            body = r.json()
            new += store(conn, "kalshi", "/milestones", {"related_event_ticker": q}, body)
            if any(q in m["related_event_tickers"] for m in body.get("milestones") or []):
                break
    conn.commit()
    print(f"{'kalshi':10} {'/milestones':28} events={len(todo)} new={new}")


def sync_pm_markets(conn, get=clients.gateway_get, now=None):
    """Archive the public market record (sportsMarketType, line, gameStartTime) for every traded single.

    Parlay legs carry their own eventStartTime in the trade. Re-fetched until the game has started.
    """
    now = now or utc_now()
    trades = [a["trade"] for a in records(conn, "polymarket", ("/v1/portfolio/activities",), "activities") if a.get("trade")]
    singles = {t["marketSlug"] for t in trades if not t.get("comboLegDetails")}
    stored = {m["slug"]: m for m in records(conn, "polymarket", ("/v1/market/slug/{slug}",), "market")}

    def done(slug):
        m = stored.get(slug)
        return m is not None and (m.get("gameStartTime") is None or m["gameStartTime"] <= now)

    todo = sorted(s for s in singles if not done(s))
    new = 0
    for slug in todo:
        r = get(f"/v1/market/slug/{slug}")
        r.raise_for_status()
        new += store(conn, "polymarket", "/v1/market/slug/{slug}", {"slug": slug}, r.json())
    conn.commit()
    print(f"{'polymarket':10} {'/v1/market/slug/{slug}':28} fetched={len(todo)} new={new}")
```

And in `__main__`, after `sync_kalshi_markets(conn)`:

```python
    sync_kalshi_milestones(conn)  # after market records: needs their event tickers and legs
    sync_pm_markets(conn)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/ -q` — Expected: all pass.

- [ ] **Step 5: Run against the live APIs and check counts**

Run: `python -m sharp_check.archive`
Expected: `kalshi /milestones events=~150`, `polymarket /v1/market/slug/{slug} fetched=36`, no `FAILED`. Re-run: both lines show `events=0`/`fetched=0` except games not yet started.

- [ ] **Step 6: Commit**

```bash
git add sharp_check/archive.py tests/test_archive.py
git commit -m "Phase 3: archive Kalshi milestones and Polymarket market records"
```

---

### Task 3: `games`, `markets` and linked `bets`

**Files:**
- Modify: `sharp_check/normalize.py` (SCHEMA, new functions before `rebuild`, `rebuild`, `__main__`)
- Modify: `tests/test_metrics.py` (bet helper inserts named columns)
- Test: `tests/test_normalize.py`

**Interfaces:**
- Consumes: `classify.kalshi_market`, `classify.pm_market`, `archive.milestones_by_event`, `archive.kalshi_milestone`, `iso_us`.
- Produces: tables `games(game_id, venue, league, title, start_time)`, `markets(venue, market_id, event_id, title, sport, league, market_type, line, game_id)`; `bets` columns `start_time, link, sport, market_type` (plus filled `game_id`, `is_live`); `link_rule(opened_ts, leg_starts) -> (link, start_time, is_live)`; `link_bets(conn)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_normalize.py`)

```python
def test_link_rule_cases():
    rule = normalize.link_rule
    opened = "2026-09-25T00:15:00.500000Z"
    assert rule(opened, []) == ("no_start", None, None)                                        # future / all-future parlay
    assert rule(opened, ["2026-09-25T00:15:00.000000Z"]) == ("game", "2026-09-25T00:15:00.000000Z", 1)  # same second: live
    assert rule(opened, ["2026-09-25T01:00:00.000000Z"]) == ("game", "2026-09-25T01:00:00.000000Z", 0)
    assert rule(opened, [None]) == ("unlinked", None, None)                                    # game not found
    later, earlier = "2026-09-26T00:00:00.000000Z", "2026-09-24T00:00:00.000000Z"
    assert rule(opened, [later, None]) == ("unlinked", None, None)                             # nothing started, a leg unknown
    assert rule(opened, [None, earlier, later]) == ("game", earlier, 1)                        # a leg started: live anyway
    assert rule(opened, [later, "2026-09-25T12:00:00.000000Z"]) == ("game", "2026-09-25T12:00:00.000000Z", 0)


def test_rebuild_links_bets_end_to_end_and_replaces_old_bets_schema():
    conn = archive.connect(":memory:")
    conn.execute("CREATE TABLE bets (bet_id TEXT)")  # an older schema left in the live DB
    store = lambda *a: archive.store(conn, *a)
    fill = lambda fid, ticker, ts: {"fill_id": fid, "order_id": "o", "market_ticker": ticker, "outcome_side": "yes",
                                    "count_fp": "1.00", "yes_price_dollars": "0.5000", "fee_cost": "0", "is_taker": True,
                                    "created_time": ts}
    store("kalshi", "/portfolio/fills", {}, {"fills": [
        fill("f1", "KXNFLTD-26SEP24ATLGB-X", "2026-09-24T22:00:00Z"),         # prop, pregame via game event
        fill("f2", "KXNFLGAME-26JAN18LACHI-CHI", "2026-01-19T02:25:09Z"),     # live
        fill("f3", "KXNFLNFCCHAMP-27-CHI", "2026-01-01T00:00:00Z"),           # future
        fill("f4", "KXMVE-1", "2026-09-24T22:00:00Z"),                        # parlay: one known leg, one missing
    ]})
    market = lambda t, e, legs=None: {"market": {"ticker": t, "event_ticker": e, "title": t, "status": "active",
                                                 "floor_strike": None, "mve_selected_legs": legs}}
    store("kalshi", "/markets/{ticker}", {}, market("KXNFLTD-26SEP24ATLGB-X", "KXNFLTD-26SEP24ATLGB"))
    store("kalshi", "/markets/{ticker}", {}, market("KXNFLGAME-26JAN18LACHI-CHI", "KXNFLGAME-26JAN18LACHI"))
    store("kalshi", "/markets/{ticker}", {}, market("KXNFLNFCCHAMP-27-CHI", "KXNFLNFCCHAMP-27"))
    store("kalshi", "/markets/{ticker}", {}, market("KXMVE-1", "KXMVE", [
        {"event_ticker": "KXNFLGAME-26SEP24ATLGB", "market_ticker": "KXNFLGAME-26SEP24ATLGB-GB", "side": "yes"},
        {"event_ticker": "KXNBAGAME-26SEP24AB", "market_ticker": "KXNBAGAME-26SEP24AB-A", "side": "yes"}]))
    ms = lambda mid, e, start: {"id": mid, "related_event_tickers": [e], "start_date": start, "title": e,
                                "details": {"league": "NFL"}}
    store("kalshi", "/milestones", {}, {"milestones": [ms("m1", "KXNFLGAME-26SEP24ATLGB", "2026-09-25T00:15:00Z"),
                                                       ms("m2", "KXNFLGAME-26JAN18LACHI", "2026-01-18T23:30:00Z")]})

    normalize.rebuild(conn)
    got = {r[0]: r[1:] for r in conn.execute(
        "SELECT market_id, link, is_live, start_time, sport, market_type, game_id FROM bets")}
    assert got["KXNFLTD-26SEP24ATLGB-X"] == ("game", 0, "2026-09-25T00:15:00.000000Z", "football", "prop", "kalshi:m1")
    assert got["KXNFLGAME-26JAN18LACHI-CHI"][:2] == ("game", 1)
    assert got["KXNFLNFCCHAMP-27-CHI"][:2] == ("no_start", None)
    assert got["KXMVE-1"] == ("unlinked", None, None, "multi", "parlay", None)  # NBA leg has no milestone
    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == 2
```

In `tests/test_metrics.py`, replace the `bet` helper so it names its columns:

```python
def bet(conn, n, is_parlay, status, stake, pnl, avg_entry, payout):
    conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, entry_qty, avg_entry,
                    stake, exit_qty, exit_proceeds, exit_fees, payout, realized_pnl, status)
                    VALUES (?, 'kalshi', 'M', 'yes', ?, 't', 1, ?, ?, 0, 0, 0, ?, ?, ?)""",
                 (f"b{n}", is_parlay, avg_entry, stake, payout, pnl, status))
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/ -q` — Expected: the two new tests FAIL (`no attribute 'link_rule'`).

- [ ] **Step 3: Implement** (`sharp_check/normalize.py`)

Imports: `from sharp_check import archive, classify`.

Append to the `bets` CREATE in SCHEMA (before the closing `);`, after `status`):

```sql
    status TEXT NOT NULL,       -- open | closed_early | settled | void
    start_time TEXT,            -- game start; earliest game leg for parlays
    link TEXT,                  -- game | no_start | unlinked
    sport TEXT,
    market_type TEXT
);
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,   -- kalshi:<milestone id> | polymarket:<event slug>
    venue TEXT NOT NULL,
    league TEXT,
    title TEXT,
    start_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS markets (
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    event_id TEXT,
    title TEXT,
    sport TEXT,
    league TEXT,
    market_type TEXT,           -- moneyline | spread | total | prop | future | parlay | other; NULL for PM parlay legs
    line REAL,
    game_id TEXT,
    PRIMARY KEY (venue, market_id)
);
```

(Remove the old `status TEXT NOT NULL ... -- open | ...` line so it isn't duplicated.)

New functions before `rebuild`:

```python
DERIVED = ("fills", "cash_ledger", "parlay_legs", "settlements", "bets", "games", "markets")


def kalshi_games_markets(conn):
    """Yields ("game", row) and ("market", row). Traded markets first, so their titles beat bare leg rows."""
    by_event = archive.milestones_by_event(conn)
    for m in {m["id"]: m for m in by_event.values()}.values():
        yield "game", (f"kalshi:{m['id']}", "kalshi", m["details"].get("league"), m["title"], iso_us(m["start_date"]))
    markets = list(records(conn, "kalshi", ("/markets/{ticker}",), "market"))
    rows = [(m["ticker"], m["event_ticker"], m["title"], m.get("floor_strike")) for m in markets]
    rows += [(l["market_ticker"], l["event_ticker"], None, None) for m in markets for l in m.get("mve_selected_legs") or []]
    for ticker, event, title, line in rows:
        league, sport, mtype = classify.kalshi_market(ticker)
        ms = archive.kalshi_milestone(by_event, event) if mtype not in ("future", "parlay") else None
        yield "market", ("kalshi", ticker, event, title, sport, league, mtype, line, ms and f"kalshi:{ms['id']}")


def pm_games_markets(conn):
    stored = {m["slug"]: m for m in records(conn, "polymarket", ("/v1/market/slug/{slug}",), "market")}
    for a in records(conn, "polymarket", ("/v1/portfolio/activities",), "activities"):
        t = a.get("trade")
        if t is None:
            continue
        slug, legs = t["marketSlug"], t.get("comboLegDetails") or []
        if legs:
            yield "market", ("polymarket", slug, None, None, None, None, "parlay", None, None)
            for l in legs:  # legs are always game markets with their own start time; no sportsMarketType
                game = f"polymarket:{l['eventSlug']}"
                league, sport = classify.pm_league(l["slug"])
                yield "game", (game, "polymarket", league, l["eventSlug"], iso_us(l["eventStartTime"]))
                yield "market", ("polymarket", l["slug"], l["eventSlug"], l["title"], sport, league, None, None, game)
            continue
        mine = t["aggressorExecution"] if t["isAggressor"] else t["passiveExecution"]
        event = mine["order"]["marketMetadata"]["eventSlug"]
        m = stored.get(slug, {})
        start = m.get("gameStartTime")
        league, sport, mtype = classify.pm_market(slug, m.get("sportsMarketType"), start)
        game = f"polymarket:{event}" if start and mtype != "future" else None
        if game:
            yield "game", (game, "polymarket", league, event, iso_us(start))
        yield "market", ("polymarket", slug, event, m.get("question"), sport, league, mtype, m.get("line"), game)


def link_rule(opened_ts, leg_starts):
    """(link, start_time, is_live) for a bet. leg_starts: start per game leg (a single is one leg), None if not found.

    Live if any leg had started when the bet opened; unknown legs only matter if none had.
    """
    if not leg_starts:
        return "no_start", None, None
    known = [s for s in leg_starts if s]
    start = min(known) if known else None
    if any(s <= opened_ts for s in known):
        return "game", start, 1
    if len(known) < len(leg_starts):
        return "unlinked", None, None
    return "game", start, 0


def link_bets(conn):
    markets = {(v, m): (g, s, t) for v, m, g, s, t in
               conn.execute("SELECT venue, market_id, game_id, sport, market_type FROM markets")}
    starts = dict(conn.execute("SELECT game_id, start_time FROM games"))
    legs = {}
    for v, m, leg in conn.execute("SELECT venue, market_id, leg_market_id FROM parlay_legs"):
        legs.setdefault((v, m), []).append(markets.get((v, leg), (None, None, None)))
    updates = []
    for bet_id, v, m, opened, is_parlay in conn.execute(
            "SELECT bet_id, venue, market_id, opened_ts, is_parlay FROM bets").fetchall():
        if is_parlay:
            info = legs.get((v, m), [])
            sports = {s for _, s, _ in info if s}
            game_id, sport, mtype = None, sports.pop() if len(sports) == 1 else "multi", "parlay"
        else:
            game_id, sport, mtype = markets.get((v, m), (None, None, None))
            info = [(game_id, sport, mtype)]
        leg_starts = [starts.get(g) for g, _, t in info if t != "future"]
        updates.append((game_id, sport, mtype, *link_rule(iso_us(opened), leg_starts), bet_id))  # same format as starts
    conn.executemany("UPDATE bets SET game_id = ?, sport = ?, market_type = ?, link = ?, start_time = ?, is_live = ?"
                     " WHERE bet_id = ?", updates)
```

In `rebuild`, replace `conn.executescript(SCHEMA)` and the `DELETE FROM` lines with a drop-and-create (derived schemas change between phases), then add games/markets/linking after bets:

```python
def rebuild(conn, manual_path=None):
    with conn:
        for table in DERIVED:  # derived tables are dropped, not emptied, so schema changes apply
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA)
    with conn:
        # ... existing fills / cash_ledger / parlay_legs / settlements inserts unchanged ...
        conn.executemany(f"INSERT INTO bets VALUES ({', '.join('?' * 21)})",
                         (b + (None,) * 4 for b in derive_bets(conn)))
        for source in (kalshi_games_markets(conn), pm_games_markets(conn)):
            for kind, row in list(source):
                table = "games" if kind == "game" else "markets"
                conn.execute(f"INSERT OR {'REPLACE' if kind == 'game' else 'IGNORE'} INTO {table}"
                             f" VALUES ({', '.join('?' * len(row))})", row)
        link_bets(conn)
```

In `__main__`, after the bets print:

```python
    for venue, link, n in conn.execute("SELECT venue, link, COUNT(*) FROM bets GROUP BY venue, link"):
        print(f"{venue:10} bets link {link}={n}")
    others = conn.execute("""SELECT venue, substr(market_id, 1, instr(market_id, '-') - 1), COUNT(*)
                             FROM markets WHERE market_type = 'other' GROUP BY 1, 2""").fetchall()
    for venue, code, n in others:  # extend classify.py if a code here is really a main line or prop
        print(f"{venue:10} market_type other: {code}={n}")
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/ -q` — Expected: all pass.

- [ ] **Step 5: Rebuild live data and check known games**

Run: `python -m sharp_check.normalize && python -m sharp_check.reconcile`
Then:
```bash
sqlite3 -header -column data/sharp_check.db "SELECT market_id, link, is_live, start_time, sport, market_type FROM bets
 WHERE market_id IN ('KXNFLSPREAD-26SEP24ATLGB-GB6', 'KXNFLGAME-26JAN18LACHI-CHI'); SELECT link, COUNT(*) FROM bets GROUP BY 1;"
```
Expected: GB6 `game, 0, 2026-09-25T00:15:00.000000Z, football, spread`; LACHI `game, 1`; reconcile still OK; `unlinked` only parlays touching Super Bowl LX (≈1–2).

- [ ] **Step 6: Commit**

```bash
git add sharp_check/normalize.py tests/test_normalize.py tests/test_metrics.py
git commit -m "Phase 3: games, markets, and linked bets with live/pregame"
```

---

### Task 4: Segments and linkage panel

**Files:**
- Modify: `sharp_check/metrics.py`, `sharp_check/app.py`
- Test: `tests/test_metrics.py`

**Interfaces:**
- Consumes: `bets.is_live`, `bets.sport`, `bets.market_type`, `bets.link`.
- Produces: `headline(conn, venues=VENUES, split="all", where=None)`; `SEGMENTS`; `segments(conn, column, min_n=30) -> ([(value, headline_dict)], hidden_count)`; `linkage(conn) -> ({link: n}, [(bet_id, opened_ts, stake)])`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_metrics.py`)

```python
def test_segments_hide_small_slices_and_linkage_lists_unlinked():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    for i in range(30):
        bet(conn, i, 0, "settled", 100_000, -100_000, 500_000, 0)
    for i in range(30, 35):
        bet(conn, i, 1, "settled", 100_000, -100_000, 500_000, 0)
    conn.execute("UPDATE bets SET market_type = CASE is_parlay WHEN 1 THEN 'parlay' ELSE 'prop' END, link = 'game'")
    conn.execute("UPDATE bets SET link = 'unlinked' WHERE bet_id = 'b31'")

    shown, hidden = metrics.segments(conn, "market_type")
    assert [(v, h["bets"]) for v, h in shown] == [("prop", 30)] and hidden == 1
    counts, unlinked = metrics.linkage(conn)
    assert counts == {"game": 34, "unlinked": 1} and [u[0] for u in unlinked] == ["b31"]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_metrics.py -v` — Expected: FAIL, `no attribute 'segments'`.

- [ ] **Step 3: Implement** (`sharp_check/metrics.py`)

Change `headline` to take a filter:

```python
SEGMENTS = {"is_live": "Live vs pregame", "sport": "Sport", "market_type": "Market type"}


def headline(conn, venues=VENUES, split="all", where=None):
    """... (docstring unchanged). `where` = (column in SEGMENTS, value) narrows to one segment."""
    extra, args = "", ()
    if where:
        assert where[0] in SEGMENTS
        extra, args = f" AND {where[0]} = ?", (where[1],)
    rows = conn.execute(f"""
        SELECT status, stake, realized_pnl, avg_entry, payout
        FROM bets WHERE venue IN ({','.join('?' * len(venues))}) AND is_parlay IN ({','.join('?' * len(SPLITS[split]))}){extra}
        """, (*venues, *SPLITS[split], *args)).fetchall()
    # ... rest unchanged
```

Add:

```python
def segments(conn, column, min_n=30):
    """Headline per value of `column`, both venues. Slices with fewer than min_n closed bets are hidden (pnl-rules)."""
    assert column in SEGMENTS
    values = [v for (v,) in conn.execute(f"SELECT DISTINCT {column} FROM bets WHERE {column} IS NOT NULL ORDER BY 1")]
    rows = [(v, headline(conn, where=(column, v))) for v in values]
    shown = [r for r in rows if r[1]["bets"] >= min_n]
    return shown, len(rows) - len(shown)


def linkage(conn):
    counts = dict(conn.execute("SELECT link, COUNT(*) FROM bets GROUP BY link"))
    unlinked = conn.execute("SELECT bet_id, opened_ts, stake FROM bets WHERE link = 'unlinked' ORDER BY opened_ts").fetchall()
    return counts, unlinked
```

In `sharp_check/app.py`, extract the per-row formatting from `betting_rows` into a helper and add two sections:

```python
def headline_cells(h):
    lo, hi = h["ci"]
    return (h["bets"], usd(h["stake"]), usd(h["pnl"]), "" if h["roi"] is None else f"{h['roi']:+.1%}",
            f"{h['wins']} / {h['settled']}", f"{h['expected']:.1f} ({max(lo, 0):.1f}–{hi:.1f})")


def betting_rows(conn):
    for scope, venues in SCOPES.items():
        for split in metrics.SPLITS:
            h = metrics.headline(conn, venues, split)
            yield (scope, split, *headline_cells(h), h["open"])


LIVE_LABEL = {0: "pregame", 1: "live"}


def segment_sections(conn):
    header = ("Segment", "Closed bets", "Stake", "P&L", "ROI", "Wins / settled", "Expected wins (95% CI)")
    for column, title in metrics.SEGMENTS.items():
        shown, hidden = metrics.segments(conn, column)
        yield html.H3(title)
        yield table(header, [(LIVE_LABEL.get(v, v) if column == "is_live" else v, *headline_cells(h)) for v, h in shown])
        if hidden:
            yield html.P(f"{hidden} segment{'s' * (hidden > 1)} hidden (n < 30).")


def linkage_section(conn):
    counts, unlinked = metrics.linkage(conn)
    yield html.P(", ".join(f"{k}: {n}" for k, n in sorted(counts.items(), key=lambda kv: str(kv[0]))))
    if unlinked:
        yield table(("Unlinked bet", "Opened (UTC)", "Stake"), [(b, ts[:16], usd(s)) for b, ts, s in unlinked])
```

In `layout()`, after the own-money paragraph:

```python
        html.H2("Segments"),
        *segment_sections(conn),
        html.P("Live = first fill at or after game start. Futures and unlinked bets have no live/pregame."),
        html.H2("Game linkage"),
        *linkage_section(conn),
```

- [ ] **Step 4: Run tests and render**

Run: `python -m pytest tests/ -q` — Expected: all pass.
Run the app in the background, fetch `http://127.0.0.1:8050/_dash-layout`, check the Segments and Game linkage sections render with real numbers, then stop the app.

- [ ] **Step 5: Commit**

```bash
git add sharp_check/metrics.py sharp_check/app.py tests/test_metrics.py
git commit -m "Phase 3: segment views and game linkage panel"
```

---

### Task 5: Docs and exit check

**Files:**
- Modify: `docs/kalshi.md`, `docs/polymarket-us.md`, `docs/data-model.md`, `docs/roadmap.md`

- [ ] **Step 1: `docs/kalshi.md`** — replace the "Game time candidate" bullet with:

```markdown
- **Game start time: milestones**, not `occurrence_datetime` (that is ~3h after kickoff, about the expected end, and null on some games). `GET /milestones?related_event_ticker=<event>` (public) returns the game with `start_date` (kickoff, verified on ATL @ GB and LAR @ CHI), `related_event_tickers` and Sportradar `source_ids`. The `event_ticker` filter is silently ignored and returns unrelated milestones. Player-prop events aren't linked; look up their game event instead (`KXNFLTD-26SEP24ATLGB` → `KXNFLGAME-26SEP24ATLGB`). Milestone `type` is unreliable (`soccer_tournament_multi_leg` for single World Cup matches, `hockey_tournament` for an NHL game), so game vs future comes from the ticker: a game event's second segment is a date+teams code like `26SEP24ATLGB`.
```

- [ ] **Step 2: `docs/polymarket-us.md`** — after the "Single-market lookup" bullet add:

```markdown
- Phase 3 uses the market lookup, not the event lookup, for start time and type: an event page lists every market (~1,100 for an NFL game, ~5 MB). `gameStartTime` (deprecated) matched kickoff on PHI @ CHI. Futures slugs have segment `f` (`tec-f-wc-…`).
```

- [ ] **Step 3: `docs/data-model.md`** — replace the `markets` and `games (phase 3+)` sections and extend `bets`:

```markdown
### `markets`
- Columns: `venue, market_id, event_id, title, sport, league, market_type, line, game_id`. Built from archived Kalshi market records and parlay legs, Polymarket market records and trade legs. Classification in `sharp_check/classify.py`.
- `market_type` is one of `moneyline | spread | total | prop | future | parlay | other`; NULL for Polymarket parlay legs (no type in the trade). Only full-game lines are moneyline/spread/total; period lines and team totals are `other`.
- **Many bets are player props, not game outcomes** (...keep the existing bullet...).

### `games`
- Columns: `game_id, venue, league, title, start_time`. Per venue: `kalshi:<milestone id>` or `polymarket:<event slug>`. Cross-venue columns (`odds_event_id`, `sportradar_id`) are phase 5.
```

and in `bets`, add `start_time, link, sport, market_type` to the column list plus:

```markdown
- `link`: `game | no_start | unlinked`. Futures (and all-future parlays) are `no_start`. A parlay is live if any game leg had started at `opened_ts`; otherwise `unlinked` if any game leg has no game. `start_time` is the earliest known game leg.
```

- [ ] **Step 4: `docs/roadmap.md`** — phase 3 exit check cell becomes `Every settled bet has a game, is flagged no-start, or is listed as unlinked`; update "Next up (phase 3)" with what's done and the resume point.

- [ ] **Step 5: Verify exit check on live data**

```bash
sqlite3 data/sharp_check.db "SELECT COUNT(*) FROM bets WHERE status != 'open' AND link IS NULL"
```
Expected: `0`.

- [ ] **Step 6: Commit**

```bash
git add docs/
git commit -m "Phase 3: docs for game linkage; exit check reworded"
```
