"""Backward-looking line shop: archive the other venue, price each past single. Run: python -m pytest tests/  (no network)."""
from sharp_check import archive, match, normalize

SR, SR2 = "62a57ae8", "9f00aa11"
PM_SLUG, PM_EVENT = "aec-nfl-nyj-chi-2026-10-11", "nfl-nyj-chi-2026-10-11"
OPENED = "2026-10-11T16:00:30.123Z"
KEY_K = archive.fill_key("kalshi", OPENED)
MILESTONE = {"id": "m1", "title": "NY Jets at CHI Bears", "start_date": "2026-10-11T17:00:00Z",
             "source_ids": {"source_3_id": SR}, "details": {"main_game_event_ticker": "KXNFLGAME-26OCT11NYJCHI"},
             "related_event_tickers": ["KXNFLGAME-26OCT11NYJCHI", "KXNFLSPREAD-26OCT11NYJCHI"]}


def db():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    return conn


def bet(conn, bet_id, venue, market, outcome, game_id, mtype, stake, qty, opened=OPENED, is_live=0,
        start="2026-10-11T17:00:00.000000Z", sport="football"):
    conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, game_id, opened_ts, is_live, entry_qty,
                    avg_entry, stake, exit_qty, exit_proceeds, exit_fees, payout, status, start_time, link, sport,
                    market_type) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, 0, ?, 0, 0, 0, 0, 'settled', ?, 'game', ?, ?)""",
                 (bet_id, venue, market, outcome, game_id, opened, is_live, qty, stake, start, sport, mtype))


def candle(t, bid, ask):
    return {"end_period_ts": t, "yes_bid": {"close_dollars": bid}, "yes_ask": {"close_dollars": ask}}


def kmarket(ticker, strike=None, sub=""):
    return {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0], "floor_strike": strike, "yes_sub_title": sub,
            "title": ticker, "status": "finalized", "settlement_value_dollars": "1.0000"}


def pm_single(conn, smt="football_team_full_game_winner", slug=PM_SLUG, markets=True, milestone=True):
    """A Polymarket single on the Jets, with the Kalshi side archived (unless switched off)."""
    bet(conn, f"polymarket:{slug}:1", "polymarket", slug, "yes", f"polymarket:{PM_EVENT}",
        "prop" if "player" in smt else "moneyline", stake=3_315_000, qty=10)
    archive.store(conn, "polymarket", "/v1/market/slug/{slug}", {}, {"market": {
        "slug": slug, "sportsMarketType": smt, "marketSides": [{"description": "Jets", "team": {"abbreviation": "nyj"}},
                                                               {"description": "Bears", "team": {"abbreviation": "chi"}}]}})
    q = {"slug": PM_EVENT, "marketTypes": "moneyline", "limit": 1}
    archive.store(conn, "polymarket", "/v1/events", q, {"events": [{"slug": PM_EVENT, "sportradarGameId": SR}], "query": q})
    if milestone:
        archive.store(conn, "kalshi", "/milestones", {"related_event_ticker": "KXNFLGAME-26OCT11NYJCHI"},
                      {"milestones": [MILESTONE]})
    else:
        archive.store(conn, "kalshi", "/milestones", {"sportradarGameId": SR},
                      {"milestones": [], "query": {"sportradarGameId": SR}})
    if markets is not False:
        q = {"event_ticker": "KXNFLGAME-26OCT11NYJCHI", "limit": 1000}
        ms = [kmarket("KXNFLGAME-26OCT11NYJCHI-NYJ"), kmarket("KXNFLGAME-26OCT11NYJCHI-CHI")] if markets is True else []
        archive.store(conn, "kalshi", "/markets", q, {"markets": ms, "query": q})


def kalshi_candles(conn, ticker, *candles):
    archive.store(conn, "kalshi", "/markets/{ticker}/candlesticks", {},
                  {"candlesticks": list(candles), "market": ticker, "start_ts": KEY_K})


def rows(conn):
    normalize.line_shop_rows(conn)
    cols = "bet_id, other_venue, other_market, other_side, yours, theirs, their_ask, their_fee, gap, note"
    return {r[0]: dict(zip(cols.split(", ")[1:], r[1:])) for r in conn.execute(f"SELECT {cols} FROM line_shop")}


def test_polymarket_single_priced_against_kalshi():
    conn = db()
    pm_single(conn)
    kalshi_candles(conn, "KXNFLGAME-26OCT11NYJCHI-NYJ", candle(KEY_K, "0.3000", "0.3100"))
    kalshi_candles(conn, "KXNFLGAME-26OCT11NYJCHI-CHI", candle(KEY_K, "0.6700", "0.6900"))  # NO on CHI asks .33
    r = rows(conn)[f"polymarket:{PM_SLUG}:1"]
    fee = round(match.kalshi_fee(310_000, stake=3_315_000))
    assert (r["other_venue"], r["other_market"], r["other_side"], r["their_ask"]) == (
        "kalshi", "KXNFLGAME-26OCT11NYJCHI-NYJ", "yes", 310_000)
    assert r["theirs"] == 310_000 + fee and r["yours"] == 331_500
    assert r["gap"] == 331_500 - (310_000 + fee) > 0 and r["note"] is None


def test_latest_point_decides():
    """An older two-sided candle doesn't price the bet when the latest one is one-sided."""
    conn = db()
    pm_single(conn)
    kalshi_candles(conn, "KXNFLGAME-26OCT11NYJCHI-NYJ", candle(KEY_K - 120, "0.3000", "0.3100"),
                   candle(KEY_K, "0.3000", "1.0000"))
    kalshi_candles(conn, "KXNFLGAME-26OCT11NYJCHI-CHI", candle(KEY_K, None, "0.6900"))
    r = rows(conn)[f"polymarket:{PM_SLUG}:1"]
    assert r["theirs"] is None and r["gap"] is None and r["note"] == "one-sided quote"


