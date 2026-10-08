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


def getter(routes, calls=None):
    def get(path, params=None):
        if calls is not None:
            calls.append(path)
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
                         "feeCoefficient": 0.0695, "bestBidQuote": {"value": "0.3000"},
                         "bestAskQuote": {"value": "0.3200"}, "marketSides": [{"description": "Jets", "team": {"abbreviation": "nyj"}},
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


def test_thin_flag_checks_the_book_only_where_polymarket_is_cheaper():
    """Polymarket prices come from the event page; its book (sizes) is fetched only for rows it wins."""
    r = routes(pm_book={"marketData": {"bids": [{"px": {"value": "0.3000"}, "qty": "5"}],
                                        "offers": [{"px": {"value": "0.3200"}, "qty": "5"}]}},
               kalshi_markets=[kmarket("KXNFLGAME-26OCT11NYJCHI-NYJ", "0.3300", "0.3500"),
                               kmarket("KXNFLGAME-26OCT11NYJCHI-CHI", "0.6500", "0.6700")])
    game = lineshop.slate(NOW, getter(r), getter(r), refresh=True)[0]
    calls = []
    rows = lineshop.game_rows(game, getter(r), getter(r, calls), now=NOW)["rows"]
    jets = next(row for row in rows if row["label"] == "New York Jets win")
    assert jets["best"] == "polymarket" and jets["polymarket"]["thin"] == 5 * 0.32
    assert calls.count("/v1/markets/aec-nfl-nyj-chi-2026-10-11/book") == 1


def test_no_book_call_when_kalshi_is_cheaper():
    r = routes()
    game = lineshop.slate(NOW, getter(r), getter(r), refresh=True)[0]
    calls = []
    lineshop.game_rows(game, getter(r), getter(r, calls), now=NOW)
    assert not any(c.endswith("/book") for c in calls)


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


def test_cached_slate_drops_games_that_have_started():
    r = routes()
    lineshop.slate(NOW, getter(r), getter(r), refresh=True)
    assert lineshop.slate("2026-10-11T17:04:00Z", getter({}), getter({})) == []


def test_swapped_team_order_is_reoriented():
    """Kalshi lists CHI first for this (neutral-site) game; Polymarket's slug has nyj first."""
    swapped = {**MILESTONE, "details": {"main_game_event_ticker": "KXNFLGAME-26OCT11CHINYJ"}}
    r = routes(kalshi_markets=[kmarket("KXNFLGAME-26OCT11CHINYJ-NYJ", "0.3000", "0.3100"),
                               kmarket("KXNFLGAME-26OCT11CHINYJ-CHI", "0.6800", "0.7000")])
    r[("/milestones", "NFL")] = {"milestones": [swapped], "cursor": ""}
    r[("/markets", "KXNFLGAME-26OCT11CHINYJ")] = r.pop(("/markets", "KXNFLGAME-26OCT11NYJCHI"))
    game = lineshop.slate(NOW, getter(r), getter(r), refresh=True)[0]
    jets = next(row for row in lineshop.game_rows(game, getter(r), getter(r), now=NOW)["rows"]
                if row["label"] == "New York Jets win")
    assert jets["kalshi"]["ask"] == 310_000


def test_unknown_order_is_oriented_by_moneyline_prices():
    """Neither code matches either end: pick the orientation whose moneyline mids agree."""
    odd = {**MILESTONE, "details": {"main_game_event_ticker": "KXNFLGAME-26OCT11BRSJTS"}}
    r = routes(kalshi_markets=[kmarket("KXNFLGAME-26OCT11BRSJTS-JTS", "0.3000", "0.3100"),
                               kmarket("KXNFLGAME-26OCT11BRSJTS-BRS", "0.6800", "0.7000")])
    r[("/milestones", "NFL")] = {"milestones": [odd], "cursor": ""}
    r[("/markets", "KXNFLGAME-26OCT11BRSJTS")] = r.pop(("/markets", "KXNFLGAME-26OCT11NYJCHI"))
    game = lineshop.slate(NOW, getter(r), getter(r), refresh=True)[0]
    jets = next(row for row in lineshop.game_rows(game, getter(r), getter(r), now=NOW)["rows"]
                if row["label"] == "New York Jets win")
    assert jets["kalshi"]["ask"] == 310_000


def test_refresh_reloads_the_slate_only_without_a_game():
    assert lineshop.refresh_slate("ls-refresh", None) is True
    assert lineshop.refresh_slate("ls-refresh", "nfl-nyj-chi-2026-10-11") is False
    assert lineshop.refresh_slate("tabs", None) is False


def test_missing_pm_fee_coefficient_is_not_fee_free():
    assert lineshop.live_theta({"feeCoefficient": 0.05}) == 0.05
    assert lineshop.live_theta({"feeCoefficient": 0}) == 0
    assert lineshop.live_theta({}) == match.PM_THETA[-1][1]
