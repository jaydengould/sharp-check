"""fills are rebuilt from raw_pages. Run: python -m pytest tests/  (no network)."""
import copy
import json
from pathlib import Path

import pytest

from sharp_check import archive, normalize

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


def fills(conn):
    cols = "venue, fill_id, order_id, market_id, outcome, qty, price, fee, is_taker, ts"
    rows = conn.execute(f"SELECT {cols} FROM fills ORDER BY fill_id").fetchall()
    return {r[1]: dict(zip(cols.split(", "), r)) for r in rows}


def test_kalshi_fill_acquires_outcome_side_at_its_price_and_dedups():
    a, b = load("kalshi_fills")[:2]
    a["fill_id"], b["fill_id"] = "k1", "k2"
    conn = archive.connect(":memory:")
    archive.store(conn, "kalshi", "/historical/fills", {}, {"fills": [a], "cursor": ""})
    archive.store(conn, "kalshi", "/portfolio/fills", {}, {"fills": [a, b], "cursor": ""})  # k1 overlaps
    normalize.rebuild(conn)

    got = fills(conn)
    assert len(got) == 2
    assert got["k1"] == {
        "venue": "kalshi", "fill_id": "k1", "order_id": "<scrubbed>",
        "market_id": "KXNFLPASSYDS-26SEP27KCMIA-KCPMAHOMES15-200",
        "outcome": "no", "qty": 243.12, "price": 290_000, "fee": 3_504_100,
        "is_taker": 1, "ts": "2026-09-25T09:09:52.685851Z",
    }
    assert got["k2"]["outcome"] == "yes" and got["k2"]["price"] == 75_000


def pm_page(*trades):
    return {"activities": [{"type": "ACTIVITY_TYPE_TRADE", "trade": t} for t in trades], "eof": True}


def test_pm_trade_maps_intent_to_outcome_and_uses_long_price():
    buy = load("pm_trades")[0]
    buy["id"] = "p1"
    short = copy.deepcopy(buy)
    short["id"] = "p2"
    short["aggressorExecution"]["order"]["intent"] = "ORDER_INTENT_BUY_SHORT"
    sell = copy.deepcopy(buy)
    sell["id"] = "p3"
    sell["aggressorExecution"]["order"]["intent"] = "ORDER_INTENT_SELL_LONG"

    conn = archive.connect(":memory:")
    archive.store(conn, "polymarket", "/v1/portfolio/activities", {}, pm_page(buy, short, sell))
    normalize.rebuild(conn)

    got = fills(conn)
    assert got["p1"] == {
        "venue": "polymarket", "fill_id": "p1", "order_id": "<scrubbed>",
        "market_id": "atc-fwc-arg-aut-2026-06-22-draw",
        "outcome": "yes", "qty": 34.21, "price": 140_000, "fee": 210_000,
        "is_taker": 1, "ts": "2026-06-22T17:46:04.908589Z",
    }
    # Trade price is the long (YES) price; NO costs 1 - price. Selling long = acquiring short.
    assert (got["p2"]["outcome"], got["p2"]["price"]) == ("no", 860_000)
    assert (got["p3"]["outcome"], got["p3"]["price"]) == ("no", 860_000)


def test_pm_unknown_trade_state_fails_loudly():
    t = load("pm_trades")[0]
    t["state"] = "TRADE_STATE_BUSTED"
    conn = archive.connect(":memory:")
    archive.store(conn, "polymarket", "/v1/portfolio/activities", {}, pm_page(t))
    with pytest.raises(ValueError, match="TRADE_STATE_BUSTED"):
        normalize.rebuild(conn)


def test_rebuild_is_idempotent():
    f = load("kalshi_fills")[0]
    f["fill_id"] = "k1"
    conn = archive.connect(":memory:")
    archive.store(conn, "kalshi", "/portfolio/fills", {}, {"fills": [f], "cursor": ""})
    normalize.rebuild(conn)
    normalize.rebuild(conn)
    assert len(fills(conn)) == 1


def ledger(conn):
    return conn.execute("SELECT venue, txn_id, ts, kind, amount FROM cash_ledger ORDER BY venue, ts, kind").fetchall()


def kalshi_cash(type_, id_, status, amount_cents, fee_cents=0, ts=1790000000):
    return {"id": id_, "status": status, "type": type_, "amount_cents": amount_cents,
            "fee_cents": fee_cents, "created_ts": ts}


def test_kalshi_cash_ledger_signs_fees_and_skips_failed():
    conn = archive.connect(":memory:")
    archive.store(conn, "kalshi", "/portfolio/deposits", {}, {"deposits": [kalshi_cash("debit", "d1", "applied", 5000, 150)]})
    archive.store(conn, "kalshi", "/portfolio/withdrawals", {}, {"withdrawals": [
        kalshi_cash("ach", "w1", "applied", 2000, ts=1790000100),
        kalshi_cash("ach", "w2", "failed", 9999, ts=1790000200),
    ]})
    normalize.rebuild(conn)
    assert ledger(conn) == [
        ("kalshi", "d1", "2026-09-21T14:13:20.000000Z", "deposit", 50_000_000),
        ("kalshi", "d1", "2026-09-21T14:13:20.000000Z", "deposit_fee", -1_500_000),
        ("kalshi", "w1", "2026-09-21T14:15:00.000000Z", "withdrawal", -20_000_000),
    ]


