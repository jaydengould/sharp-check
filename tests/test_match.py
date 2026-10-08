"""Cross-venue matching. Run: python -m pytest tests/  (no network)."""
import json
from pathlib import Path

from sharp_check import match

FIX = Path(__file__).parent / "fixtures"


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


def test_three_way_can_be_forced_for_a_lone_market():
    out = match.kalshi_outcomes([km("KXWCGAME-26JUL18FRAENG-FRA")], "FRAENG", three_way=True)
    assert out[("not", "ml", "away")] == [("KXWCGAME-26JUL18FRAENG-FRA", "no")]
    assert ("ml", "home") not in out


def test_soccer_per_team_winner_and_bare_prop_titles():
    out = match.pm_outcomes([
        pm("atc-fwc-fra-eng-2026-07-18-fra", "soccer_team_full_time_winner", [("Yes", "fra"), ("No", "fra")]),
        pm("astatc-nfl-dal-nyg-2026-09-13-td-geopic-gte1", "football_player_touchdowns", [("Yes", None), ("No", None)],
           line=1, title="George Pickens")], "fra", "eng")
    assert out[("ml", "away")] == [("atc-fwc-fra-eng-2026-07-18-fra", "yes")]
    assert out[("not", "ml", "away")] == [("atc-fwc-fra-eng-2026-07-18-fra", "no")]
    assert ("prop", "Anytime TD", "george pickens", 1, "yes") in out


def test_orientation_from_team_codes():
    assert match.orientation("NYJCHI", "nyj", "chi") == "same"
    assert match.orientation("PHIJAC", "phi", "jax") == "same"        # one code agrees
    assert match.orientation("INDOSU", "ohiost", "ind") == "swapped"  # neutral site, listed the other way round
    assert match.orientation("ABCXYZ", "foo", "bar") is None
    assert match.orientation(None, "foo", "bar") is None


def test_unparseable_kalshi_game_code_doesnt_crash():
    assert match.kalshi_outcomes([km("KXNFLGAME-26OCT04NYJCHI-NYJ")], None) == {}


def test_kalshi_multiplier_follows_fee_change_history():
    changes = {("KXMLBGAME", "2025-10-04T07:00:00Z", 1), ("KXMLBGAME", "2026-08-07T04:59:45.131Z", 0.5),
               ("KXNFLGAME", "2026-01-01T00:00:00Z", 1)}
    assert match.kalshi_multiplier_at(changes, "KXMLBGAME", "2026-07-17T20:35:07.236017Z") == 1.0
    assert match.kalshi_multiplier_at(changes, "KXMLBGAME", "2026-09-27T17:52:20.724243Z") == 0.5
    assert match.kalshi_multiplier_at(changes, "KXNHLGAME", "2026-09-27T00:00:00Z") == 1.0  # no history: 1
    assert match.kalshi_fee(500_000, 0.5) < match.kalshi_fee(500_000, 1.0)
