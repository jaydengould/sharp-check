# Phase 8b: Cross-Venue Line Shopping Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A live "Line shop" tab that compares Kalshi vs Polymarket all-in prices (ask + taker fee) for pregame NFL / college football / MLB games, plus a backward-looking `line_shop` table that prices each past single against the other venue at its fill.

**Architecture:** `sharp_check/match.py` (pure) turns each venue's markets into *outcome keys* (`("ml", "home")`, `("spread", "away", -3.5)`, `("prop", "Anytime TD", "kyle monangai", 1, "yes")`, …) mapped to refs `(market_id, side)`, pairs keys present on both venues, and holds the fee model. `sharp_check/lineshop.py` fetches the slate and one game live (no DB). `archive.sync_line_shop` stores the other venue's game and markets for past singles; `archive.sync_closes` gains their fill-time quote targets; `normalize` builds `line_shop`; `habits` aggregates; `app` renders.

**Tech Stack:** Python 3.12, SQLite, Plotly Dash 4, requests, pytest (no network).

**Spec:** `docs/superpowers/specs/2026-10-05-phase8b-line-shop-design.md`

## Global Constraints

- Read-only: only GETs; never call order create/cancel/modify endpoints. Polymarket US hosts only (`api.polymarket.us`, `gateway.polymarket.us`).
- Never print, log or commit secrets. Never print response headers. Test fixtures are synthetic public market data (no account data).
- `raw_pages` is the source of truth; `line_shop` is rebuilt by `normalize`. The live checker never writes to the DB.
- Timestamps UTC. Money as integer micro-dollars (`ONE = 1_000_000`).
- Checker leagues: NFL, college football, MLB. Checker shows games that haven't started, starting within 48 h.
- Lines match exactly or not at all. Prop names normalized; zero or several matches on one venue → skipped.
- All-in per contract = ask + taker fee. Kalshi fee = ceil-to-cent(0.07 × fee_multiplier × C × p × (1−p)) per order at the usual stake, spread over C contracts. Polymarket fee = θ × p × (1−p) per contract (live θ = market `feeCoefficient`; past θ from `PM_THETA`).
- `USUAL_STAKE` = $10. Thin flag when size at the best ask × ask < `USUAL_STAKE`. "No clear edge" when |gap| < 0.5¢.
- Side sanity: a paired outcome whose two venues' mids differ by more than 15¢ is dropped ("side check failed").
- Backward-looking quote rule = 8a's: key = `archive.fill_key(other_venue, first fill ts)`; latest point at or before the key decides; two-sided; ≤ 5 min old; no fallback.
- Tests: `python -m pytest tests/` passes with no network.
- Recurring mistakes (CLAUDE.md): Kalshi fills use `outcome_side`; Dash 4 styling from the real DOM (`dcc.Dropdown` → `button.dash-dropdown`, menu `.dash-dropdown-content`); the latest quote point decides.

## Review Focus

- A 2-way game where Kalshi lists the away team's market but its NO side is the home team: both refs must land on the same key and the cheaper all-in wins → Task 1 `test_kalshi_moneyline_no_side_is_other_team` and Task 3 `test_cheapest_ref_wins`.
- A soccer event with a TIE market: NO on a team is *not* the other team → Task 1 `test_three_way_no_is_not`.
- Two players whose names normalize the same on one venue (e.g. "Josh Allen" QB and LB): the prop is skipped, not paired with the wrong player → Task 1 `test_ambiguous_prop_name_skipped`.
- One venue's API fails or times out while the other works: the game still renders with "<venue> unavailable" → Task 3 `test_one_venue_down`.
- A past single whose other-venue quote page is archived but whose latest point is one-sided or > 5 min old: unpriced with that reason, never priced from an older point → Task 5 `test_latest_point_decides`.

---

### Task 1: `match.py` — outcome keys and pairing

**Files:**
- Create: `sharp_check/match.py`
- Test: `tests/test_match.py`

**Interfaces:**
- Produces:
  - `LEAGUES: dict[str, tuple[str, str, str]]` — league → (Polymarket `tagSlug`, Kalshi milestone `competition`, Kalshi game-series prefix e.g. `"KXNFL"`).
  - `PROPS: dict[str, tuple[tuple[str, ...], str]]` — prop type → (Kalshi series, Polymarket `sportsMarketType`).
  - `norm_name(s: str) -> str`
  - `teams_from_kalshi_event(event_ticker: str) -> str | None` — `"KXNFLGAME-26OCT04NYJCHI"` → `"NYJCHI"`.
  - `teams_from_pm_slug(slug: str) -> tuple[str, str]` — event slug → (away, home) abbreviations.
  - `kalshi_outcomes(markets: list[dict], teams: str) -> dict[tuple, list[tuple[str, str]]]` — key → [(ticker, "yes"|"no")].
  - `pm_outcomes(markets: list[dict], away: str, home: str) -> dict[tuple, list[tuple[str, str]]]` — key → [(slug, "yes"|"no")].
  - `pair(k: dict, p: dict) -> dict[tuple, tuple[list, list]]` — keys on both venues.
  - `label(key: tuple, names: dict[str, str]) -> str` — readable outcome, names = {"away": ..., "home": ...}.

- [ ] **Step 1: Write the failing tests**