def test_stale_and_missing_quotes():
    conn = db()
    pm_single(conn)
    kalshi_candles(conn, "KXNFLGAME-26OCT11NYJCHI-NYJ", candle(KEY_K - 360, "0.3000", "0.3100"))
    assert rows(conn)[f"polymarket:{PM_SLUG}:1"]["note"] == "stale quote"
    conn = db()
    pm_single(conn)
    assert rows(conn)[f"polymarket:{PM_SLUG}:1"]["note"] == "no quote"


def test_reasons():
    conn = db()
    pm_single(conn, milestone=False)
    assert rows(conn)[f"polymarket:{PM_SLUG}:1"]["note"] == "game not on other venue"
    conn = db()
    pm_single(conn, markets=[])
    assert rows(conn)[f"polymarket:{PM_SLUG}:1"]["note"] == "no matching line"
    conn = db()
    pm_single(conn, markets=False)
    assert rows(conn)[f"polymarket:{PM_SLUG}:1"]["note"] == "not fetched yet"
    conn = db()
    pm_single(conn, smt="baseball_player_home_runs", slug="astatc-mlb-nyj-chi-2026-10-11-hr-abc-gte1")
    assert rows(conn)["polymarket:astatc-mlb-nyj-chi-2026-10-11-hr-abc-gte1:1"]["note"] == "prop type not covered"


def test_side_check_drops_a_flipped_price():
    conn = db()
    pm_single(conn)
    kalshi_candles(conn, "KXNFLGAME-26OCT11NYJCHI-NYJ", candle(KEY_K, "0.6800", "0.7000"))
    conn.execute("INSERT INTO fill_costs VALUES ('polymarket', 'f1', ?, 'entry', 10, 310000, 10000, NULL)",
                 (f"polymarket:{PM_SLUG}:1",))
    conn.execute("INSERT INTO fills VALUES ('polymarket', 'f1', 'o', ?, 'yes', 10, 320000, 0, 1, ?)", (PM_SLUG, OPENED))
    r = rows(conn)[f"polymarket:{PM_SLUG}:1"]
    assert r["note"] == "side check failed" and r["gap"] is None


def test_kalshi_single_priced_against_polymarket():
    conn = db()
    ticker, opened = "KXNFLSPREAD-26OCT04DETCAR-DET5", "2026-10-04T20:00:10Z"
    bet(conn, f"kalshi:{ticker}:1", "kalshi", ticker, "yes", "kalshi:m2", "spread", stake=5_000_000, qty=10,
        opened=opened, start="2026-10-05T00:20:00.000000Z")
    archive.store(conn, "kalshi", "/markets/{ticker}", {}, {"market": kmarket(ticker, 4.5, "DET wins by over 4.5")})
    archive.store(conn, "kalshi", "/milestones", {"related_event_ticker": "KXNFLGAME-26OCT04DETCAR"}, {"milestones": [
        {**MILESTONE, "id": "m2", "source_ids": {"source_3_id": SR2},
         "related_event_tickers": ["KXNFLGAME-26OCT04DETCAR", "KXNFLSPREAD-26OCT04DETCAR"],
         "details": {"main_game_event_ticker": "KXNFLGAME-26OCT04DETCAR"}}]})
    slug = "asc-nfl-det-car-2026-10-04-neg-4pt5"
    q = {"sportradarGameId": SR2, "limit": 1}
    archive.store(conn, "polymarket", "/v1/events", q, {"query": q, "events": [{
        "slug": "nfl-det-car-2026-10-04", "sportradarGameId": SR2, "markets": [{
            "slug": slug, "sportsMarketType": "football_team_full_game_spread", "line": -4.5,
            "marketSides": [{"description": "-4.50", "team": {"abbreviation": "det"}},
                            {"description": "+4.50", "team": {"abbreviation": "car"}}]}]}]})
    key = archive.fill_key("polymarket", opened)
    archive.store(conn, "polymarket", "/v1/price-history", {}, {
        "history": [{"timestamp": key, "longPrice": 0.52, "shortPrice": 0.50}], "market": slug, "start_ts": key})
    r = rows(conn)[f"kalshi:{ticker}:1"]
    fee = round(match.pm_fee(520_000, match.pm_theta_at(opened)))
    assert (r["other_venue"], r["other_market"], r["other_side"], r["their_ask"]) == ("polymarket", slug, "yes", 520_000)
    assert r["gap"] == 500_000 - (520_000 + fee) and r["note"] is None


