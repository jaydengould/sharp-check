"""Phase 3: league, sport and market type from Kalshi tickers and Polymarket slugs. Never from titles."""
import re

GAME_CODE = re.compile(r"\d{2}[A-Z]{3}\d{2}")  # 26SEP24ATLGB: yymmmdd, then teams. Futures have 27, MAST26, ...

# Kalshi series = "KX" + league + kind, e.g. KXNFLSPREAD.
KALSHI_LEAGUES = {"NCAAMB": "basketball", "NCAAWB": "basketball", "NCAAF": "football", "NFL": "football", "NBA": "basketball",
                  "MLB": "baseball", "NHL": "hockey", "WC": "soccer", "PGA": "golf", "UFC": "mma"}
KALSHI_KINDS = {"GAME": "moneyline", "ADVANCE": "moneyline", "FIGHT": "moneyline", "SPREAD": "spread", "TOTAL": "total",
                **dict.fromkeys(("ANYTD", "FIRSTTD", "TD", "REC", "PASSYDS", "RECYDS", "RSHYDS", "RRYDS", "KS", "PTS",
                                 "GOAL", "FIRSTGOAL", "BTTS",
                                 # MLB per-game player props (series list checked 2026-10-08)
                                 "HR", "HIT", "TB", "HRR", "RBI", "SB", "WALK", "WA", "HA", "ERA", "OUTS"), "prop")}
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
    return f"KX{league}{'FIGHT' if league == 'UFC' else 'GAME'}-{event_ticker.split('-')[1]}"


def pm_league(slug):
    parts = slug.split("-")
    key = parts[2] if parts[1] == "f" else parts[1]  # futures: tec-f-wc-...
    return PM_LEAGUES.get(key, (key.upper(), "other"))


def pm_type(smt):
    smt = smt or ""
    if "_player_" in smt or smt.endswith("_game_btts"):  # BTTS is a prop on Kalshi too
        return "prop"
    if smt == "drawable_outcome" or smt.endswith(("_full_game_winner", "_full_time_winner", "_to_advance")):
        return "moneyline"
    if smt.endswith("_full_game_spread"):
        return "spread"
    if smt.endswith(("_team_full_game_total", "_game_full_game_total")):  # team_points_full_game_total = team total
        return "total"
    return "other"


# Decided `other` (data-model.md): exact score, team totals, period lines. normalize reports any other `other` type.
PM_OTHER = ("_exact_score", "_team_points_full_game_total", "_team_total_", "_half_")


def pm_expected_other(smt):
    return any(k in (smt or "") for k in PM_OTHER)


def pm_market(slug, sports_market_type, start):
    if slug.startswith("caoc-"):
        return None, None, "parlay"
    league, sport = pm_league(slug)
    if slug.split("-")[1] == "f" or start is None:
        return league, sport, "future"
    return league, sport, pm_type(sports_market_type)


# Novig `marketType` (GET /v3/types/markets). `*_PROTECTED` variants classify like their base type.
NOVIG_TYPES = {
    **dict.fromkeys(("MONEY", "1X2", "MONEYLINE_3_WAY_WIN", "MONEYLINE_3_WAY_DRAW", "TO_ADVANCE"), "moneyline"),
    "SPREAD": "spread", "TOTAL": "total",
    **dict.fromkeys(("WINNER", "TOP_FIVE_FINISH", "TOP_TEN_FINISH", "TOP_TWENTY_FINISH", "TO_MAKE_THE_CUT",
                     "END_OF_ROUND_1_LEADER", "END_OF_ROUND_2_LEADER", "END_OF_ROUND_3_LEADER"), "future"),
    **dict.fromkeys((
        "ASSISTS", "SHOTS_ON_TARGET", "GOALS_ASSISTS", "RECEIVING_YARDS", "PLAYER_GOALS", "RECEPTIONS", "RUSHING_YARDS",
        "SHOTS_ON_GOAL", "FIRST_GOAL_SCORER", "POINTS", "SAVES", "PASSING_YARDS", "POWER_PLAY_POINTS", "TOUCHDOWNS",
        "REBOUNDS", "REBOUNDS_ASSISTS", "POINTS_REBOUNDS", "POINTS_ASSISTS", "POINTS_REBOUNDS_ASSISTS",
        "PASSING_TOUCHDOWNS", "PASSING_AND_RUSHING_YARDS", "KICKING_POINTS", "INTERCEPTIONS_THROWN", "TACKLES_ASSISTS",
        "FIELD_GOALS_MADE", "RUSHING_ATTEMPTS", "PASSING_COMPLETIONS", "PASSING_ATTEMPTS", "LONGEST_RECEPTION",
        "LONGEST_RUSH", "LONGEST_COMPLETION", "RUSHING_AND_RECEIVING_YARDS", "ANYTIME_TOUCHDOWN_SCORER",
        "FIRST_TOUCHDOWN_SCORER"), "prop"),
}
# Decided `other`, like PM_OTHER: team totals, period lines, custom markets. normalize reports any other `other` type.
NOVIG_OTHER = ("TEAM_TOTAL", "_1H", "CUSTOM")


def novig_type(market_type):
    return NOVIG_TYPES.get(market_type.removesuffix("_PROTECTED"), "other")


def novig_expected_other(market_type):
    return any(k in market_type for k in NOVIG_OTHER)