```python
"""Cross-venue matching. Run: python -m pytest tests/  (no network)."""
from sharp_check import match


def km(ticker, sub="", strike=None):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "yes_sub_title": sub, "floor_strike": strike}


def pm(slug, smt, sides, line=None, title=None):
    return {"slug": slug, "sportsMarketType": smt, "line": line, "title": title,
            "marketSides": [{"description": d, "team": {"abbreviation": a} if a else None} for d, a in sides]}


def test_teams():
    assert match.teams_from_kalshi_event("KXNFLGAME-26OCT04NYJCHI") == "NYJCHI"
    assert match.teams_from_kalshi_event("KXMLBGAME-26JUL172138DETLAA") == "DETLAA"
    assert match.teams_from_pm_slug("nfl-nyj-chi-2026-10-04") == ("nyj", "chi")
    assert match.teams_from_pm_slug("aec-nfl-sea-was-2025-11-02") == ("sea", "was")


def test_kalshi_moneyline_no_side_is_other_team():
    out = match.kalshi_outcomes([km("KXNFLGAME-26OCT04NYJCHI-NYJ"), km("KXNFLGAME-26OCT04NYJCHI-CHI")], "NYJCHI")
    assert sorted(out[("ml", "away")]) == [("KXNFLGAME-26OCT04NYJCHI-CHI", "no"), ("KXNFLGAME-26OCT04NYJCHI-NYJ", "yes")]
    assert sorted(out[("ml", "home")]) == [("KXNFLGAME-26OCT04NYJCHI-CHI", "yes"), ("KXNFLGAME-26OCT04NYJCHI-NYJ", "no")]


def test_three_way_no_is_not():
    out = match.kalshi_outcomes([km("KXWCGAME-26JUL18FRAENG-FRA"), km("KXWCGAME-26JUL18FRAENG-ENG"),
                                 km("KXWCGAME-26JUL18FRAENG-TIE")], "FRAENG")
    assert out[("ml", "draw")] == [("KXWCGAME-26JUL18FRAENG-TIE", "yes")]
    assert out[("not", "ml", "away")] == [("KXWCGAME-26JUL18FRAENG-FRA", "no")]
    assert ("KXWCGAME-26JUL18FRAENG-FRA", "no") not in out[("ml", "home")]


def test_spread_total_and_props_kalshi():
    out = match.kalshi_outcomes([
        km("KXNFLSPREAD-26OCT04DETCAR-DET5", strike=4.5),
        km("KXNFLTOTAL-26OCT04DETCAR-45", strike=44.5),
        km("KXNFLTD-26OCT04NYJCHI-CHIKMONANGAI25-1", "Kyle Monangai: 1+", 0.5),
        km("KXNFLANYTD-25NOV03ARIDAL-DALGPICKENS3", "George Pickens"),
        km("KXNFLFIRSTTD-26SEP20MINCHI-MINJADDISON3", "Jordan Addison"),
        km("KXNFLRECYDS-26SEP28PHICHI-CHIDSWIFT4-25", "D'Andre Swift: 25+", 24.5),
    ], "DETCAR")
    assert out[("spread", "away", -4.5)] == [("KXNFLSPREAD-26OCT04DETCAR-DET5", "yes")]
    assert out[("spread", "home", 4.5)] == [("KXNFLSPREAD-26OCT04DETCAR-DET5", "no")]
    assert out[("total", "under", 44.5)] == [("KXNFLTOTAL-26OCT04DETCAR-45", "no")]
    assert ("prop", "Anytime TD", "kyle monangai", 1, "yes") in out
    assert ("prop", "Anytime TD", "george pickens", 1, "no") in out
    assert ("prop", "First TD", "jordan addison", None, "yes") in out
    assert ("prop", "Receiving yds", "dandre swift", 25, "yes") in out


def test_pm_outcomes():
    ms = [pm("aec-nfl-nyj-chi-2026-10-04", "football_team_full_game_winner", [("Jets", "nyj"), ("Bears", "chi")]),
          pm("asc-nfl-nyj-chi-2026-10-04-neg-10pt5", "football_team_full_game_spread",
             [("-10.50", "nyj"), ("+10.50", "chi")], line=-10.5),
          pm("tsc-nfl-nyj-chi-2026-10-04-total-16pt5", "football_team_full_game_total",
             [("Over", None), ("Under", None)], line=16.5),
          pm("astatc-nfl-nyj-chi-2026-10-04-td-kylmon-gte2", "football_player_touchdowns",
             [("Yes", "chi"), ("No", "chi")], line=2, title="Kyle Monangai 2+ touchdowns"),
          pm("astatc-nfl-nyj-chi-2026-10-04-firsttd-colkme", "football_player_first_touchdown",
             [("Yes", "chi"), ("No", "chi")], title="Cole Kmet scores the first touchdown"),
          pm("atc-fwc-fra-eng-2026-07-18-draw", "drawable_outcome", [("Yes", None), ("No", None)]),
          pm("aadc-fwc-mex-eng-2026-07-05-to-advance-mex", "soccer_game_to_advance", [("Yes", None), ("No", None)])]
    out = match.pm_outcomes(ms, "nyj", "chi")
    assert out[("ml", "away")] == [("aec-nfl-nyj-chi-2026-10-04", "yes")]
    assert out[("ml", "home")] == [("aec-nfl-nyj-chi-2026-10-04", "no")]
    assert out[("spread", "home", 10.5)] == [("asc-nfl-nyj-chi-2026-10-04-neg-10pt5", "no")]
    assert out[("total", "over", 16.5)] == [("tsc-nfl-nyj-chi-2026-10-04-total-16pt5", "yes")]
    assert ("prop", "Anytime TD", "kyle monangai", 2, "yes") in out
    assert ("prop", "First TD", "cole kmet", None, "no") in out
    assert out[("ml", "draw")] == [("atc-fwc-fra-eng-2026-07-18-draw", "yes")]
    soccer = match.pm_outcomes(ms[-1:], "mex", "eng")
    assert soccer[("advance", "away")] == [("aadc-fwc-mex-eng-2026-07-05-to-advance-mex", "yes")]
    assert soccer[("advance", "home")] == [("aadc-fwc-mex-eng-2026-07-05-to-advance-mex", "no")]


def test_lines_match_exactly_and_pair():
    k = match.kalshi_outcomes([km("KXNFLSPREAD-26OCT04DETCAR-DET5", strike=4.5)], "DETCAR")
    p = match.pm_outcomes([pm("asc-nfl-det-car-2026-10-04-neg-5pt5", "football_team_full_game_spread",
                              [("-5.50", "det"), ("+5.50", "car")], line=-5.5)], "det", "car")
    assert match.pair(k, p) == {}
    p = match.pm_outcomes([pm("asc-nfl-det-car-2026-10-04-neg-4pt5", "football_team_full_game_spread",
                              [("-4.50", "det"), ("+4.50", "car")], line=-4.5)], "det", "car")
    assert set(match.pair(k, p)) == {("spread", "away", -4.5), ("spread", "home", 4.5)}


def test_ambiguous_prop_name_skipped():
    k = match.kalshi_outcomes([km("KXNFLTD-26SEP17DETBUF-BUFJALLEN17-1", "Josh Allen: 1+", 0.5),
                               km("KXNFLTD-26SEP17DETBUF-BUFJALLEN58-1", "Josh Allen: 1+", 0.5)], "DETBUF")
    assert not any(key[0] == "prop" for key in k)


def test_norm_name():
    assert match.norm_name("Kylian Mbappé") == "kylian mbappe"
    assert match.norm_name("Luther Burden III") == "luther burden"
    assert match.norm_name("D'Andre Swift") == "dandre swift"


def test_label():
    names = {"away": "Jets", "home": "Bears"}
    assert match.label(("spread", "home", 10.5), names) == "Bears +10.5"
    assert match.label(("total", "over", 16.5), names) == "Over 16.5"
    assert match.label(("prop", "Anytime TD", "kyle monangai", 2, "yes"), names) == "Kyle Monangai 2+ TD: Yes"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_match.py -v`
Expected: FAIL with `ImportError: cannot import name 'match'`.

- [ ] **Step 3: Implement `sharp_check/match.py`**