# ---- archive ----

class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.ok = body, status, status == 200

    def json(self):
        return self.body

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


def getter(routes, calls):
    def get(path, params=None):
        calls.append((path, tuple(sorted((params or {}).items()))))
        for (p, match_param), body in routes.items():
            if p == path and (match_param is None or match_param in (params or {}).values()):
                return Resp(body)
        return Resp({}, 404)
    return get


def test_sync_line_shop_fetches_once_per_game_and_targets_the_fill_quote():
    conn = db()
    bet(conn, f"polymarket:{PM_SLUG}:1", "polymarket", PM_SLUG, "yes", f"polymarket:{PM_EVENT}", "moneyline",
        stake=3_315_000, qty=10)
    archive.store(conn, "polymarket", "/v1/market/slug/{slug}", {}, {"market": {
        "slug": PM_SLUG, "sportsMarketType": "football_team_full_game_winner",
        "marketSides": [{"description": "Jets", "team": {"abbreviation": "nyj"}},
                        {"description": "Bears", "team": {"abbreviation": "chi"}}]}})
    routes = {("/v1/events", PM_EVENT): {"events": [{"slug": PM_EVENT, "sportradarGameId": SR}]},
              ("/milestones", "KXNFLGAME-26OCT11NYJCHI"): {"milestones": [MILESTONE]},
              ("/milestones", None): {"milestones": [MILESTONE], "cursor": ""},
              ("/markets", "KXNFLGAME-26OCT11NYJCHI"): {"markets": [kmarket("KXNFLGAME-26OCT11NYJCHI-NYJ"),
                                                                    kmarket("KXNFLGAME-26OCT11NYJCHI-CHI")]}}
    calls = []
    now = "2026-10-12T00:00:00Z"
    archive.sync_line_shop(conn, getter(routes, calls), getter(routes, calls), now=now)
    n = len(calls)
    assert {c[0] for c in calls} == {"/v1/events", "/milestones", "/markets"}
    archive.sync_line_shop(conn, getter(routes, calls), getter(routes, calls), now=now)
    assert len(calls) == n  # nothing re-fetched
    targets = archive.line_shop_targets(conn)
    assert targets == {("kalshi", "KXNFLGAME-26OCT11NYJCHI-NYJ", KEY_K), ("kalshi", "KXNFLGAME-26OCT11NYJCHI-CHI", KEY_K)}


def test_sync_line_shop_skips_games_not_started():
    conn = db()
    bet(conn, f"polymarket:{PM_SLUG}:1", "polymarket", PM_SLUG, "yes", f"polymarket:{PM_EVENT}", "moneyline",
        stake=3_315_000, qty=10)
    calls = []
    archive.sync_line_shop(conn, getter({}, calls), getter({}, calls), now="2026-10-11T16:59:00Z")
    assert calls == []


def test_kalshi_line_events_for_a_knockout_game():
    m = {"details": {"main_game_event_ticker": "KXWCADVANCE-26JUL18FRAENG"}}
    assert archive.kalshi_line_events(m, "moneyline", "soccer_team_full_time_winner") == ["KXWCGAME-26JUL18FRAENG"]
    assert archive.kalshi_line_events(m, "moneyline", "soccer_game_to_advance") == ["KXWCADVANCE-26JUL18FRAENG"]


