"""Archive is append-only and idempotent. Run: python -m pytest tests/  (no network)."""
from sharp_check import archive, normalize


def fake_pager(pages):
    return lambda endpoint, limit=None: iter(pages)


def test_resync_adds_nothing_and_new_data_appends():
    conn = archive.connect(":memory:")
    page1 = ({"limit": 1}, {"fills": [{"fill_id": "a"}], "cursor": ""})
    eps = [("kalshi", "/portfolio/fills", fake_pager([page1]), 1)]

    assert archive.sync(conn, eps)
    assert archive.sync(conn, eps)  # same data again
    assert conn.execute("SELECT COUNT(*) FROM raw_pages").fetchone()[0] == 1

    page2 = ({"limit": 1}, {"fills": [{"fill_id": "b"}], "cursor": ""})
    archive.sync(conn, [("kalshi", "/portfolio/fills", fake_pager([page2]), 1)])
    assert conn.execute("SELECT COUNT(*) FROM raw_pages").fetchone()[0] == 2  # old page kept, new appended


def test_one_failing_endpoint_does_not_block_others():
    def boom(endpoint, limit=None):
        raise RuntimeError("HTTP 500")
        yield

    conn = archive.connect(":memory:")
    good = ("polymarket", "/v1/portfolio/activities", fake_pager([({}, {"activities": [], "eof": True})]), 100)
    assert not archive.sync(conn, [("kalshi", "/portfolio/fills", boom, 1), good])
    assert conn.execute("SELECT endpoint FROM raw_pages").fetchall() == [("/v1/portfolio/activities",)]


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self.body = status, body

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_kalshi_markets_fall_back_to_historical_and_skip_finalized():
    conn = archive.connect(":memory:")
    fills = [{"market_ticker": "OLD"}, {"market_ticker": "LIVE"}, {"market_ticker": "GONE"}]
    archive.store(conn, "kalshi", "/portfolio/fills", {}, {"fills": fills, "cursor": ""})
    calls = []

    def get(path):
        calls.append(path)
        if path in ("/markets/OLD", "/markets/GONE", "/historical/markets/GONE"):  # GONE: on neither tier, skipped
            return Resp(404)
        status = "active" if path == "/markets/LIVE" else "finalized"
        return Resp(200, {"market": {"ticker": path.rsplit("/", 1)[1], "status": status}})

    archive.sync_kalshi_markets(conn, get)
    assert calls == ["/markets/GONE", "/historical/markets/GONE", "/markets/LIVE", "/markets/OLD", "/historical/markets/OLD"]

    calls.clear()
    archive.sync_kalshi_markets(conn, get)
    assert calls == ["/markets/GONE", "/historical/markets/GONE", "/markets/LIVE"]  # OLD is finalized; LIVE may still settle


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

    # A started game with an unsettled market of yours (postponed?) is refetched, for two weeks at most.
    archive.store(conn, "kalshi", "/markets/{ticker}", {}, {"market": {
        "ticker": "KXNFLTD-26SEP24ATLGB-X", "event_ticker": "KXNFLTD-26SEP24ATLGB", "status": "active"}})
    archive.sync_kalshi_milestones(conn, get, now="2026-10-01T00:00:00Z")
    assert calls == ["KXNFLTD-26SEP24ATLGB", "KXNFLGAME-26SEP24ATLGB"]
    calls.clear()
    archive.sync_kalshi_milestones(conn, get, now="2026-10-09T01:00:00Z")
    assert calls == []
    settled = market_page("KXNFLTD-26SEP24ATLGB-X", "KXNFLTD-26SEP24ATLGB")
    settled["market"]["result"] = "yes"  # a new body (raw_pages dedups on body)
    archive.store(conn, "kalshi", "/markets/{ticker}", {}, settled)
    archive.sync_kalshi_milestones(conn, get, now="2026-10-01T00:00:00Z")
    assert calls == []  # settled since: the older active page doesn't count


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
    assert conn.execute("SELECT COUNT(*) FROM raw_pages WHERE endpoint = '/v1/market/slug/{slug}'").fetchone()[0] == 1


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
    bet("polymarket", "p2", "2026-09-01T00:00:00.000000Z")      # same body as p1
    bet("polymarket", "p4", "2026-09-01T00:00:00.000000Z")      # no points yet
    bet("polymarket", "p3", "2026-10-03T12:00:00.000000Z")      # not started
    calls = []

    def kget(path, params):
        calls.append(path)
        if "KXB" in path:
            return Resp(500)
        return Resp(404) if path.startswith("/series/") else Resp(200, {"candlesticks": [{"end_period_ts": 1}]})

    def gget(path, params):
        calls.append(params["symbol"])
        return Resp(200, {"history": [] if params["symbol"] == "p4" else [{"timestamp": 1}]})

    now = "2026-10-03T11:58:00Z"
    assert not archive.sync_closes(conn, kget, gget, now)
    assert calls == ["/series/KXA/markets/KXA-1/candlesticks", "/historical/markets/KXA-1/candlesticks",
                     "/series/KXB/markets/KXB-1/candlesticks", "p1", "p2", "p4"]
    calls.clear()
    archive.sync_closes(conn, kget, gget, now)
    assert calls == ["/series/KXB/markets/KXB-1/candlesticks", "p4"]   # failed and empty ones are retried
    conn.execute("UPDATE bets SET start_time = '2026-09-02T00:00:00.000000Z' WHERE market_id = 'p1'")
    calls.clear()
    archive.sync_closes(conn, kget, gget, now)
    assert "p1" in calls                                         # postponed: new start is refetched