```python
"""Phase 8b: cross-venue matching (docs/pnl-rules.md, line shop). Pure: no network, no DB.

Each venue's markets become outcome keys mapped to refs (market id, "yes"|"no" = which side to buy). A key present on
both venues is a pair. Teams map by role: both venues list a game away team first (Kalshi `NYJCHI`, Polymarket
`nyj-chi`), so a team code is "away" or "home" by its place in that game code.
"""
import re
import unicodedata

# Checker leagues: (Polymarket tagSlug, Kalshi milestone competition, Kalshi series prefix).
LEAGUES = {"NFL": ("nfl", "NFL", "KXNFL"), "College football": ("cfb", "NCAAFB", "KXNCAAF"),
           "MLB": ("mlb", "MLB", "KXMLB")}

# Prop registry (NFL only for now): type -> (Kalshi series, Polymarket sportsMarketType). Kalshi "N+" thresholds
# (floor_strike N - 0.5) pair with Polymarket line N. KXNFLANYTD is the 2025 anytime-TD series (no threshold = 1+).
PROPS = {
    "Anytime TD": (("KXNFLTD", "KXNFLANYTD"), "football_player_touchdowns"),
    "First TD": (("KXNFLFIRSTTD",), "football_player_first_touchdown"),
    "Rushing yds": (("KXNFLRSHYDS",), "football_player_rushing_yards"),
    "Receiving yds": (("KXNFLRECYDS",), "football_player_receiving_yards"),
    "Scrimmage yds": (("KXNFLRRYDS",), "football_player_scrimmage_yards"),
}
PROP_BY_SERIES = {s: t for t, (series, _) in PROPS.items() for s in series}
PROP_BY_PM = {smt: t for t, (_, smt) in PROPS.items()}
SHORT = {"Anytime TD": "TD", "First TD": "first TD", "Rushing yds": "rush yds", "Receiving yds": "rec yds",
         "Scrimmage yds": "scrimmage yds"}

GAME_CODE = re.compile(r"^\d{2}[A-Z]{3}\d{2}(?:\d{4})?([A-Z]+)$")
PM_PROP_NAME = re.compile(r"^(.*?) (?:\d+\+ |scores the first)")
OTHER = {"away": "home", "home": "away"}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def norm_name(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return " ".join(w for w in re.sub(r"[^a-z ]", "", s).split() if w not in SUFFIXES)


def teams_from_kalshi_event(event_ticker):
    m = GAME_CODE.match(event_ticker.split("-", 1)[1]) if "-" in event_ticker else None
    return m and m.group(1)


def teams_from_pm_slug(slug):
    parts = slug.split("-")
    return parts[-5], parts[-4]


def kalshi_role(code, teams):
    a, h = teams.startswith(code), teams.endswith(code)
    return "away" if a and not h else "home" if h and not a else None


def _add(out, key, ref):
    out.setdefault(key, [])
    if ref not in out[key]:
        out[key].append(ref)


def _drop_ambiguous_props(out):
    """A prop key fed by two different markets on one venue (two players, same normalized name) is skipped."""
    return {k: v for k, v in out.items() if k[0] != "prop" or len({m for m, _ in v}) == 1}


def kalshi_outcomes(markets, teams):
    out = {}
    three_way = any(m["ticker"].endswith("-TIE") for m in markets)
    for m in markets:
        t = m["ticker"]
        series, suffix = t.split("-")[0], t.rsplit("-", 1)[1]
        if series.endswith("GAME"):
            role = "draw" if suffix == "TIE" else kalshi_role(suffix, teams)
            if role is None:
                continue
            _add(out, ("ml", role), (t, "yes"))
            if three_way:
                _add(out, ("not", "ml", role), (t, "no"))
            elif role in OTHER:
                _add(out, ("ml", OTHER[role]), (t, "no"))
        elif series.endswith("ADVANCE"):
            role = kalshi_role(suffix, teams)
            if role:
                _add(out, ("advance", role), (t, "yes"))
                _add(out, ("advance", OTHER[role]), (t, "no"))
        elif series.endswith("SPREAD") and m.get("floor_strike") is not None:
            role = kalshi_role(re.sub(r"\d+$", "", suffix), teams)
            if role:
                n = float(m["floor_strike"])
                _add(out, ("spread", role, -n), (t, "yes"))
                _add(out, ("spread", OTHER[role], n), (t, "no"))
        elif series.endswith("TOTAL") and m.get("floor_strike") is not None:
            n = float(m["floor_strike"])
            _add(out, ("total", "over", n), (t, "yes"))
            _add(out, ("total", "under", n), (t, "no"))
        elif series in PROP_BY_SERIES:
            kind = PROP_BY_SERIES[series]
            name = norm_name((m.get("yes_sub_title") or "").split(":")[0])
            fs = m.get("floor_strike")
            n = None if kind == "First TD" else 1 if fs is None else round(float(fs) + 0.5)
            if name:
                for side in ("yes", "no"):
                    _add(out, ("prop", kind, name, n, side), (t, side))
    return _drop_ambiguous_props(out)


def pm_role(abbr, away, home):
    return "away" if abbr == away else "home" if abbr == home else None


def pm_outcomes(markets, away, home):
    out = {}
    for m in markets:
        slug, smt = m["slug"], m.get("sportsMarketType") or ""
        sides = m.get("marketSides") or []
        side0 = sides[0] if sides else {}
        yes_no = side0.get("description") == "Yes"
        team0 = (side0.get("team") or {}).get("abbreviation")
        if smt.endswith("_full_game_winner") or smt in ("moneyline", "drawable_outcome"):
            if yes_no or smt == "drawable_outcome":
                last = slug.rsplit("-", 1)[1]
                role = "draw" if last == "draw" else pm_role(last, away, home)
                if role:
                    _add(out, ("ml", role), (slug, "yes"))
                    _add(out, ("not", "ml", role), (slug, "no"))
            elif (role := pm_role(team0, away, home)):
                _add(out, ("ml", role), (slug, "yes"))
                _add(out, ("ml", OTHER[role]), (slug, "no"))
        elif smt == "soccer_game_to_advance":
            role = pm_role(slug.rsplit("-", 1)[1] if yes_no else team0, away, home)
            if role:
                _add(out, ("advance", role), (slug, "yes"))
                _add(out, ("advance", OTHER[role]), (slug, "no"))
        elif smt.endswith("_full_game_spread") and m.get("line") is not None:
            if (role := pm_role(team0, away, home)):
                n = float(m["line"])
                _add(out, ("spread", role, n), (slug, "yes"))
                _add(out, ("spread", OTHER[role], -n), (slug, "no"))
        elif smt.endswith("_full_game_total") and m.get("line") is not None:
            n = float(m["line"])
            _add(out, ("total", "over", n), (slug, "yes"))
            _add(out, ("total", "under", n), (slug, "no"))
        elif smt in PROP_BY_PM:
            kind = PROP_BY_PM[smt]
            hit = PM_PROP_NAME.match(m.get("title") or "")
            n = None if kind == "First TD" else m.get("line") and round(float(m["line"]))
            if hit:
                for side in ("yes", "no"):
                    _add(out, ("prop", kind, norm_name(hit.group(1)), n, side), (slug, side))
    return _drop_ambiguous_props(out)


def pair(k, p):
    return {key: (k[key], p[key]) for key in k.keys() & p.keys()}


def label(key, names):
    kind = key[0]
    if kind == "ml":
        return "Draw" if key[1] == "draw" else f"{names[key[1]]} win"
    if kind == "not":
        return "No draw" if key[2] == "draw" else f"{names[key[2]]} don't win"
    if kind == "advance":
        return f"{names[key[1]]} advance"
    if kind == "spread":
        return f"{names[key[1]]} {key[2]:+g}"
    if kind == "total":
        return f"{key[1].title()} {key[2]:g}"
    _, prop, name, n, side = key
    what = SHORT[prop] if n is None else f"{n}+ {SHORT[prop]}"
    return f"{name.title()} {what}: {side.title()}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_match.py -v`
Expected: all PASS. If a key shape differs from a test (e.g. `round()` returning int vs float), fix the code, not the test.

- [ ] **Step 5: Commit**

```bash
git add sharp_check/match.py tests/test_match.py
git commit -m "Phase 8b: cross-venue outcome keys and pairing"
```

---

### Task 2: Fees and all-in price (`match.py`)

**Files:**
- Modify: `sharp_check/match.py` (append)
- Test: `tests/test_match.py` (append)

**Interfaces:**
- Produces:
  - `ONE = 1_000_000`, `USUAL_STAKE = 10 * ONE`, `KALSHI_THETA = 0.07`, `PM_THETA: tuple[tuple[str, float], ...]`, `SIDE_CHECK = 150_000`, `NO_EDGE = 5_000`.
  - `kalshi_fee(price: int, multiplier: float = 1.0, stake: int = USUAL_STAKE) -> float` — micro-dollars per contract.
  - `pm_fee(price: int, theta: float) -> float` — micro-dollars per contract.
  - `pm_theta_at(ts: str) -> float` — θ in force at an ISO timestamp.
  - `side_ask(bid: int|None, ask: int|None, size_bid, size_ask, side: str) -> tuple[int|None, float|None]` — (ask of that side in micros, contracts available at it).

- [ ] **Step 1: Write the failing tests**

```python
import json
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def test_kalshi_fee_rounds_up_per_order():
    # $10 at 50c = 20 contracts; 0.07 * 20 * .25 = $0.35 exactly -> 1.75c per contract
    assert round(match.kalshi_fee(500_000)) == 17_500
    # $10 at 10c = 100 contracts; 0.07 * 100 * .09 = $0.63 -> 0.63c per contract
    assert round(match.kalshi_fee(100_000)) == 6_300
    # rounding up: $10 at 33c ~ 30.3 contracts; 0.07*30.3*.33*.67 = $0.469 -> $0.47
    assert abs(match.kalshi_fee(330_000) * (10 * match.ONE / 330_000) - 470_000) < 1


def test_kalshi_fee_matches_fixture_fills():
    """Model at each fill's own size reproduces the fee paid within a cent (Kalshi rounds per order)."""
    for f in json.load(open(FIX / "kalshi_fills.json")):
        if not f.get("is_taker"):
            continue
        side = f["outcome_side"]
        p = round(float(f[f"{side}_price_dollars"]) * match.ONE)
        qty = float(f["count_fp"])
        model = match.kalshi_fee(p, stake=qty * p) * qty
        assert abs(model - float(f["fee_cost"]) * match.ONE) <= 10_000


def test_pm_fee_and_schedule():
    assert match.pm_fee(500_000, 0.0695) == 0.0695 * 250_000
    assert match.pm_theta_at("2026-06-22T17:46:04Z") == 0.05
    assert match.pm_theta_at("2026-08-22T00:00:00Z") == 0.06
    assert match.pm_theta_at("2026-10-04T00:00:00Z") == 0.0695


def test_side_ask():
    assert match.side_ask(450_000, 470_000, 100.0, 50.0, "yes") == (470_000, 50.0)
    assert match.side_ask(450_000, 470_000, 100.0, 50.0, "no") == (550_000, 100.0)
    assert match.side_ask(None, 470_000, None, 50.0, "no") == (None, None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_match.py -v`
