"""Market classification from tickers and slugs. Run: python -m pytest tests/  (no network)."""
from sharp_check import classify


def test_kalshi_market_types():
    cases = {
        "KXNFLGAME-26JAN18LACHI-CHI": ("NFL", "football", "moneyline"),
        "KXNFLSPREAD-26SEP24ATLGB-GB6": ("NFL", "football", "spread"),
        "KXNHLTOTAL-25NOV29SJVGK-6": ("NHL", "hockey", "total"),
        "KXNFLTD-26SEP24ATLGB-ATLBROBINSON7": ("NFL", "football", "prop"),
        "KXMLBKS-26JUL011310TEXCLE-CLEJCANTILLO54": ("MLB", "baseball", "prop"),
        "KXMLBHR-26OCT081910NYYTOR-NYYAJUDGE99-1": ("MLB", "baseball", "prop"),
        "KXMLBTB-26OCT081910NYYTOR-NYYAJUDGE99-2": ("MLB", "baseball", "prop"),
        "KXMLBTEAMTOTAL-26OCT081910NYYTOR-NYY4": ("MLB", "baseball", "other"),  # team total stays other
        "KXWCADVANCE-26JUL01USABIH-USA": ("WC", "soccer", "moneyline"),
        "KXNCAAMBGAME-25NOV24ASUTEX-ASU": ("NCAAMB", "basketball", "moneyline"),
        "KXNFLNFCCHAMP-27-CHI": ("NFL", "football", "future"),
        "KXPGATOUR-MAST26-SCHEFFLER": ("PGA", "golf", "future"),
        "KXOSCARPIC-26-X": (None, "other", "future"),
        "KXNFLWEIRD-26SEP24ATLGB-X": ("NFL", "football", "other"),  # unknown kind on a game
        "KXMVESPORTSMULTIGAMEEXTENDED-S2026-ABC": (None, None, "parlay"),
        "KXNCAAWBGAME-26APR03TEXUCLA-TEX": ("NCAAWB", "basketball", "moneyline"),
        "KXUFCFIGHT-26JUL11MCGHOL-HOL": ("UFC", "mma", "moneyline"),
    }
    for ticker, want in cases.items():
        assert classify.kalshi_market(ticker) == want, ticker


def test_game_event_maps_prop_event_to_game_event():
    assert classify.game_event("KXNFLTD-26SEP24ATLGB") == "KXNFLGAME-26SEP24ATLGB"
    assert classify.game_event("KXMLBKS-26JUL011310TEXCLE") == "KXMLBGAME-26JUL011310TEXCLE"
    assert classify.game_event("KXUFCKO-26JUL11MCGHOL") == "KXUFCFIGHT-26JUL11MCGHOL"
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
        ("atc-fwc-jor-alg-2026-06-22-draw", "drawable_outcome"): ("WC", "soccer", "moneyline"),  # 3-way draw side
        ("astatc-fwc-esp-arg-2026-07-19-btts", "soccer_game_btts"): ("WC", "soccer", "prop"),
        ("atc-fwc-jor-alg-2026-06-22-exact-score-1-1", "soccer_game_exact_score"): ("WC", "soccer", "other"),
    }
    for (slug, smt), want in cases.items():
        assert classify.pm_market(slug, smt, start) == want, slug
    assert classify.pm_market("tec-f-wc-2026-07-19-winner-fra", None, None) == ("WC", "soccer", "future")
    assert classify.pm_market("aec-nfl-gb-min-2026-09-13", "football_team_full_game_winner", None)[2] == "future"


def test_pm_expected_other_covers_decided_types_only():
    for smt in ("soccer_game_exact_score", "soccer_team_total_goals", "football_team_points_full_game_total",
                "football_team_first_half_spread"):
        assert classify.pm_expected_other(smt), smt
    for smt in ("soccer_game_corners", "baseball_team_full_game_total", None):
        assert not classify.pm_expected_other(smt), smt