def test_light_event_falls_back_to_the_full_event():
    """Group-stage soccer events have no `moneyline` market type, so the light lookup comes back empty."""
    conn = db()
    bet(conn, f"polymarket:{PM_SLUG}:1", "polymarket", PM_SLUG, "yes", f"polymarket:{PM_EVENT}", "moneyline",
        stake=3_315_000, qty=10)
    archive.store(conn, "polymarket", "/v1/market/slug/{slug}", {}, {"market": {
        "slug": PM_SLUG, "sportsMarketType": "football_team_full_game_winner",
        "marketSides": [{"description": "Jets", "team": {"abbreviation": "nyj"}},
                        {"description": "Bears", "team": {"abbreviation": "chi"}}]}})
    full = {"events": [{"slug": PM_EVENT, "sportradarGameId": SR}]}
    calls = []

    def gateway(path, params=None):
        calls.append(params)
        return Resp({"events": []} if "marketTypes" in params else full)
    kalshi = getter({("/milestones", None): {"milestones": [], "cursor": ""}}, [])
    archive.sync_line_shop(conn, kalshi, gateway, now="2026-10-12T00:00:00Z")
    assert [("marketTypes" in p) for p in calls] == [True, False]
    reason, *_ = archive.line_shop_counterparts(conn)[f"polymarket:{PM_SLUG}:1"]
    assert reason == "game not on other venue"  # found SR via the full event; Kalshi has no milestone for it


def test_light_moneyline_page_doesnt_hide_the_full_event():
    """A Polymarket bet's moneyline-only page for the same game must not replace the full event a Kalshi bet uses."""
    conn = db()
    ticker = "KXNFLSPREAD-26OCT11NYJCHI-CHI3"
    bet(conn, f"kalshi:{ticker}:1", "kalshi", ticker, "yes", "kalshi:m1", "spread", stake=5_000_000, qty=10)
    archive.store(conn, "kalshi", "/markets/{ticker}", {}, {"market": kmarket(ticker, 2.5)})
    archive.store(conn, "kalshi", "/milestones", {}, {"milestones": [MILESTONE]})
    spread = {"slug": "asc-nfl-nyj-chi-2026-10-11-pos-2pt5", "sportsMarketType": "football_team_full_game_spread",
              "line": 2.5, "marketSides": [{"description": "+2.50", "team": {"abbreviation": "nyj"}},
                                           {"description": "-2.50", "team": {"abbreviation": "chi"}}]}
    ml = {"slug": PM_SLUG, "sportsMarketType": "football_team_full_game_winner",
          "marketSides": [{"description": "Jets", "team": {"abbreviation": "nyj"}},
                          {"description": "Bears", "team": {"abbreviation": "chi"}}]}
    q = {"sportradarGameId": SR, "limit": 1}
    archive.store(conn, "polymarket", "/v1/events", q, {"query": q, "events": [
        {"slug": PM_EVENT, "sportradarGameId": SR, "markets": [ml, spread]}]})
    q2 = {"slug": PM_EVENT, "marketTypes": "moneyline", "limit": 1}
    archive.store(conn, "polymarket", "/v1/events", q2, {"query": q2, "events": [
        {"slug": PM_EVENT, "sportradarGameId": SR, "markets": [ml]}]})
    reason, _, refs, _ = archive.line_shop_counterparts(conn)[f"kalshi:{ticker}:1"]
    assert reason is None and refs == [("asc-nfl-nyj-chi-2026-10-11-pos-2pt5", "no")]


def test_a_failed_id_lookup_is_retried():
    conn = db()
    ticker = "KXNFLGAME-26OCT11NYJCHI-NYJ"
    bet(conn, f"kalshi:{ticker}:1", "kalshi", ticker, "yes", "kalshi:m1", "moneyline", stake=3_000_000, qty=10)
    archive.store(conn, "kalshi", "/markets/{ticker}", {}, {"market": kmarket(ticker)})
    archive.store(conn, "kalshi", "/milestones", {}, {"milestones": [
        {**MILESTONE, "source_ids": {"source_2_id": "other", "source_3_id": SR}}]})
    state = {"fail": True}

    def gateway(path, params=None):
        if params.get("sportradarGameId") == SR and state["fail"]:
            return Resp({}, 429)
        return Resp({"events": [{"slug": PM_EVENT, "sportradarGameId": SR, "markets": []}]}
                    if params.get("sportradarGameId") == SR else {"events": []})
    archive.sync_line_shop(conn, getter({}, []), gateway, now="2026-10-12T00:00:00Z")
    assert archive.line_shop_counterparts(conn)[f"kalshi:{ticker}:1"][0] == "not fetched yet"
    state["fail"] = False
    archive.sync_line_shop(conn, getter({}, []), gateway, now="2026-10-12T00:00:00Z")
    assert archive.line_shop_counterparts(conn)[f"kalshi:{ticker}:1"][0] != "not fetched yet"