Expected: the four new tests FAIL with `AttributeError`.

- [ ] **Step 3: Implement (append to `match.py`)**

```python
import math

ONE = 1_000_000
USUAL_STAKE = 10 * ONE    # median single stake (habits, 2026-10-03): the size an all-in price is quoted for
KALSHI_THETA = 0.07       # taker fee coefficient; series fee_multiplier scales it
# Polymarket US taker θ by date, read off your fills (fee / (C·p·(1−p))): ~0.05 to 2026-06-24, ~0.06 from 2026-07-06,
# ~0.0695 from 2026-09-17. Change dates fall between observed fills; before 2026-06-22 there are no fills (assumed 0.05).
PM_THETA = (("", 0.05), ("2026-07-01", 0.06), ("2026-09-15", 0.0695))
SIDE_CHECK = 150_000      # mids 15c apart on one outcome = sides likely flipped: drop the pair
NO_EDGE = 5_000           # a gap under 0.5c isn't an edge


def kalshi_fee(price, multiplier=1.0, stake=USUAL_STAKE):
    """Per contract, micros: Kalshi rounds each order's fee up to the cent."""
    qty = stake / price
    fee = KALSHI_THETA * multiplier * qty * (price / ONE) * (1 - price / ONE)
    return math.ceil(round(fee * 100, 6)) * 10_000 / qty


def pm_fee(price, theta):
    return theta * price * (ONE - price) / ONE


def pm_theta_at(ts):
    return [theta for start, theta in PM_THETA if ts >= start][-1]


def side_ask(bid, ask, size_bid, size_ask, side):
    """Ask and size for buying `side`. Buying NO = selling YES into the bid: NO ask = 1 − YES bid."""
    if side == "yes":
        return (ask, size_ask) if ask is not None else (None, None)
    return (ONE - bid, size_bid) if bid is not None else (None, None)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_match.py -v`
Expected: all PASS. If `test_kalshi_fee_matches_fixture_fills` fails on a partial fill of a larger order (rounding is per order, not per fill), loosen only that fill by skipping fills whose `count_fp` differs from their order's total; note it in a comment.

- [ ] **Step 5: Calibrate against your real fills (not a test; prints counts only)**

```bash
python - <<'EOF'
import sqlite3
from sharp_check import match
c = sqlite3.connect("data/sharp_check.db")
bad = 0
for v, ts, qty, price, fee in c.execute("SELECT venue, ts, qty, price, fee FROM fills WHERE is_taker AND price>0 "
        "AND price<1000000 AND market_id NOT LIKE 'caoc-%' AND market_id NOT LIKE 'KXMVE%'"):
    model = match.kalshi_fee(price, stake=qty * price) * qty if v == "kalshi" else match.pm_fee(price, match.pm_theta_at(ts)) * qty
    bad += fee and abs(model - fee) > max(10_000, 0.05 * fee)
print("fills off by more than 1c and 5%:", bad)
EOF
```

Expected: a small count (fee-free promo fills show as 0-fee). If many Polymarket fills miss, adjust `PM_THETA` dates and note it in the spec.

- [ ] **Step 6: Commit**

```bash
git add sharp_check/match.py tests/test_match.py
git commit -m "Phase 8b: fee model and all-in price"
```

---

### Task 3: Live checker fetch (`lineshop.py`)

**Files:**
- Create: `sharp_check/lineshop.py`
- Test: `tests/test_lineshop.py`

**Interfaces:**
- Consumes: Task 1/2 `match.*`; `clients.kalshi_get`, `clients.gateway_get`.
- Produces:
  - `slate(now: str | None = None, kalshi_get=..., gateway_get=..., refresh=False) -> list[dict]` — games `{"key", "league", "title", "start", "pm_slug", "kalshi_event", "milestone"}` sorted by start; cached 10 min unless `refresh`.
  - `game_rows(game: dict, kalshi_get=..., gateway_get=..., now=None) -> dict` — `{"as_of": iso, "errors": [str], "rows": [row]}`; row = `{"group", "label", "kalshi": cell|None, "polymarket": cell|None, "gap": int|None, "best": "kalshi"|"polymarket"|None}`, cell = `{"all_in": int, "ask": int, "thin": float|None}` (thin = $ available when under the usual stake, else None).
  - `GROUPS: tuple[str, ...]` row group order.

Endpoints (all GET, verified 2026-10-05):
- Polymarket slate: `gateway /v1/events` params `tagSlug`, `marketTypes=moneyline`, `startTimeMin`, `startTimeMax`, `limit=100`, `offset` (page until < 100). Event has `slug`, `startTime`, `title`, `sportradarGameId`, `teams[]` (`abbreviation`, `name`).
- Polymarket game: `gateway /v1/events/slug/{slug}` → `event.markets[]` (`slug`, `sportsMarketType`, `line`, `title`, `marketSides`, `feeCoefficient`).
- Polymarket depth: `gateway /v1/markets/{slug}/book` → `marketData.bids[]` / `marketData.offers[]` of `{px: {value}, qty}` (YES prices; best = highest bid / lowest offer). Confirm the offers key name on the first real call; if it isn't `offers`, use the key that holds asks and fix the docs.
- Kalshi slate: `/milestones` params `competition`, `minimum_start_date`, `maximum_start_date`, `limit=200`, `cursor` (page until no cursor). Milestone has `source_ids`, `start_date`, `title`, `primary_event_tickers`, `related_event_tickers`, `details.main_game_event_ticker`.
- Kalshi markets: `/markets` params `event_ticker`, `limit=1000` → `yes_bid_dollars`, `yes_ask_dollars`, `yes_bid_size_fp`, `yes_ask_size_fp`, `floor_strike`, `yes_sub_title`.
- Kalshi fee: `/series/{series}` → `series.fee_multiplier` (cache per series).

- [ ] **Step 1: Write the failing tests** (fake getters return canned bodies keyed by path)