def pm_cash(type_, txn, status, value, ts="2026-07-01T00:00:00.123456789Z"):
    return {"type": type_, "accountBalanceChange": {
        "transactionId": txn, "status": status, "amount": {"value": value, "currency": "USD"}, "createTime": ts}}


def test_pm_cash_ledger_kinds_and_skips_rejected():
    conn = archive.connect(":memory:")
    archive.store(conn, "polymarket", "/v1/portfolio/activities", {}, {"eof": True, "activities": [
        pm_cash("ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "t1", "ACCOUNT_BALANCE_CHANGE_STATUS_COMPLETED", "25.00"),
        pm_cash("ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "t2", "ACCOUNT_BALANCE_CHANGE_STATUS_REJECTED", "25.00"),
        pm_cash("ACTIVITY_TYPE_REFERRAL_BONUS", "t3", "ACCOUNT_BALANCE_CHANGE_STATUS_COMPLETED", "4.67993"),
        pm_cash("ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL", "t4", "ACCOUNT_BALANCE_CHANGE_STATUS_COMPLETED", "10"),
        pm_cash("ACTIVITY_TYPE_ACCOUNT_DEPOSIT", "t5", "ACCOUNT_BALANCE_CHANGE_STATUS_PENDING", "50"),
        pm_cash("ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL", "t6", "ACCOUNT_BALANCE_CHANGE_STATUS_PENDING", "5"),
    ]})
    normalize.rebuild(conn)
    assert sorted((r[1], r[3], r[4]) for r in ledger(conn)) == [
        ("t1", "deposit", 25_000_000), ("t3", "bonus", 4_679_930), ("t4", "withdrawal", -10_000_000),
        ("t5", "deposit", 50_000_000)]


def test_unknown_cash_type_or_status_fails_loudly():
    conn = archive.connect(":memory:")
    archive.store(conn, "polymarket", "/v1/portfolio/activities", {}, {"eof": True, "activities": [
        pm_cash("ACTIVITY_TYPE_TAKER_FEE_REBATE", "t1", "ACCOUNT_BALANCE_CHANGE_STATUS_COMPLETED", "1")]})
    with pytest.raises(ValueError, match="TAKER_FEE_REBATE"):
        normalize.rebuild(conn)

    conn = archive.connect(":memory:")
    archive.store(conn, "kalshi", "/portfolio/withdrawals", {}, {"withdrawals": [kalshi_cash("ach", "w1", "pending", 1)]})
    with pytest.raises(ValueError, match="pending"):
        normalize.rebuild(conn)


def test_parlay_legs_from_kalshi_markets_and_pm_combo_trades():
    conn = archive.connect(":memory:")
    legs = [{"event_ticker": "E1", "market_ticker": "E1-A", "side": "yes"},
            {"event_ticker": "E2", "market_ticker": "E2-B", "side": "no"}]
    archive.store(conn, "kalshi", "/markets/{ticker}", {"ticker": "KXMVE-1"},
                  {"market": {"ticker": "KXMVE-1", "status": "active", "mve_selected_legs": legs}})
    archive.store(conn, "kalshi", "/markets/{ticker}", {"ticker": "KXNFL-1"},
                  {"market": {"ticker": "KXNFL-1", "status": "active", "mve_selected_legs": None}})

    t = load("pm_trades")[0]
    t["id"], t["marketSlug"] = "p1", "caoc-abc"
    t["comboLegDetails"] = [{"slug": "leg-a", "outcomeSide": "OUTCOME_SIDE_YES"},
                            {"slug": "leg-b", "outcomeSide": "OUTCOME_SIDE_NO"}]
    t2 = copy.deepcopy(t)
    t2["id"] = "p2"  # second fill of the same parlay repeats its legs
    archive.store(conn, "polymarket", "/v1/portfolio/activities", {}, pm_page(t, t2))
    normalize.rebuild(conn)

    assert conn.execute("SELECT * FROM parlay_legs ORDER BY venue, market_id, leg_market_id").fetchall() == [
        ("kalshi", "KXMVE-1", "E1-A", "yes"), ("kalshi", "KXMVE-1", "E2-B", "no"),
        ("polymarket", "caoc-abc", "leg-a", "yes"), ("polymarket", "caoc-abc", "leg-b", "no"),
    ]


def test_settlements_store_yes_value_for_finalized_markets_only():
    conn = archive.connect(":memory:")
    for ticker, status, value in (("K1", "finalized", "1.0000"), ("K2", "finalized", "0.0000"), ("K3", "active", None)):
        archive.store(conn, "kalshi", "/markets/{ticker}", {"ticker": ticker}, {"market": {
            "ticker": ticker, "status": status, "settlement_value_dollars": value,
            "settlement_ts": "2025-11-02T04:31:35.483634Z" if value else None}})

    r = load("pm_resolutions")[0]  # draw market, resolved NO
    archive.store(conn, "polymarket", "/v1/portfolio/activities", {}, {"eof": True, "activities": [
        {"type": "ACTIVITY_TYPE_POSITION_RESOLUTION", "positionResolution": r}]})
    normalize.rebuild(conn)

    assert conn.execute("SELECT * FROM settlements ORDER BY venue, market_id").fetchall() == [
        ("kalshi", "K1", 1_000_000, "2025-11-02T04:31:35.483634Z"),
        ("kalshi", "K2", 0, "2025-11-02T04:31:35.483634Z"),
        ("polymarket", "atc-fwc-arg-aut-2026-06-22-draw", 0, "2026-06-22T19:05:18.816321Z"),
    ]


def test_pm_resolution_side_disagreeing_with_price_fails_loudly():
    r = load("pm_resolutions")[0]
    r["side"] = "POSITION_RESOLUTION_SIDE_LONG"  # but outcomePrices says NO won
    conn = archive.connect(":memory:")
    archive.store(conn, "polymarket", "/v1/portfolio/activities", {}, {"eof": True, "activities": [
        {"type": "ACTIVITY_TYPE_POSITION_RESOLUTION", "positionResolution": r}]})
    with pytest.raises(ValueError, match="atc-fwc-arg-aut"):
        normalize.rebuild(conn)


def test_kalshi_perps_transfers_move_cash_and_shard_moves_are_ignored():
    def t(id_, src, dst, status="complete"):
        return {"transfer_id": id_, "source": src, "destination": dst, "amount": "50.000000",
                "status": status, "created_ts": 1790000000}
    conn = archive.connect(":memory:")
    archive.store(conn, "kalshi", "/portfolio/intra_exchange_instance_transfers", {}, {"transfers": [
        t("x1", "event_contract", "margined"), t("x2", "margined", "event_contract", "pending"),
        t("x3", "event_contract", "event_contract")]})
    normalize.rebuild(conn)
    assert sorted((r[1], r[3], r[4]) for r in ledger(conn)) == [
        ("x1", "perps_transfer", -50_000_000), ("x2", "perps_transfer", 50_000_000)]


def test_manual_cash_rows_join_the_ledger(tmp_path):
    f = tmp_path / "manual_cash.csv"
    f.write_text("venue,date,kind,amount,note\nkalshi,2025-11-01,bonus,10.00,a\nkalshi,2025-11-01,bonus,10.00,b\n"
                 "kalshi,2026-07-16,perps_bonus,25.00,skipped\n")
    conn = archive.connect(":memory:")
    normalize.rebuild(conn, manual_path=f)
    assert ledger(conn) == [("kalshi", "manual-2", "2025-11-01T00:00:00.000000Z", "bonus", 10_000_000),
                            ("kalshi", "manual-3", "2025-11-01T00:00:00.000000Z", "bonus", 10_000_000)]

    f.write_text("venue,date,kind,amount,note\nkalshi,2025-11-01,refund,1,a\n")
    with pytest.raises(ValueError, match="line 2: unhandled kind refund"):
        normalize.rebuild(conn, manual_path=f)


def bet_fill(conn, venue, fill_id, market, outcome, qty, price, fee, ts):
    conn.execute("INSERT INTO fills VALUES (?, ?, 'o', ?, ?, ?, ?, ?, 1, ?)",
                 (venue, fill_id, market, outcome, qty, price, fee, ts))


def test_derive_bets_matches_known_app_pnl():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    # Kalshi app, LAC @ CHI: cost $10.76, paid out $40.72, profit $29.96. Sale stored as acquire NO @0.54.
    bet_fill(conn, "kalshi", "f1", "CHI", "yes", 92.0, 110_000, 640_000, "2026-01-19T02:25:09.000000Z")
    bet_fill(conn, "kalshi", "f2", "CHI", "no", 92.0, 540_000, 1_600_000, "2026-01-19T02:43:19.000000Z")
    # Parlay: buy, partial sale, settles YES. Legs make it a parlay.
    bet_fill(conn, "kalshi", "f3", "KXMVE-1", "yes", 10.0, 100_000, 0, "2026-02-01T00:00:00.000000Z")
    bet_fill(conn, "kalshi", "f4", "KXMVE-1", "no", 4.0, 700_000, 10_000, "2026-02-01T01:00:00.000000Z")
    conn.execute("INSERT INTO parlay_legs VALUES ('kalshi', 'KXMVE-1', 'LEG', 'yes')")
    conn.execute("INSERT INTO settlements VALUES ('kalshi', 'KXMVE-1', 1000000, 't')")
    # Overshooting exit flips: 5 YES, then 8 NO -> closes 5, opens 3 NO that stay open. Fee split 5:3.
    bet_fill(conn, "polymarket", "f5", "M", "yes", 5.0, 400_000, 80_000, "2026-03-01T00:00:00.000000Z")
    bet_fill(conn, "polymarket", "f6", "M", "no", 8.0, 500_000, 80_000, "2026-03-01T01:00:00.000000Z")
    # Void: settles at 0.40.
    bet_fill(conn, "polymarket", "f7", "V", "no", 10.0, 500_000, 0, "2026-04-01T00:00:00.000000Z")
    conn.execute("INSERT INTO settlements VALUES ('polymarket', 'V', 400000, 't')")

    bets = {b[0]: b for b in normalize.derive_bets(conn)}
    pnl = {k: (b[15], b[16]) for k, b in bets.items()}
    assert pnl == {
        "kalshi:CHI:1": (29_960_000, "closed_early"),
        "kalshi:KXMVE-1:1": (-1_000_000 + 4 * 300_000 - 10_000 + 6 * 1_000_000, "settled"),
        "polymarket:M:1": (5 * 500_000 - 2_000_000 - 80_000 - 50_000, "closed_early"),
        "polymarket:M:2": (None, "open"),
        "polymarket:V:1": (6_000_000 - 5_000_000, "void"),
    }
    assert bets["kalshi:CHI:1"][10] == 10_760_000  # stake = app's cost
    assert bets["kalshi:KXMVE-1:1"][4] and not bets["kalshi:CHI:1"][4]  # is_parlay
    assert bets["polymarket:M:2"][8:11] == (3.0, 500_000, 1_530_000)  # 3 NO @0.50 + 3/8 of the fee


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


def test_pick_close_takes_latest_usable_quote_in_window():
    s = 10_000
    pts = [(s - 4000, 400_000, 500_000),   # stale
           (s - 60, 450_000, 470_000),
           (s, None, None),                # synthetic candle
           (s, 0, 470_000),                # one-sided
           (s + 60, 460_000, 480_000)]     # after start
    assert normalize.pick_close(s, pts) == (s - 60, 450_000, 470_000)
    assert normalize.pick_close(s, pts[:1]) is None


def test_quotes_from_both_venues_and_both_kalshi_spellings():
    conn = archive.connect(":memory:")
    hist, live, pm = load("kalshi_candles_hist"), load("kalshi_candles_live"), load("pm_price_history")
    for name, cs in (("H", hist), ("L", live)):
        archive.store(conn, "kalshi", "/markets/{ticker}/candlesticks", {},
                      {"candlesticks": cs, "market": name, "start_ts": cs[-1]["end_period_ts"]})
    archive.store(conn, "polymarket", "/v1/price-history", {},
                  {"history": pm, "market": "P", "start_ts": pm[-1]["timestamp"]})
    normalize.rebuild(conn)
    rows = {r[0]: r[1:] for r in conn.execute("SELECT market_id, yes_bid, yes_ask, yes_mid FROM quotes")}
    assert set(rows) == {"H", "L", "P"}
    p = pm[-1]
    bid, ask = normalize.ONE - normalize.micros(str(p["shortPrice"])), normalize.micros(str(p["longPrice"]))
    assert rows["P"] == (bid, ask, round((bid + ask) / 2))
    assert rows["H"][:2] == (normalize.micros(hist[-1]["yes_bid"]["close"]), normalize.micros(hist[-1]["yes_ask"]["close"]))
    assert rows["L"][:2] == (normalize.micros(live[-1]["yes_bid"]["close_dollars"]),
                             normalize.micros(live[-1]["yes_ask"]["close_dollars"]))


def test_clv_uses_your_side_mid_and_skips_ineligible_and_void():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)

    def bet(bid, outcome, status="settled", is_live=0):
        conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, is_live, entry_qty,
                        avg_entry, stake, exit_qty, exit_proceeds, exit_fees, payout, status, start_time, link,
                        market_type) VALUES (?, 'kalshi', ?, ?, 0, 't', ?, 10, 400000, 4100000, 0, 0, 0, 0, ?, 'S', 'game',
                        'moneyline')""", (bid, bid, outcome, is_live, status))
        conn.execute("INSERT INTO quotes VALUES ('kalshi', ?, 'S', 'q', 440000, 460000, 450000)", (bid,))
    bet("Y", "yes"); bet("N", "no"); bet("V", "yes", "void"); bet("L", "yes", is_live=1)
    normalize.clv_bets(conn)
    got = {r[0]: r[1:] for r in conn.execute("SELECT bet_id, close_mid, clv, clv_usd, clv_net, clv_move FROM bets")}
    assert got["Y"] == (450_000, 50_000, 500_000, 40_000, 60_000)     # net of 1c fee; move vs YES ask
    assert got["N"] == (550_000, 150_000, 1_500_000, 140_000, 160_000)  # NO ask = 1 - YES bid
    assert got["V"] == got["L"] == (None,) * 5


def test_closed_ts_is_settlement_or_last_exit_fill():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    bet_fill(conn, "kalshi", "a1", "S", "yes", 1, 400_000, 0, "2026-01-01T00:00:00.00001Z")
    conn.execute("INSERT INTO settlements VALUES ('kalshi', 'S', 1000000, '2026-01-02T00:00:00.5Z')")
    bet_fill(conn, "kalshi", "b1", "E", "yes", 2, 400_000, 0, "2026-01-01T00:00:00Z")
    bet_fill(conn, "kalshi", "b2", "E", "no", 1, 500_000, 0, "2026-01-01T01:00:00Z")
    bet_fill(conn, "kalshi", "b3", "E", "no", 1, 500_000, 0, "2026-01-01T02:00:00.12345Z")
    bet_fill(conn, "kalshi", "c1", "O", "yes", 1, 400_000, 0, "2026-01-01T00:00:00Z")
    got = {b[2]: b[17] for b in normalize.derive_bets(conn)}
    assert got == {"S": "2026-01-02T00:00:00.500000Z", "E": "2026-01-01T02:00:00.123450Z", "O": None}


def test_parlay_clv_products_and_breakdown():
    from test_archive import parlay_with_quotes
    conn = parlay_with_quotes()
    got = {r[0]: r[1:] for r in conn.execute(
        "SELECT bet_id, close_mid, fair_entry, clv, clv_net, clv_usd, clv_move FROM bets")}
    assert got["P1"] == (420_000, 300_000, 170_000, 160_000, 170_000, None)
    assert got["P2"] == got["SG"] == got["FU"] == (None,) * 6   # P2: no placement quotes


def test_parlay_leg_quote_too_wide_or_stale_gives_no_clv():
    from test_archive import parlay_with_quotes
    for bid, ask, quote_ts in ((100_000, 280_000, "2026-08-31T10:00:00.000000Z"),    # 18c wide
                               (495_000, 505_000, "2026-08-31T09:40:00.000000Z")):   # 20 min old
        conn = parlay_with_quotes()
        conn.execute("UPDATE quotes SET yes_bid = ?, yes_ask = ?, quote_ts = ? WHERE market_id = 'KXA-1'"
                     " AND at_time = '2026-08-31T10:00:00.000000Z'", (bid, ask, quote_ts))
        normalize.clv_bets(conn)
        assert conn.execute("SELECT close_mid, fair_entry FROM bets WHERE bet_id = 'P1'").fetchone() == (None, None)


def pm_market_page(slug, smt, sides, title=None, line=None, status="MARKET_STATUS_OPEN", prices=None,
                   start="2026-09-14T00:20:00Z"):
    m = {"slug": slug, "sportsMarketType": smt, "title": title, "question": f"Q {slug}?", "line": line,
         "gameStartTime": start, "status": status, "marketSides": sides}
    if prices:
        m["outcomePrices"] = json.dumps(prices)
    return {"market": m}


def test_pm_labels_name_both_sides_and_results_come_from_resolved_markets():
    side = lambda d, team=None: {"description": d, "team": team and {"name": team}}
    label = lambda *a, **k: normalize.pm_labels(pm_market_page(*a, **k)["market"])
    assert label("a", "football_team_full_game_winner", [side("Tigers", "LSU"), side("Rebels", "Ole Miss")]) \
        == ("LSU", "Ole Miss")
    assert label("b", "football_team_full_game_spread",
                 [side("-4.50", "Detroit Lions"), side("+4.50", "Carolina Panthers")]) \
        == ("Detroit Lions -4.5", "Carolina Panthers +4.5")
    assert label("c", "baseball_team_full_game_total", [side("Over"), side("Under")], line=8.5) == ("Over 8.5", "Under 8.5")
    assert label("d", "football_player_touchdowns", [side("Yes"), side("No")], title="Kyle Monangai 1+ touchdowns") \
        == ("Kyle Monangai 1+ touchdowns", None)
    assert label("e", "soccer_team_full_time_winner", [side("Yes", "France"), side("No", "France")]) == ("France", None)
    assert label("h", "soccer_player_goals", [side("Yes"), side("No")], title="Lionel Messi", line=1.0) \
        == ("Lionel Messi 1+ goals", None)
    assert label("i", "football_player_first_touchdown", [side("Yes"), side("No")], title="George Pickens") \
        == ("George Pickens first touchdown", None)
    yes = lambda *a, **k: normalize.pm_yes_value(pm_market_page(*a, **k)["market"])
    assert yes("f", "x", [], status="MARKET_STATUS_RESOLVED", prices=["0", "1"]) == 0
    assert yes("g", "x", [], prices=["0.4", "0.6"]) is None  # not resolved


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
    for slug in ("aec-nfl-a", "aec-nfl-b"):  # both resolved YES: a won (yes side); b lost (no side)
        archive.store(conn, "polymarket", "/v1/market/slug/{slug}", {"slug": slug},
                      pm_market_page(slug, "football_player_touchdowns", yn, title=f"{slug} 1+ touchdowns",
                                     status="MARKET_STATUS_RESOLVED", prices=["1", "0"]))
    normalize.rebuild(conn)
    got = dict(conn.execute("SELECT market_id, yes_value FROM markets WHERE venue = 'polymarket'"))
    assert got["caoc-lost"] == 0              # one lost leg settles it
    assert got["caoc-pending"] is None        # leg c has no lookup yet
    row = lambda m: conn.execute("SELECT label_yes, market_type, title FROM markets WHERE market_id = ?", (m,)).fetchone()
    assert row("aec-nfl-a") == ("aec-nfl-a 1+ touchdowns", "prop", "Q aec-nfl-a?")
    assert row("aec-nfl-c") == (None, None, "Game aec-nfl-c")


def test_pm_game_start_prefers_market_lookup_over_trade_copy():
    conn = archive.connect(":memory:")
    leg = lambda slug: {"slug": slug, "outcomeSide": "OUTCOME_SIDE_YES", "eventSlug": "ev-1", "title": slug,
                        "eventStartTime": "2026-09-14T00:20:00Z"}  # copied into the trade when placed
    t = load("pm_trades")[0]
    t["id"], t["marketSlug"] = "p1", "caoc-1"
    t["comboLegDetails"] = [leg("aec-nfl-a"), leg("aec-nfl-b")]
    t2 = copy.deepcopy(t)
    t2["id"], t2["marketSlug"], t2["comboLegDetails"] = "p2", "caoc-2", [leg("aec-nfl-b")]  # later, no lookup
    archive.store(conn, "polymarket", "/v1/portfolio/activities", {}, pm_page(t, t2))
    archive.store(conn, "polymarket", "/v1/market/slug/{slug}", {"slug": "aec-nfl-a"},
                  pm_market_page("aec-nfl-a", "football_team_full_game_winner", [], start="2026-09-15T00:20:00Z"))
    normalize.rebuild(conn)
    assert conn.execute("SELECT start_time FROM games WHERE game_id = 'polymarket:ev-1'").fetchone() \
        == ("2026-09-15T00:20:00.000000Z",)  # postponed a day; the lookup wins whatever the activity order


def test_kalshi_markets_carry_yes_label_and_final_value():
    conn = archive.connect(":memory:")
    archive.store(conn, "kalshi", "/markets/{ticker}", {}, {"market": {
        "ticker": "KXNFLGAME-26SEP24ATLGB-GB", "event_ticker": "KXNFLGAME-26SEP24ATLGB",
        "title": "Atlanta at Green Bay Winner?", "yes_sub_title": "Green Bay", "status": "finalized",
        "settlement_value_dollars": "1.0000", "settlement_ts": "2026-09-25T03:00:00Z", "floor_strike": None,
        "mve_selected_legs": None}})
    archive.store(conn, "kalshi", "/markets/{ticker}", {}, {"market": {
        "ticker": "KXNFLANYTD-25NOV03ARIDAL-DALGPICKENS3", "event_ticker": "KXNFLANYTD-25NOV03ARIDAL",
        "title": "Arizona at Dallas: Anytime Touchdown Scorer: George Pickens", "yes_sub_title": "George Pickens",
        "status": "active", "floor_strike": None, "mve_selected_legs": None}})
    normalize.rebuild(conn)
    got = {r[0]: r[1:] for r in conn.execute("SELECT market_id, label_yes, label_no, yes_value FROM markets")}
    assert got["KXNFLGAME-26SEP24ATLGB-GB"] == ("Green Bay", None, 1_000_000)
    assert got["KXNFLANYTD-25NOV03ARIDAL-DALGPICKENS3"] == ("George Pickens: Anytime Touchdown Scorer", None, None)


def test_latest_kalshi_market_page_wins_so_a_finalized_result_lands():
    conn = archive.connect(":memory:")
    m = lambda status, value=None: {"market": {
        "ticker": "KXNFLGAME-26SEP24ATLGB-GB", "event_ticker": "KXNFLGAME-26SEP24ATLGB", "title": "Atlanta at Green Bay Winner?",
        "yes_sub_title": "Green Bay", "status": status, "settlement_value_dollars": value,
        "settlement_ts": "2026-09-25T03:00:00Z",
        "floor_strike": None, "mve_selected_legs": None}}
    archive.store(conn, "kalshi", "/markets/{ticker}", {}, m("active"))
    archive.store(conn, "kalshi", "/markets/{ticker}", {}, m("finalized", "0.0000"))
    normalize.rebuild(conn)
    assert conn.execute("SELECT yes_value FROM markets").fetchone() == (0,)


def test_pm_helpers_survive_odd_market_records():
    assert normalize.pm_side_label({"description": "", "team": {"name": "LSU"}}, None) == "LSU"
    assert normalize.pm_yes_value({"status": "MARKET_STATUS_RESOLVED"}) is None


def test_fill_quote_needs_a_two_sided_quote_within_5_minutes_before_the_key():
    k = 100_000
    assert normalize.fill_quote([(k, 380_000, 400_000)], k) == (390_000, None)            # ends exactly at the key
    assert normalize.fill_quote([(k + 60, 380_000, 400_000)], k) == (None, "no quote")    # the fill's own minute
    assert normalize.fill_quote([(k - 301, 380_000, 400_000)], k) == (None, "stale quote")
    assert normalize.fill_quote([(k - 4000, 380_000, 400_000)], k) == (None, "no quote")  # outside the hour
    assert normalize.fill_quote([], k) == (None, "no quote")


def test_polymarket_point_at_the_fill_minute_is_excluded():
    m = archive.epoch("2026-09-01T10:00:00Z")
    key = archive.fill_key("polymarket", "2026-09-01T10:00:20Z")
    assert normalize.fill_quote([(m, 380_000, 400_000)], key) == (None, "no quote")
    assert normalize.fill_quote([(m - 60, 380_000, 400_000)], key) == (390_000, None)


def test_price_index_merges_pages_and_records_empty_ones():
    conn = archive.connect(":memory:")
    c = lambda t, b, a: {"end_period_ts": t, "yes_bid": {"close_dollars": b}, "yes_ask": {"close_dollars": a}}
    archive.store(conn, "kalshi", "/markets/{ticker}/candlesticks", {},
                  {"candlesticks": [c(60, "0.40", "0.42"), c(120, "0.41", "0.43")], "market": "K", "start_ts": 120})
    archive.store(conn, "kalshi", "/markets/{ticker}/candlesticks", {},
                  {"candlesticks": [c(120, "0.45", "0.47"), {"end_period_ts": 180}], "market": "K", "start_ts": 180})
    archive.store(conn, "kalshi", "/markets/{ticker}/candlesticks", {}, {"candlesticks": [], "market": "E", "start_ts": 60})
    points, pages = normalize.price_index(conn)
    assert points[("kalshi", "K")] == [(60, 400_000, 420_000), (120, 450_000, 470_000)]   # later page wins; null skipped
    assert pages == {("kalshi", "K", 120), ("kalshi", "K", 180), ("kalshi", "E", 60)}


def test_leg_value_rules():
    k, ONE = 100_000, normalize.ONE
    inplay = (k - 120, 850_000, 870_000)
    won = [inplay, (k, 990_000, ONE)]
    lost = [(k, 0, 10_000)]
    assert normalize.leg_value(won, k, True, True, ONE) == (ONE, True)        # pinned beats the older in-play quote
    assert normalize.leg_value(won, k, True, True, 0) is None                 # pinned disagrees with the result
    assert normalize.leg_value(won, k, True, True, None) is None              # no result yet
    assert normalize.leg_value(lost, k, True, True, 0) == (0, True)
    assert normalize.leg_value([], k, True, True, ONE) == (ONE, True)         # kept empty page after kickoff: closed
    assert normalize.leg_value([], k, False, True, ONE) is None               # never fetched
    assert normalize.leg_value([], k, True, False, ONE) is None               # before kickoff
    assert normalize.leg_value([(k - 60, 290_000, 310_000)], k, True, True, None) == (300_000, False)
    assert normalize.leg_value([(k - 60, 200_000, 260_000)], k, True, True, None) is None   # 6c wide
    assert normalize.leg_value([(k - 301, 290_000, 310_000)], k, True, True, None) is None  # stale
    assert normalize.leg_value([(k - 60, None, 310_000)], k, True, True, None) is None      # one-sided


def candle(t, bid, ask):
    return {"end_period_ts": t, "yes_bid": {"close_dollars": bid}, "yes_ask": {"close_dollars": ask}}


def spread_db(fills, candles):
    """fills: (fill_id, market, outcome, qty, price, ts); candles: {market: [candle, ...]}. Runs derive + spread."""
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    for f, m, o, q, p, ts in fills:
        bet_fill(conn, "kalshi", f, m, o, q, p, 0, ts)
    for m, cs in candles.items():
        archive.store(conn, "kalshi", "/markets/{ticker}/candlesticks", {},
                      {"candlesticks": cs, "market": m, "start_ts": cs[-1]["end_period_ts"]})
    allocs = []
    conn.executemany(f"INSERT INTO bets VALUES ({', '.join('?' * 30)})",
                     (b + (None,) * 12 for b in normalize.derive_bets(conn, allocs)))
    normalize.spread_costs(conn, allocs)
    return conn


def test_spread_sums_entries_and_exits_per_bet_signed_and_null_when_unpriced():
    e = archive.epoch
    t = lambda hm: e(f"2026-09-01T{hm}:00Z")
    conn = spread_db(
        [("a", "K", "yes", 10, 400_000, "2026-09-01T10:00:30Z"),    # mid .39: paid 1c
         ("b", "K", "yes", 10, 420_000, "2026-09-01T10:05:10Z"),    # mid .41: paid 1c
         ("c", "K", "no", 20, 450_000, "2026-09-01T11:00:20Z"),     # sold YES at .55 vs mid .57: 2c
         ("d", "N", "no", 5, 600_000, "2026-09-01T10:00:30Z"),      # NO mid .61: -1c, kept signed
         ("u", "U", "yes", 1, 500_000, "2026-09-01T10:00:30Z")],    # no quote
        {"K": [candle(t("10:00"), "0.38", "0.40"), candle(t("10:05"), "0.40", "0.42"), candle(t("11:00"), "0.56", "0.58")],
         "N": [candle(t("10:00"), "0.38", "0.40")]})
    got = {r[0]: r[1:] for r in conn.execute("SELECT market_id, entry_spread_usd, exit_spread_usd FROM bets")}
    assert got == {"K": (200_000, 400_000), "N": (-50_000, None), "U": (None, None)}
    rows = {r[0]: r[1:] for r in conn.execute("SELECT fill_id, role, mid_before, cost, note FROM fill_costs")}
    assert rows["c"] == ("exit", 430_000, 20_000, None)
    assert rows["u"] == ("entry", None, None, "no quote")


def test_flip_fill_is_an_exit_and_a_new_entry():
    t = archive.epoch("2026-09-01T10:00:00Z")
    conn = spread_db([("a", "K", "yes", 10, 400_000, "2026-09-01T09:59:30Z"),
                      ("b", "K", "no", 15, 600_000, "2026-09-01T10:00:30Z")],
                     {"K": [candle(t - 60, "0.38", "0.40"), candle(t, "0.38", "0.40")]})
    rows = conn.execute("SELECT fill_id, bet_id, role, qty, cost FROM fill_costs ORDER BY bet_id, role").fetchall()
    assert rows == [("a", "kalshi:K:1", "entry", 10.0, 10_000), ("b", "kalshi:K:1", "exit", 10.0, -10_000),
                    ("b", "kalshi:K:2", "entry", 5.0, -10_000)]


def test_parlay_exit_prices_legs_and_entry_uses_fair_entry():
    from test_archive import parlay_db
    ONE, e = normalize.ONE, archive.epoch
    conn = parlay_db()
    key = e("2026-09-01T02:00:00Z")
    conn.execute("UPDATE bets SET fair_entry = 300000 WHERE bet_id = 'P1'")
    bet_fill(conn, "kalshi", "in", "P1", "yes", 1, 250_000, 0, "2026-08-31T10:00:00Z")
    bet_fill(conn, "kalshi", "out", "P1", "no", 1, 400_000, 0, "2026-09-01T02:00:30Z")   # sold the parlay at .60
    bet_fill(conn, "kalshi", "sg", "SG", "no", 1, 400_000, 0, "2026-09-01T02:00:30Z")
    store = lambda m, cs: archive.store(conn, "kalshi", "/markets/{ticker}/candlesticks", {},
                                        {"candlesticks": cs, "market": m, "start_ts": key})
    store("KXA-1", [candle(key - 120, "0.85", "0.87"), candle(key, "0.99", "1.00")])   # YES leg, decided won
    store("KXB-1", [candle(key, "0.29", "0.31")])                                      # NO leg: .70 our side
    conn.execute("UPDATE markets SET yes_value = ? WHERE market_id = 'KXA-1'", (ONE,))
    allocs = [("kalshi", "in", "P1", "entry", 1.0), ("kalshi", "out", "P1", "exit", 1.0),
              ("kalshi", "sg", "SG", "exit", 1.0)]
    normalize.spread_costs(conn, allocs)
    rows = {r[0]: r[1:] for r in conn.execute("SELECT fill_id, mid_before, cost, note FROM fill_costs")}
    assert rows["in"] == (300_000, -50_000, None)
    assert rows["out"] == (300_000, 100_000, "1 legs decided")            # fair .70, exit mid (NO side) .30
    assert rows["sg"] == (None, None, "parlay not eligible: same-game legs")
    assert conn.execute("SELECT entry_spread_usd, exit_spread_usd FROM bets WHERE bet_id = 'P1'").fetchone() == (
        -50_000, 100_000)
    conn.execute("UPDATE markets SET yes_value = 0 WHERE market_id = 'KXA-1'")   # pinned high but lost: unpriced
    conn.execute("DELETE FROM fill_costs")
    normalize.spread_costs(conn, allocs)
    assert conn.execute("SELECT cost, note FROM fill_costs WHERE fill_id = 'out'").fetchone() == (
        None, "parlay leg unpriced")


def test_fill_quote_never_falls_back_past_a_one_sided_latest_quote():
    k, ONE = 100_000, normalize.ONE
    decided = [(k - 300, 340_000, 580_000), (k, 910_000, ONE)]    # no offers once the game is decided
    assert normalize.fill_quote(decided, k) == (None, "one-sided quote")


def test_a_fill_is_priced_only_from_a_page_whose_window_covers_its_key():
    t = archive.epoch("2026-09-01T10:00:00Z")
    # The only stored page ends at fill a's key; fill b's own fetch never landed (e.g. an outage).
    conn = spread_db([("a", "K", "yes", 1, 400_000, "2026-09-01T10:00:30Z"),
                      ("b", "K", "yes", 1, 400_000, "2026-09-01T10:03:30Z")],
                     {"K": [candle(t, "0.38", "0.40")]})
    rows = {r[0]: r[1:] for r in conn.execute("SELECT fill_id, cost, note FROM fill_costs")}
    assert rows["a"] == (10_000, None)
    assert rows["b"] == (None, "no quote")    # not the 3-minute-old quote from a's page
