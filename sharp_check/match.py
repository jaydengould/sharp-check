"""Phase 8b: cross-venue matching (docs/pnl-rules.md, line shop). Pure: no network, no DB.

Each venue's markets become outcome keys mapped to refs (market id, "yes"|"no" = which side to buy). A key present on
both venues is a pair. Teams map by role: both venues list a game away team first (Kalshi `NYJCHI`, Polymarket
`nyj-chi`), so a team code is "away" or "home" by its place in that game code.
"""
import math
import re
import unicodedata
from datetime import datetime

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
    if not teams:
        return None
    a, h = teams.startswith(code), teams.endswith(code)
    return "away" if a and not h else "home" if h and not a else None


def orientation(teams, away, home):
    """How Kalshi's game code orders Polymarket's (away, home): "same", "swapped" (some neutral-site games, e.g.
    Kalshi INDOSU vs Polymarket ohiost-ind), or None when neither code matches either end."""
    if not teams:
        return None
    a, h = away.upper(), home.upper()
    same = teams.startswith(a) or teams.endswith(h)
    swapped = teams.startswith(h) or teams.endswith(a)
    return "same" if same and not swapped else "swapped" if swapped and not same else None


ROLE_KINDS = ("ml", "not", "advance", "spread")  # keys that name a team; totals and props don't


def _add(out, key, ref):
    out.setdefault(key, [])
    if ref not in out[key]:
        out[key].append(ref)


def _drop_ambiguous_props(out):
    """A prop key fed by two different markets on one venue (two players, same normalized name) is skipped."""
    return {k: v for k, v in out.items() if k[0] != "prop" or len({m for m, _ in v}) == 1}


def kalshi_outcomes(markets, teams, three_way=None):
    """three_way: None = detect from a TIE market in the list (pass it for a lone market of a soccer game)."""
    out = {}
    if three_way is None:
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
        if smt.endswith(("_full_game_winner", "_full_time_winner")) or smt in ("moneyline", "drawable_outcome"):
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
            title = m.get("title") or ""
            hit = PM_PROP_NAME.match(title)  # live titles: "Kyle Monangai 1+ touchdowns"; archived ones: the name only
            name = norm_name(hit.group(1) if hit else title)
            n = None if kind == "First TD" else m.get("line") and round(float(m["line"]))
            if name:
                for side in ("yes", "no"):
                    _add(out, ("prop", kind, name, n, side), (slug, side))
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


def kalshi_multiplier_at(changes, series, ts):
    """A series' fee multiplier at ts from Kalshi's fee-change history: the latest change scheduled at or before it,
    else 1 (MLB went 1 -> 0.5 on 2026-08-07). changes: (series, scheduled_ts, multiplier)."""
    past = [(epoch_s(t), m) for s, t, m in changes if s == series and epoch_s(t) <= epoch_s(ts)]
    return float(max(past)[1]) if past else 1.0


def epoch_s(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def side_ask(bid, ask, size_bid, size_ask, side):
    """Ask and size for buying `side`. Buying NO = selling YES into the bid: NO ask = 1 − YES bid."""
    if side == "yes":
        return (ask, size_ask) if ask is not None else (None, None)
    return (ONE - bid, size_bid) if bid is not None else (None, None)