```python
"""Live line-shop fetch with fake getters. Run: python -m pytest tests/  (no network)."""
from sharp_check import lineshop, match


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.ok = body, status, status == 200

    def json(self):
        return self.body

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


def getter(routes):
    def get(path, params=None):
        key = (path, (params or {}).get("event_ticker") or (params or {}).get("competition")
               or (params or {}).get("tagSlug"))
        body = routes.get(key, routes.get((path, None)))
        if isinstance(body, Exception):
            raise body
        return Resp(body) if body is not None else Resp({}, 404)
    return get


SR = "62a57ae8"
MILESTONE = {"id": "m1", "title": "NY Jets at CHI Bears", "start_date": "2026-10-11T17:00:00Z",
             "source_ids": {"source_3_id": SR}, "details": {"main_game_event_ticker": "KXNFLGAME-26OCT11NYJCHI"},
             "related_event_tickers": ["KXNFLGAME-26OCT11NYJCHI", "KXNFLSPREAD-26OCT11NYJCHI"]}
PM_EVENT = {"slug": "nfl-nyj-chi-2026-10-11", "startTime": "2026-10-11T17:00:00Z", "title": "NY Jets vs. CHI Bears",
            "sportradarGameId": SR, "teams": [{"abbreviation": "nyj", "name": "New York Jets"},
                                              {"abbreviation": "chi", "name": "Chicago Bears"}],
            "markets": [{"slug": "aec-nfl-nyj-chi-2026-10-11", "sportsMarketType": "football_team_full_game_winner",
                         "feeCoefficient": 0.0695, "marketSides": [{"description": "Jets", "team": {"abbreviation": "nyj"}},
                                                                   {"description": "Bears", "team": {"abbreviation": "chi"}}]}]}


def kmarket(ticker, bid, ask, bsz="500.00", asz="500.00"):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "yes_bid_dollars": bid, "yes_ask_dollars": ask,
            "yes_bid_size_fp": bsz, "yes_ask_size_fp": asz, "floor_strike": None, "yes_sub_title": ""}


def routes(pm_book=None, kalshi_markets=None):
    return {
        ("/milestones", "NFL"): {"milestones": [MILESTONE], "cursor": ""},
        ("/milestones", None): {"milestones": [], "cursor": ""},
        ("/v1/events", "nfl"): {"events": [PM_EVENT]},
        ("/v1/events", None): {"events": []},
        ("/v1/events/slug/nfl-nyj-chi-2026-10-11", None): {"event": PM_EVENT},
        ("/v1/markets/aec-nfl-nyj-chi-2026-10-11/book", None): pm_book or {"marketData": {
            "bids": [{"px": {"value": "0.3000"}, "qty": "1000"}], "offers": [{"px": {"value": "0.3200"}, "qty": "1000"}]}},
        ("/markets", "KXNFLGAME-26OCT11NYJCHI"): {"markets": kalshi_markets or [
            kmarket("KXNFLGAME-26OCT11NYJCHI-NYJ", "0.3000", "0.3100"),
            kmarket("KXNFLGAME-26OCT11NYJCHI-CHI", "0.6800", "0.7000")]},
        ("/markets", None): {"markets": []},
        ("/series/KXNFLGAME", None): {"series": {"fee_multiplier": 1}},
        ("/series/KXNFLSPREAD", None): {"series": {"fee_multiplier": 1}},
    }


NOW = "2026-10-10T12:00:00Z"


def test_slate_joins_on_sportradar_id():
    r = routes()
    games = lineshop.slate(NOW, getter(r), getter(r), refresh=True)
    assert [(g["league"], g["pm_slug"], g["kalshi_event"]) for g in games] == [
        ("NFL", "nfl-nyj-chi-2026-10-11", "KXNFLGAME-26OCT11NYJCHI")]


def test_started_games_left_out():
    r = routes()
    assert lineshop.slate("2026-10-11T17:00:01Z", getter(r), getter(r), refresh=True) == []


def test_cheapest_ref_wins():
    """Jets win: Kalshi YES on NYJ asks 0.31, NO on CHI asks 1 - 0.68 = 0.32 -> 0.31 + fee is used."""
    r = routes()
    game = lineshop.slate(NOW, getter(r), getter(r), refresh=True)[0]
    out = lineshop.game_rows(game, getter(r), getter(r), now=NOW)
    jets = next(row for row in out["rows"] if row["label"] == "New York Jets win")
    assert jets["kalshi"]["ask"] == 310_000
    assert jets["kalshi"]["all_in"] == 310_000 + round(match.kalshi_fee(310_000))
    assert jets["polymarket"]["ask"] == 320_000
    pm_all_in = 320_000 + round(match.pm_fee(320_000, 0.0695))
    assert jets["gap"] == jets["kalshi"]["all_in"] - pm_all_in < 0   # Kalshi cheaper (~1c)
    assert jets["best"] == "kalshi"


def test_thin_flag():
    r = routes(pm_book={"marketData": {"bids": [{"px": {"value": "0.3000"}, "qty": "5"}],
                                        "offers": [{"px": {"value": "0.3200"}, "qty": "5"}]}})
    game = lineshop.slate(NOW, getter(r), getter(r), refresh=True)[0]
    jets = next(row for row in lineshop.game_rows(game, getter(r), getter(r), now=NOW)["rows"]
                if row["label"] == "New York Jets win")
    assert jets["polymarket"]["thin"] == 5 * 0.32


def test_side_check_drops_flipped_pair():
    r = routes(kalshi_markets=[kmarket("KXNFLGAME-26OCT11NYJCHI-NYJ", "0.6800", "0.7000"),
                               kmarket("KXNFLGAME-26OCT11NYJCHI-CHI", "0.3000", "0.3100")])
    game = lineshop.slate(NOW, getter(r), getter(r), refresh=True)[0]
    assert lineshop.game_rows(game, getter(r), getter(r), now=NOW)["rows"] == []


def test_one_venue_down():
    r = routes()
    game = lineshop.slate(NOW, getter(r), getter(r), refresh=True)[0]
    r[("/markets", "KXNFLGAME-26OCT11NYJCHI")] = RuntimeError("timeout")
    out = lineshop.game_rows(game, getter(r), getter(r), now=NOW)
    assert out["errors"] == ["Kalshi unavailable"]
    assert out["rows"] and all(row["kalshi"] is None and row["best"] is None for row in out["rows"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_lineshop.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement `sharp_check/lineshop.py`**

```python
"""Phase 8b live line-shop checker: fetch on demand, compare all-in prices, store nothing (docs/pnl-rules.md)."""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sharp_check import clients, match

ONE = match.ONE
SLATE_HOURS = 48
SLATE_TTL = 600  # seconds
GROUPS = ("Moneyline", "Spread", "Total") + tuple(match.PROPS)
GAME_LINES = ("GAME", "SPREAD", "TOTAL")
_slate = {"at": 0.0, "games": []}
_fee_multiplier = {}


def micros(v):
    return None if v is None else round(float(v) * ONE)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _get(get, path, params=None):
    r = get(path, params)
    r.raise_for_status()
    return r.json()


def slate(now=None, kalshi_get=clients.kalshi_get, gateway_get=clients.gateway_get, refresh=False):
    """Games in the checker leagues starting in (now, now + 48 h] that both venues list, joined on Sportradar ID."""
    if not refresh and time.time() - _slate["at"] < SLATE_TTL:
        return _slate["games"]
    now_dt = utc(now) if now else datetime.now(timezone.utc)
    lo, hi = iso(now_dt), iso(now_dt + timedelta(hours=SLATE_HOURS))
    games = []
    for league, (tag, competition, _) in match.LEAGUES.items():
        milestones, cursor = [], None
        while True:
            p = {"competition": competition, "minimum_start_date": lo, "maximum_start_date": hi, "limit": 200}
            if cursor:
                p["cursor"] = cursor
            b = _get(kalshi_get, "/milestones", p)
            milestones += b.get("milestones") or []
            cursor = b.get("cursor")
            if not cursor:
                break
        by_sr = {sid: m for m in milestones for sid in (m.get("source_ids") or {}).values()}
        offset = 0
        while True:
            evs = _get(gateway_get, "/v1/events", {"tagSlug": tag, "marketTypes": "moneyline", "startTimeMin": lo,
                                                   "startTimeMax": hi, "limit": 100, "offset": offset}).get("events") or []
            for e in evs:
                m = by_sr.get(e.get("sportradarGameId"))
                if m and e["startTime"] > lo:  # not started
                    games.append({"key": e["slug"], "league": league, "title": e.get("title") or m["title"],
                                  "start": e["startTime"], "pm_slug": e["slug"],
                                  "kalshi_event": m["details"]["main_game_event_ticker"], "milestone": m})
            if len(evs) < 100:
                break
            offset += 100
    games.sort(key=lambda g: g["start"])
    _slate.update(at=time.time(), games=games)
    return games


def kalshi_events(game):
    """Game-line events from the milestone, plus registry prop events built from the game code (NFL only)."""
    code = game["kalshi_event"].split("-", 1)[1]
    prefix = game["kalshi_event"].split("-")[0][:-len("GAME")]
    events = [f"{prefix}{kind}-{code}" for kind in GAME_LINES]
    if prefix == "KXNFL":
        events += [f"{s}-{code}" for series, _ in match.PROPS.values() for s in series]
    return events


def fee_multiplier(kalshi_get, series):
    if series not in _fee_multiplier:
        try:
            _fee_multiplier[series] = float(_get(kalshi_get, f"/series/{series}")["series"].get("fee_multiplier") or 1)
        except Exception:
            return 1.0
    return _fee_multiplier[series]