def parlay_db():
    """Two eligible cross-game parlays sharing leg A1, one same-game parlay. Game g1 at 2026-09-01, g2 a day later."""
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    conn.executemany("INSERT INTO games VALUES (?, 'kalshi', 'NFL', ?, ?)",
                     [("g1", "g1", "2026-09-01T00:00:00.000000Z"), ("g2", "g2", "2026-09-02T00:00:00.000000Z")])
    conn.executemany("INSERT INTO markets (venue, market_id, market_type, game_id) VALUES ('kalshi', ?, ?, ?)",
                     [("KXA-1", "moneyline", "g1"), ("KXB-1", "total", "g2"), ("KXC-1", "prop", "g1"),
                      ("KXF-1", "future", None)])
    parlays = {"P1": (["KXA-1", "KXB-1"], "2026-08-31T10:00:00.123456Z"),
               "P2": (["KXA-1", "KXB-1"], "2026-08-31T12:00:00Z"),
               "SG": (["KXA-1", "KXC-1"], "2026-08-31T10:00:00Z"),
               "FU": (["KXA-1", "KXF-1"], "2026-08-31T10:00:00Z")}
    for p, (legs, opened) in parlays.items():
        conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, is_live, entry_qty,
                        avg_entry, stake, exit_qty, exit_proceeds, exit_fees, payout, status, start_time, link,
                        market_type) VALUES (?, 'kalshi', ?, 'yes', 1, ?, 0, 1, 1, 1, 0, 0, 0, 0, 'settled',
                        '2026-09-01T00:00:00.000000Z', 'game', 'parlay')""", (p, p, opened))
        conn.executemany("INSERT INTO parlay_legs VALUES ('kalshi', ?, ?, ?)",
                         [(p, leg, "no" if leg == "KXB-1" else "yes") for leg in legs])
    return conn


def test_parlay_eligibility_reasons():
    conn = parlay_db()
    got = {b: r for b, (r, _, _) in archive.parlay_clv_legs(conn).items()}
    assert got == {"P1": None, "P2": None, "SG": "same-game legs", "FU": "leg without a game"}
    conn.execute("UPDATE bets SET status = 'void' WHERE bet_id = 'P2'")
    conn.execute("UPDATE bets SET is_live = 1 WHERE bet_id = 'SG'")
    conn.execute("UPDATE bets SET is_live = NULL WHERE bet_id = 'FU'")
    got = {b: r for b, (r, _, _) in archive.parlay_clv_legs(conn).items()}
    assert got == {"P1": None, "P2": "void", "SG": "live", "FU": "no start time"}


def test_sync_closes_fetches_parlay_leg_windows_at_kickoff_and_placement():
    conn = parlay_db()
    calls = []

    def kget(path, params):
        calls.append((path.split("/")[-2], params["end_ts"]))
        return Resp(200, {"candlesticks": [{"end_period_ts": 1}]})

    assert archive.sync_closes(conn, kget, None, "2026-10-01T00:00:00Z")
    e = archive.epoch
    assert sorted(calls) == sorted([
        ("KXA-1", e("2026-09-01T00:00:00Z")), ("KXB-1", e("2026-09-02T00:00:00Z")),       # kickoffs, once each
        ("KXA-1", e("2026-08-31T10:00:00Z")), ("KXB-1", e("2026-08-31T10:00:00Z")),       # P1 placement (floored)
        ("KXA-1", e("2026-08-31T12:00:00Z")), ("KXB-1", e("2026-08-31T12:00:00Z"))])      # P2 placement


def parlay_with_quotes():
    """parlay_db with P1 fully quoted (close 0.42, fair entry 0.30, entry 0.25, all-in 0.26) and CLV derived."""
    conn = parlay_db()
    conn.execute("UPDATE bets SET avg_entry = 250000, stake = 260000, realized_pnl = -260000")
    q = lambda m, at, mid: conn.execute("INSERT INTO quotes VALUES ('kalshi', ?, ?, ?, ?, ?, ?)",
                                        (m, at, at, mid - 5000, mid + 5000, mid))
    q("KXA-1", "2026-09-01T00:00:00.000000Z", 600_000)   # kickoff
    q("KXB-1", "2026-09-02T00:00:00.000000Z", 300_000)   # NO leg: 0.70 on our side
    q("KXA-1", "2026-08-31T10:00:00.000000Z", 500_000)   # P1 placement, floored to the second
    q("KXB-1", "2026-08-31T10:00:00.000000Z", 400_000)   # NO leg: 0.60
    normalize.clv_bets(conn)
    return conn


def add_fill(conn, venue, fill_id, market, outcome, ts):
    conn.execute("INSERT INTO fills VALUES (?, ?, 'o', ?, ?, 1, 500000, 0, 1, ?)", (venue, fill_id, market, outcome, ts))


def test_fill_key_is_the_minute_before_the_fill_and_one_more_on_polymarket():
    e = archive.epoch
    assert archive.fill_key("kalshi", "2026-09-01T10:00:45.123Z") == e("2026-09-01T10:00:00Z")
    assert archive.fill_key("kalshi", "2026-09-01T10:00:00.000000Z") == e("2026-09-01T10:00:00Z")
    assert archive.fill_key("polymarket", "2026-09-01T10:00:45.123456Z") == e("2026-09-01T09:59:00Z")


def test_sync_closes_fetches_each_fill_minute_once_and_keeps_empty_kalshi_exit_legs():
    conn = parlay_db()
    e = archive.epoch
    add_fill(conn, "kalshi", "s1", "KXS-1", "yes", "2026-08-31T10:00:15.000000Z")
    add_fill(conn, "kalshi", "s2", "KXS-1", "no", "2026-08-31T10:00:45.000000Z")         # same minute: one fetch
    add_fill(conn, "polymarket", "p1", "pm-s", "yes", "2026-08-31T10:00:30.000000Z")
    # P1 (eligible) partly cashed out and still open; the exit's fraction is shorter than closed_ts' would be
    conn.execute("UPDATE bets SET exit_qty = 1 WHERE bet_id = 'P1'")
    add_fill(conn, "kalshi", "x1", "P1", "yes", "2026-08-31T10:00:00.123456Z")            # entry: nothing new
    add_fill(conn, "kalshi", "x2", "P1", "no", "2026-09-01T02:00:30.5Z")                  # exit: every leg
    conn.execute("UPDATE bets SET exit_qty = 1, closed_ts = '2026-09-01T03:00:00.000000Z' WHERE bet_id = 'SG'")
    add_fill(conn, "kalshi", "x3", "SG", "no", "2026-09-01T02:30:00Z")                    # same-game: not priced
    exit_key = e("2026-09-01T02:00:00Z")
    calls = []

    def kget(path, params):
        calls.append((path.split("/")[-2], params["end_ts"]))
        empty = params["end_ts"] == exit_key or "KXS-1" in path
        return Resp(200, {"candlesticks": [] if empty else [{"end_period_ts": 1}]})

    def gget(path, params):
        calls.append((params["symbol"], params["timestamp.endTimestamp"]))
        return Resp(200, {"history": [{"timestamp": 1}]})

    archive.sync_closes(conn, kget, gget, "2026-10-01T00:00:00Z")
    assert calls.count(("KXS-1", e("2026-08-31T10:00:00Z"))) == 1
    assert ("pm-s", e("2026-08-31T09:59:00Z")) in calls
    assert ("KXA-1", exit_key) in calls and ("KXB-1", exit_key) in calls
    assert not [c for c in calls if c[1] == e("2026-09-01T02:30:00Z")]
    calls.clear()
    archive.sync_closes(conn, kget, gget, "2026-10-01T00:00:00Z")
    assert ("KXS-1", e("2026-08-31T10:00:00Z")) in calls     # empty single: retried
    assert ("KXA-1", exit_key) not in calls                  # empty Kalshi exit leg: kept, done


def test_kalshi_markets_include_legs_of_parlays_with_an_exit():
    conn = archive.connect(":memory:")
    fills = [{"market_ticker": "KXMVE-OUT", "outcome_side": "yes"}, {"market_ticker": "KXMVE-OUT", "outcome_side": "no"},
             {"market_ticker": "KXMVE-HELD", "outcome_side": "yes"}]
    archive.store(conn, "kalshi", "/portfolio/fills", {}, {"fills": fills, "cursor": ""})
    leg = lambda t: {"market_ticker": t, "event_ticker": t, "side": "yes"}
    for ticker, legs in (("KXMVE-OUT", [leg("LA"), leg("LB")]), ("KXMVE-HELD", [leg("LC")])):
        archive.store(conn, "kalshi", "/markets/{ticker}", {},
                      {"market": {"ticker": ticker, "status": "finalized", "mve_selected_legs": legs}})
    calls = []

    def get(path):
        calls.append(path)
        return Resp(200, {"market": {"ticker": path.rsplit("/", 1)[1], "status": "finalized"}})

    archive.sync_kalshi_markets(conn, get)
    assert calls == ["/markets/LA", "/markets/LB"]   # spread pricing needs exited parlays' leg results