def test_a_failed_historical_lookup_is_retried():
    conn = db()
    pm_single(conn, markets=False)
    state = {"fail": True}

    def kalshi(path, params=None):
        if path == "/historical/markets":
            return Resp({}, 500) if state["fail"] else Resp(
                {"markets": [kmarket("KXNFLGAME-26OCT11NYJCHI-NYJ"), kmarket("KXNFLGAME-26OCT11NYJCHI-CHI")]})
        return Resp({"markets": []})
    archive.sync_line_shop(conn, kalshi, getter({}, []), now="2026-10-12T00:00:00Z")
    assert archive.line_shop_counterparts(conn)[f"polymarket:{PM_SLUG}:1"][0] == "not fetched yet"
    state["fail"] = False
    archive.sync_line_shop(conn, kalshi, getter({}, []), now="2026-10-12T00:00:00Z")
    assert archive.line_shop_counterparts(conn)[f"polymarket:{PM_SLUG}:1"][0] is None


def test_neutral_site_order_without_own_mid():
    """Polymarket lists ohiost first, Kalshi INDOSU: an Ohio St bet with no own mid is priced on Ohio St, not Indiana."""
    conn = db()
    slug, event, opened = "aec-cfb-ohiost-ind-2025-12-06", "cfb-ohiost-ind-2025-12-06", "2025-12-06T20:00:30Z"
    bet(conn, f"polymarket:{slug}:1", "polymarket", slug, "yes", f"polymarket:{event}", "moneyline",
        stake=6_500_000, qty=10, opened=opened, start="2025-12-07T01:00:00.000000Z")
    archive.store(conn, "polymarket", "/v1/market/slug/{slug}", {}, {"market": {
        "slug": slug, "sportsMarketType": "football_team_full_game_winner",
        "marketSides": [{"description": "Buckeyes", "team": {"abbreviation": "ohiost"}},
                        {"description": "Hoosiers", "team": {"abbreviation": "ind"}}]}})
    q = {"slug": event, "marketTypes": "moneyline", "limit": 1}
    archive.store(conn, "polymarket", "/v1/events", q, {"events": [{"slug": event, "sportradarGameId": SR}], "query": q})
    archive.store(conn, "kalshi", "/milestones", {}, {"milestones": [
        {**MILESTONE, "details": {"main_game_event_ticker": "KXNCAAFGAME-25DEC06INDOSU"}}]})
    q = {"event_ticker": "KXNCAAFGAME-25DEC06INDOSU", "limit": 1000}
    archive.store(conn, "kalshi", "/markets", q, {"query": q, "markets": [
        kmarket("KXNCAAFGAME-25DEC06INDOSU-OSU"), kmarket("KXNCAAFGAME-25DEC06INDOSU-IND")]})
    key = archive.fill_key("kalshi", opened)
    for t, bid, ask in (("KXNCAAFGAME-25DEC06INDOSU-OSU", "0.6300", "0.6400"),
                        ("KXNCAAFGAME-25DEC06INDOSU-IND", "0.3500", "0.3700")):
        archive.store(conn, "kalshi", "/markets/{ticker}/candlesticks", {},
                      {"candlesticks": [candle(key, bid, ask)], "market": t, "start_ts": key})
    r = rows(conn)[f"polymarket:{slug}:1"]
    assert r["other_market"] == "KXNCAAFGAME-25DEC06INDOSU-OSU" and r["their_ask"] == 640_000


def test_unknown_team_order_without_own_mid_is_left_unpriced():
    conn = db()
    pm_single(conn, markets=False)
    odd = {**MILESTONE, "id": "m9", "source_ids": {"source_3_id": SR},
           "details": {"main_game_event_ticker": "KXNFLGAME-26OCT11BRSJTS"}}
    archive.store(conn, "kalshi", "/milestones", {"x": 1}, {"milestones": [odd]})
    q = {"event_ticker": "KXNFLGAME-26OCT11BRSJTS", "limit": 1000}
    archive.store(conn, "kalshi", "/markets", q, {"query": q, "markets": [
        kmarket("KXNFLGAME-26OCT11BRSJTS-BRS"), kmarket("KXNFLGAME-26OCT11BRSJTS-JTS")]})
    kalshi_candles(conn, "KXNFLGAME-26OCT11BRSJTS-BRS", candle(KEY_K, "0.3000", "0.3100"))
    assert rows(conn)[f"polymarket:{PM_SLUG}:1"]["note"] == "team order unclear"