def kalshi_side(kalshi_get):
    """Fetch-time pricing for Kalshi refs: (all_in, ask, size, mid) for buying `side` of `market`."""
    def price(m, side):
        bid, ask = micros(m.get("yes_bid_dollars")), micros(m.get("yes_ask_dollars"))
        bid, ask = (bid if bid and bid > 0 else None), (ask if ask and ask < ONE else None)
        a, size = match.side_ask(bid, ask, float(m.get("yes_bid_size_fp") or 0), float(m.get("yes_ask_size_fp") or 0),
                                 side)
        if a is None:
            return None
        mid = None if bid is None or ask is None else (bid + ask) / 2 if side == "yes" else ONE - (bid + ask) / 2
        return a + round(match.kalshi_fee(a, fee_multiplier(kalshi_get, m["ticker"].split("-")[0]))), a, size, mid
    return price


def pm_book(gateway_get, slug):
    md = _get(gateway_get, f"/v1/markets/{slug}/book")["marketData"]
    best = lambda levels, pick: pick(((micros(l["px"]["value"]), float(l["qty"])) for l in levels or []),
                                     key=lambda x: x[0], default=(None, None))
    return best(md.get("bids"), max), best(md.get("offers"), min)


def cell(price):
    if price is None:
        return None
    all_in, ask, size, _ = price
    usd = (size or 0) * ask / ONE
    return {"all_in": all_in, "ask": ask, "thin": usd if usd < match.USUAL_STAKE / ONE else None}


def group(key):
    return {"ml": "Moneyline", "not": "Moneyline", "advance": "Moneyline", "spread": "Spread",
            "total": "Total"}.get(key[0]) or key[1]


def game_rows(game, kalshi_get=clients.kalshi_get, gateway_get=clients.gateway_get, now=None):
    errors, k_markets, pm_event = [], None, None
    try:
        k_markets = [m for e in kalshi_events(game)
                     for m in _get(kalshi_get, "/markets", {"event_ticker": e, "limit": 1000}).get("markets") or []]
    except Exception:
        errors.append("Kalshi unavailable")
    try:
        pm_event = _get(gateway_get, f"/v1/events/slug/{game['pm_slug']}")["event"]
    except Exception:
        errors.append("Polymarket unavailable")
    if pm_event is None and k_markets is None:
        return {"as_of": now or iso(datetime.now(timezone.utc)), "errors": errors, "rows": []}
    away, home = match.teams_from_pm_slug(game["pm_slug"])
    teams = match.teams_from_kalshi_event(game["kalshi_event"])
    k_out = match.kalshi_outcomes(k_markets or [], teams) if k_markets is not None else {}
    p_out = match.pm_outcomes((pm_event or {}).get("markets") or [], away, home)
    names = {"away": away.upper(), "home": home.upper()}
    for t in (pm_event or {}).get("teams") or []:
        role = match.pm_role(t.get("abbreviation"), away, home)
        if role:
            names[role] = t.get("name") or names[role]
    # With one venue down, show the other venue's outcomes for keys the up venue lists.
    keys = (k_out.keys() & p_out.keys()) if k_markets is not None and pm_event is not None else \
        (k_out.keys() if pm_event is None else p_out.keys())
    k_by = {m["ticker"]: m for m in k_markets or []}
    pm_by = {m["slug"]: m for m in (pm_event or {}).get("markets") or []}
    kprice = kalshi_side(kalshi_get)
    books = {}
    if pm_event is not None:
        slugs = sorted({s for key in keys for s, _ in p_out.get(key, [])})
        with ThreadPoolExecutor(max_workers=8) as pool:  # Polymarket allows 25 req/s
            for slug, book in zip(slugs, pool.map(lambda s: _safe(pm_book, gateway_get, s), slugs)):
                books[slug] = book

    def pm_price(slug, side):
        book = books.get(slug)
        if not book:
            return None
        (bid, bsz), (ask, asz) = book
        a, size = match.side_ask(bid, ask, bsz, asz, side)
        if a is None:
            return None
        mid = None if bid is None or ask is None else (bid + ask) / 2 if side == "yes" else ONE - (bid + ask) / 2
        return a + round(match.pm_fee(a, float(pm_by[slug].get("feeCoefficient") or 0))), a, size, mid

    rows = []
    for key in keys:
        k = min(filter(None, (kprice(k_by[t], s) for t, s in k_out.get(key, []))), default=None)
        p = min(filter(None, (pm_price(s, side) for s, side in p_out.get(key, []))), default=None)
        if k and p and k[3] is not None and p[3] is not None and abs(k[3] - p[3]) > match.SIDE_CHECK:
            continue  # sides likely flipped
        gap = k[0] - p[0] if k and p else None
        best = None if gap is None or abs(gap) < match.NO_EDGE else "polymarket" if gap > 0 else "kalshi"
        rows.append({"group": group(key), "label": match.label(key, names), "kalshi": cell(k),
                     "polymarket": cell(p), "gap": gap, "best": best, "key": key})
    rows.sort(key=lambda r: (GROUPS.index(r["group"]), str(r["key"])))
    return {"as_of": now or iso(datetime.now(timezone.utc)), "errors": errors, "rows": rows}


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None
```

Note: `gap` = Kalshi all-in − Polymarket all-in; positive = Polymarket cheaper.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_lineshop.py -v`
Expected: all PASS.

- [ ] **Step 5: Live smoke (read-only, prints labels and public prices only)**

```bash
python - <<'EOF'
from sharp_check import lineshop
games = lineshop.slate(refresh=True)
print(len(games), "games;", [(g["league"], g["title"], g["start"]) for g in games[:5]])
if games:
    nfl = next((g for g in games if g["league"] == "NFL"), games[0])
    out = lineshop.game_rows(nfl)
    print(nfl["title"], out["errors"], len(out["rows"]), "rows")
    for r in out["rows"][:15]:
        print(r["group"], "|", r["label"], "|", r["kalshi"], "|", r["polymarket"], "|", r["gap"], r["best"])
EOF
```

Expected: a non-empty slate, an NFL game with moneyline, spread, total and prop rows, prices within a few cents across venues. If the Polymarket book key isn't `offers`, fix `pm_book` and the docs.

- [ ] **Step 6: Commit**

```bash
git add sharp_check/lineshop.py tests/test_lineshop.py
git commit -m "Phase 8b: live line-shop fetch and all-in comparison"
```

---

### Task 4: Line shop tab

**Files:**
- Modify: `sharp_check/app.py` (new tab, two callbacks), `sharp_check/assets/*.css` (only if the dropdown or highlight needs styling; inspect the DOM first)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `lineshop.slate`, `lineshop.game_rows`, `app.table`, `app.boxed`, `app.cents`.
- Produces: tab value `"lineshop"`, label `"Line shop"`, component ids `ls-game`, `ls-refresh`, `ls-body`; `lineshop_tab(conn)`, `lineshop_body(out: dict) -> list`.

- [ ] **Step 1: Write the failing tests**

```python
def test_line_shop_tab_is_listed_and_renders_without_network():
    conn = archive.connect(":memory:")
    normalize.rebuild(conn)
    page = app.layout(conn)
    tabs = [t for t in page.children if getattr(t, "id", None) == "tabs"][0].children
    assert [t.label for t in tabs] == ["Overview", "CLV", "Performance", "Bets", "Habits", "Line shop", "Data health"]
    assert {"ls-game", "ls-refresh", "ls-body"} <= ids(page)


def test_line_shop_body_marks_the_cheaper_venue_and_thin_books():
    out = {"as_of": "2026-10-10T12:00:00Z", "errors": ["Polymarket unavailable"], "rows": [
        {"group": "Moneyline", "label": "Bears win", "gap": -20_000, "best": "kalshi",
         "kalshi": {"all_in": 700_000, "ask": 690_000, "thin": None},
         "polymarket": {"all_in": 720_000, "ask": 710_000, "thin": 3.2}}]}
    body = app.lineshop_body(out)
    text = str(body)
    assert "Polymarket unavailable" in text and "Bears win" in text and "thin: $3" in text
    assert "best" in text  # the cheaper cell carries the highlight class
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_app.py -v`
Expected: FAIL (tab list and `lineshop_body` missing).

- [ ] **Step 3: Implement in `app.py`**

Add next to the other tab builders:

```python
def lineshop_tab(conn):
    return [
        html.H3("Line shop"),
        html.P("Pregame NFL, college football and MLB games in the next 48 hours that both venues list. All-in = ask + "
               "taker fee per contract at a $10 stake. Live prices, fetched when you pick a game; nothing is stored.",
               className="note"),
        html.Div([dcc.Dropdown(id="ls-game", placeholder="Pick a game", clearable=False, className="ls-game"),
                  html.Button("Refresh", id="ls-refresh", n_clicks=0, className="button")], className="ls-controls"),
        dcc.Loading(html.Div(id="ls-body")),
    ]


def ls_cell(c, best):
    if c is None:
        return "–"
    text = cents(c["all_in"]) + (f" (thin: ${c['thin']:.0f})" if c["thin"] is not None else "")
    return html.Span(text, className="best" if best else None)


def lineshop_body(out):
    parts = [html.P(f"Prices as of {when(out['as_of'])} UTC.", className="sub")]
    parts += [html.P(e, className="note") for e in out["errors"]]
    if not out["rows"]:
        return parts + [html.P("No outcome both venues list.", className="sub")]
    for g in dict.fromkeys(r["group"] for r in out["rows"]):
        rows = [r for r in out["rows"] if r["group"] == g]
        parts += [html.H4(g), table(("Outcome", "Kalshi", "Polymarket", "Gap"), [
            (r["label"], ls_cell(r["kalshi"], r["best"] == "kalshi"), ls_cell(r["polymarket"], r["best"] == "polymarket"),
             "–" if r["gap"] is None else "no clear edge" if r["best"] is None else signed_cents(abs(r["gap"])))
            for r in rows])]
    return parts
```

Add `("Line shop", "lineshop", lineshop_tab)` to `layout`'s tabs before Data health. Add callbacks:

```python
@app.callback(Output("ls-game", "options"), Input("tabs", "value"), Input("ls-refresh", "n_clicks"))
def ls_slate(tab, n):
    if tab != "lineshop":
        return no_update
    try:
        games = lineshop.slate(refresh=ctx.triggered_id == "ls-refresh")
    except Exception:
        return []
    return [{"label": f"{g['league']} · {g['title']} · {when(g['start'])}", "value": g["key"]} for g in games]


@app.callback(Output("ls-body", "children"), Input("ls-game", "value"), Input("ls-refresh", "n_clicks"))
def ls_game(key, n):
    game = next((g for g in lineshop.slate() if g["key"] == key), None) if key else None
    if game is None:
        return html.P("Pick a game.", className="sub")
    return lineshop_body(lineshop.game_rows(game))
```

Imports: `from dash import ctx, no_update` and `from sharp_check import lineshop`. Check how `cents`, `signed_cents` and `when` format (top of `app.py`) and use them as is. Style `.best` (bold plus the theme's positive colour token) in the existing stylesheet under `assets/`. Inspect the real DOM in the browser before styling the dropdown (CLAUDE.md recurring mistake).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/ -v`
Expected: all PASS (update the existing tab-list assertion in `test_empty_db_renders_every_tab_and_the_tiles` to include "Line shop").

- [ ] **Step 5: Commit**

```bash
git add sharp_check/app.py sharp_check/assets tests/test_app.py
git commit -m "Phase 8b: Line shop tab"
```

---

### Task 5: Backward-looking archive and `line_shop` table

**Files:**
- Modify: `sharp_check/archive.py` (`line_shop_counterparts`, `sync_line_shop`, targets in `sync_closes`, `__main__`), `sharp_check/normalize.py` (schema, `DERIVED`, `line_shop_rows`, `rebuild`)
- Test: `tests/test_archive.py`, `tests/test_normalize.py`

**Interfaces:**
- Consumes: `match.*`; existing `archive.records`, `archive.store`, `archive.milestones_by_event`, `archive.kalshi_milestone`, `archive.fill_key`, `archive.epoch`, `normalize.price_index`, `normalize.covered_points`, `normalize.SPREAD_MAX_AGE`.
- Produces:
  - `archive.LINE_SHOP_ELIGIBLE` SQL predicate: `is_parlay = 0 AND link = 'game' AND market_type IN ('moneyline','spread','total','prop')`.
  - `archive.line_shop_counterparts(conn) -> dict[bet_id, tuple[str|None, str|None, list[tuple[str, str]]]]` — bet → (reason, other venue, [(market, side)]); reason None = paired.
  - New `raw_pages` endpoints: Polymarket `"/v1/events"` params `{"sportradarGameId": id}` (full event list response) and `{"slug": slug, "marketTypes": "moneyline"}` (light event, for its `sportradarGameId`); Kalshi `"/markets"` params `{"event_ticker": e}` (market list response); Kalshi `"/milestones"` (existing shape) for Polymarket bets' games.
  - Table `line_shop(venue, bet_id, other_venue, other_market, other_side, is_live, qty, yours, theirs, their_ask, their_fee, gap, note)`; money in micros; `gap = yours − theirs` (positive = the other venue was cheaper).

Reasons (exact strings): `game not on other venue`, `no matching line`, `prop type not covered`, `prop not listed`, `no quote`, `stale quote`, `one-sided quote`, `side check failed`.

Archive flow (`sync_line_shop(conn, kalshi_get, gateway_get, now)`), once per game, only for games that have started (their markets are final), never re-fetched once a page exists:
1. Kalshi bet → milestone from `milestones_by_event` → for each `source_ids` value, `GET gateway /v1/events {"sportradarGameId": v, "limit": 1}`; store the first non-empty response; if none is non-empty, store the last empty response (so the game isn't re-queried).
2. Polymarket bet → event slug from `game_id` (`polymarket:<slug>`) → `GET /v1/events {"slug": slug, "marketTypes": "moneyline", "limit": 1}` → `sportradarGameId` → Kalshi milestone: page `/milestones` over `[start − 3 h, start + 3 h]` (no competition filter: past leagues vary), find one whose `source_ids` contain the ID, then `GET /milestones {"related_event_ticker": <details.main_game_event_ticker>, "limit": 5}` and store that raw response (the shape `milestones_by_event` already reads). If no milestone matches, store `{"milestones": [], "sportradarGameId": id}` under `/milestones` params `{"sportradarGameId": id}` so the game isn't re-queried.
3. Polymarket bet with a Kalshi milestone → Kalshi events for its market type (moneyline: `*GAME`, `*ADVANCE`, `*TIE`-bearing game event; spread: `*SPREAD`; total: `*TOTAL`; prop: registry series events `"{series}-{code}"`), `GET /markets {"event_ticker": e, "limit": 1000}` (fall back to `/historical/markets` if the live tier returns none); store the response with the non-empty tier.

`line_shop_counterparts`: own key from the bet's own market record (`kalshi_outcomes([own], teams)` / `pm_outcomes([own], away, home)`, keyed by the bet's `outcome`), other venue's outcomes from the stored pages, refs = other venue's refs for the same key. Reasons per the list; a prop whose type isn't in the registry is `prop type not covered`.

`sync_closes`: add `(other_venue, market, fill_key(other_venue, first_fill_ts))` for each paired bet's refs.

`normalize.line_shop_rows(conn)`: for each eligible single with a counterpart, at the first fill's key the latest point of each ref (via `price_index`/`covered_points`) decides: no point → `no quote`, older than 5 min → `stale quote`, not two-sided → `one-sided quote`; `their_ask = match.side_ask(...)`; fee: Kalshi `match.kalshi_fee(ask, stake=bet stake)`, Polymarket `match.pm_fee(ask, match.pm_theta_at(first fill ts))`; cheapest priced ref wins; side check vs `fill_costs.mid_before` of the bet's first entry fill (your side's mid) → `side check failed` when the other venue's mid differs by more than `SIDE_CHECK`. `yours = stake / entry_qty`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_normalize.py`, build a fake DB the way existing 8a tests do (raw pages for one Polymarket single on an NFL moneyline, its stored `/v1/market/slug/{slug}` record, a `/milestones` page, a `/markets` page with the Kalshi game markets, and a Kalshi candlestick page covering the fill key). Tests:

```python
def test_line_shop_prices_a_polymarket_single_against_kalshi(conn_with_pm_single):
    conn = conn_with_pm_single  # fixture: PM BUY_LONG Jets at 0.32 (fee 0.0695·p(1−p)), Kalshi NYJ 0.30/0.31
    normalize.rebuild(conn)
    (row,) = conn.execute("SELECT other_venue, other_market, other_side, their_ask, gap, note FROM line_shop").fetchall()
    assert row[:4] == ("kalshi", "KXNFLGAME-26OCT11NYJCHI-NYJ", "yes", 310_000)
    assert row[4] > 0 and row[5] is None  # Kalshi was cheaper: positive gap = money left


def test_latest_point_decides(conn_with_pm_single):
    """A one-sided latest candle leaves the bet unpriced even though an older two-sided candle exists."""
    ...  # append a newer candle with yes_ask 1.00 / no bid at the key; expect note == "one-sided quote", gap NULL


def test_reasons(conn_with_pm_single):
    ...  # remove the /markets page -> "no matching line"; empty /milestones page -> "game not on other venue";
         # a prop with sportsMarketType baseball_player_home_runs -> "prop type not covered"
```

Write the fixture with real-shaped, synthetic bodies (copy shapes from `tests/test_lineshop.py`, `tests/fixtures/pm_trades.json`, `tests/fixtures/kalshi_candles_live.json`). In `tests/test_archive.py`, test `sync_line_shop` with fake getters: stores one `/v1/events` page per game, never re-fetches a stored game, stores the empty response when no venue match exists, and `sync_closes` gains the counterpart target at `fill_key("kalshi", first fill ts)`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_normalize.py tests/test_archive.py -v`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement** (`archive.py` and `normalize.py` per the flow above; keep functions in the style of `parlay_clv_legs` / `spread_targets` / `spread_costs`; add `"line_shop"` to `DERIVED`, its `CREATE TABLE` to `SCHEMA`, and call `line_shop_rows` at the end of `rebuild` after `spread_costs`. In `__main__`, call `sync_line_shop(conn)` after the first `normalize.rebuild` and before `sync_closes`.)

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 5: Run on the real DB**

```bash
python -m sharp_check.archive && python -m sharp_check.reconcile
sqlite3 data/sharp_check.db "SELECT venue, is_live, COALESCE(note,'priced'), COUNT(*) FROM line_shop GROUP BY 1,2,3"
```

Expected: about 22 pregame and 15 in-play priced (spec findings); reconcile still prints OK for both venues. Investigate any `side check failed` row by hand (the spike mapped France-to-advance to a tie market once).

- [ ] **Step 6: Commit**

```bash
git add sharp_check/archive.py sharp_check/normalize.py tests/test_archive.py tests/test_normalize.py
git commit -m "Phase 8b: archive other-venue markets for past singles; line_shop table"
```

---

### Task 6: Habits section and Data health

**Files:**
- Modify: `sharp_check/habits.py` (`line_shop(conn)`), `sharp_check/app.py` (`line_shop_section`, Habits placement after the spread section, Data health list)
- Test: `tests/test_habits.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: `line_shop` table, `metrics.mean_ci`, `habits.median`, `metrics.labels`.
- Produces: `habits.line_shop(conn) -> {"rows": [(venue, priced, eligible, left_usd, mean_c, ci, cheaper, n)], "live": [(venue, median_c, n)], "bets": [dict], "unmatched": [(bet_id, note)]}`.

Rules: pregame rows per your venue: `priced` x of `eligible`; `left_usd` = Σ max(gap, 0) × qty; `mean_c` = mean gap in ¢ with 95% CI (`metrics.mean_ci`); `cheaper` = count gap ≥ `NO_EDGE`. In-play: median gap per your venue, out of the totals. `unmatched` = every eligible single with a note.

- [ ] **Step 1: Write the failing tests** (fake `line_shop` rows inserted after `normalize.rebuild` on an empty DB)

```python
def test_line_shop_summary():
    conn = archive.connect(":memory:")
    normalize.rebuild(conn)
    rows = [("polymarket", "polymarket:a:1", "kalshi", "K-A", "yes", 0, 10.0, 330_000, 320_000, 310_000, 10_000, 10_000, None),
            ("polymarket", "polymarket:b:1", "kalshi", "K-B", "no", 0, 20.0, 500_000, 510_000, 500_000, 10_000, -10_000, None),
            ("polymarket", "polymarket:c:1", "kalshi", None, None, 1, 5.0, 400_000, 380_000, 370_000, 10_000, 20_000, None),
            ("kalshi", "kalshi:d:1", "polymarket", None, None, 0, 1.0, 500_000, None, None, None, None, "no quote")]
    conn.executemany("INSERT INTO line_shop VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    s = habits.line_shop(conn)
    pm = next(r for r in s["rows"] if r[0] == "polymarket")
    assert pm[1:4] == (2, 2, 100_000)       # 2 of 2 pregame priced; $0.10 left (10 contracts x 1c)
    assert pm[6] == 1                        # Kalshi cheaper on 1
    assert s["live"] == [("polymarket", 2.0, 1)]
    assert s["unmatched"] == [("kalshi:d:1", "no quote")]
```

And in `tests/test_app.py`: the Habits tab renders a "Line shopping" heading on an empty DB without error.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_habits.py tests/test_app.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement** `habits.line_shop` and `app.line_shop_section` (table: Venue · Priced · Left on the table · Mean gap (CI) · Other venue cheaper; in-play row "median ±x.x¢"; a note explaining the rule; a fold with every bet: label, yours, theirs, gap, note). Data health: "Singles without a line-shop price" table (bet label, reason).

- [ ] **Step 4: Run all tests**

Run: `python -m pytest tests/ -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add sharp_check/habits.py sharp_check/app.py tests/test_habits.py tests/test_app.py
git commit -m "Phase 8b: line shopping on Habits, unmatched singles on Data health"
```

---

### Task 7: Docs, browser check, exit check

**Files:**
- Modify: `docs/data-model.md`, `docs/pnl-rules.md`, `docs/kalshi.md`, `docs/polymarket-us.md`, `docs/roadmap.md`, `docs/superpowers/specs/2026-10-05-phase8b-line-shop-design.md` (only where the build changed a decision)

- [ ] **Step 1: Update docs**
  - data-model: `line_shop` table; new archived endpoints (`/v1/events` by `sportradarGameId` / `slug`; Kalshi `/markets` by `event_ticker`; `/milestones` empty-marker page).
  - pnl-rules: "Line shop (phase 8b)" rule: all-in, fee model, `PM_THETA`, quote rule, side check, gap sign, pregame totals vs in-play medians, reasons.
  - kalshi.md: `competition` milestone filter; market list sizes (`yes_ask_size_fp`/`yes_bid_size_fp`); series `fee_multiplier`; NFL prop series (`KXNFLTD` ladder, `KXNFLANYTD` 2025, `KXNFLRRYDS` = rush + rec).
  - polymarket-us.md: `/v1/events` filters (`tagSlug`, `marketTypes`, `startTimeMin/Max`, `sportradarGameId`, and that `sportsMarketTypes` returns nothing); events embed all markets (28 MB per 100); `/v1/markets/{slug}/book` and `/bbo`; market `feeCoefficient`; `marketType` values; prop title pattern; away-first slugs.
  - roadmap: current phase, 8b results, resume note.
- [ ] **Step 2: Browser check** — run `python -m sharp_check.app`, open the Line shop tab in headless Chrome at desktop and phone width (as in phase 7): pick an NFL game, confirm rows render, the cheaper venue is highlighted, no horizontal page scroll; inspect the dropdown DOM before styling. Check Habits and Data health render.
- [ ] **Step 3: Exit check** — Line shop shows matched outcomes with all-in prices for a live NFL game on both venues; Habits shows past singles vs the other venue with priced x of y; Data health lists every unmatched single with a reason. Record the numbers in the roadmap.
- [ ] **Step 4: Full test run** — `python -m pytest tests/` → all pass.
- [ ] **Step 5: Commit**

```bash
git add docs
git commit -m "Phase 8b done: docs, exit check"
```

- [ ] **Step 6: Full-branch review** — a fresh read-only subagent reviews every 8b commit (logic, side mapping, fee math, quote rule, read-only and secrets rules) against the spec; fix the findings, re-run tests, commit.
