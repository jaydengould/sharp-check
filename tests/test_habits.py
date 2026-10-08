"""Phase 6 habit metrics. Run: python -m pytest tests/  (no network)."""
from datetime import datetime, timedelta, timezone

from sharp_check import archive, habits, normalize
from sharp_check.normalize import ONE


def db():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    return conn


def ledger(conn, ts, amount):
    conn.execute("INSERT INTO cash_ledger VALUES ('kalshi', ?, ?, 'deposit', ?)", (ts, ts, amount))


def bet(conn, bet_id, opened, stake, closed=None, pnl=None, venue="kalshi", start=None, clv_net=None, is_parlay=0):
    conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, is_live, entry_qty,
                    avg_entry, stake, exit_qty, exit_proceeds, exit_fees, payout, realized_pnl, status, closed_ts,
                    start_time, clv_net) VALUES (?, ?, ?, 'yes', ?, ?, ?, 1, 0, ?, 0, 0, 0, 0, ?, ?, ?, ?, ?)""",
                 (bet_id, venue, bet_id, is_parlay, opened, None if start is None else 0, stake, pnl,
                  "open" if closed is None else "settled", closed, start, clv_net))


def test_bankroll_moves_on_cash_and_closed_bets_only():
    conn = db()
    ledger(conn, "2026-01-01T00:00:00.000000Z", 100 * ONE)
    bet(conn, "a", "2026-01-02T00:00:00Z", 10 * ONE, closed="2026-01-03T00:00:00.000000Z", pnl=5 * ONE)
    bet(conn, "b", "2026-01-04T00:00:00Z", 20 * ONE)  # open: no move
    assert [s[1:] for s in habits.bankroll_series(conn)] == [(100 * ONE, 0), (105 * ONE, 5 * ONE)]


def test_previous_result_is_last_bet_closed_before_opening():
    conn = db()
    ledger(conn, "2026-01-01T00:00:00.000000Z", 100 * ONE)
    bet(conn, "w", "2026-01-02T00:00:00Z", ONE, closed="2026-01-02T05:00:00.000000Z", pnl=ONE)
    bet(conn, "l", "2026-01-02T01:00:00Z", ONE, closed="2026-01-02T09:00:00.000000Z", pnl=-ONE, venue="polymarket")
    bet(conn, "x", "2026-01-02T06:00:00.12345Z", 4 * ONE)  # l still running: previous is w
    bet(conn, "y", "2026-01-02T10:00:00Z", 2 * ONE)
    bet(conn, "z", "2026-01-02T09:00:00.000000Z", ONE)       # closed at the same instant doesn't count
    rows = {r["bet_id"]: r for r in habits.bet_rows(conn)}
    assert {k: r["tag"] for k, r in rows.items()} == {"w": "first", "l": "first", "x": "after_win",
                                                      "y": "after_loss", "z": "after_win"}
    assert rows["x"]["bankroll"] == 101 * ONE and abs(rows["x"]["pct"] - 4 / 101) < 1e-12
    got = {t: (n, med) for t, n, med, _ in habits.after_results(list(rows.values()))}
    assert got == {"after_win": (2, 2.5 * ONE), "after_loss": (1, 2 * ONE), "after_push": (0, None), "first": (2, ONE)}


def test_pct_is_none_without_bankroll_and_stake_pct_summary():
    conn = db()
    bet(conn, "early", "2025-12-31T00:00:00Z", ONE)  # before any deposit
    ledger(conn, "2026-01-01T00:00:00.000000Z", 100 * ONE)
    for i, s in enumerate((1, 2, 3, 10)):
        bet(conn, f"b{i}", f"2026-01-0{i + 2}T00:00:00Z", s * ONE)
    rows = habits.bet_rows(conn)
    assert rows[0]["pct"] is None
    s = habits.stake_pct(rows)
    assert abs(s["median"] - 0.025) < 1e-12 and s["max"] == 0.10 and [r["bet_id"] for r in s["top"]][:2] == ["b3", "b2"]


def test_timing_buckets_lower_bound_inclusive_and_clv_hidden_below_min_n():
    conn = db()
    opened = datetime(2026, 1, 5, tzinfo=timezone.utc)
    for i, lead in enumerate((60, 15 * 60, 3600, 6 * 3600, 30 * 3600)):
        start = (opened + timedelta(seconds=lead)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        bet(conn, f"b{i}", "2026-01-05T00:00:00Z", ONE, start=start, clv_net=10_000)
    bet(conn, "nostart", "2026-01-05T00:00:00Z", ONE)
    bet(conn, "parlay", "2026-01-05T00:00:00Z", ONE, start="2026-01-05T00:01:00.000000Z", clv_net=990_000, is_parlay=1)
    rows = habits.timing(habits.bet_rows(conn), min_n=1)
    assert rows[0][3] == 1  # the parlay counts for timing, not for CLV
    assert [(r[0], r[1]) for r in rows] == [("< 15 min", 2), ("15 min–1 h", 1), ("1–6 h", 1), ("6–24 h", 1),
                                            ("> 24 h", 1)]
    assert rows[0][4] == 1.0
    assert habits.timing(habits.bet_rows(conn))[0][4] is None


def test_empty_db_renders():
    conn = db()
    rows = habits.bet_rows(conn)
    assert habits.bankroll_series(conn) == [] and rows == []
    assert habits.stake_pct(rows)["median"] is None
    assert all(r[1] == 0 for r in habits.timing(rows))


def fill(conn, venue, market, qty, price, fee):
    conn.execute("INSERT INTO fills VALUES (?, ?, 'o', ?, 'yes', ?, ?, ?, 1, 't')",
                 (venue, f"{venue}{market}", market, qty, price, fee))


def test_fees_split_and_maker_saving():
    conn = db()
    fill(conn, "kalshi", "S", 10, 400_000, 170_000)      # single
    fill(conn, "polymarket", "P", 10, 500_000, 180_000)  # single
    fill(conn, "kalshi", "X", 5, 200_000, 50_000)        # parlay
    conn.execute("INSERT INTO parlay_legs VALUES ('kalshi', 'X', 'L', 'yes')")
    bet(conn, "S", "t", 4_170_000)
    bet(conn, "P", "t", 5_180_000, venue="polymarket")
    bet(conn, "X", "t", 1_050_000, is_parlay=1)
    got = {(r[0], r[1]): r for r in habits.fees(conn)}
    k = got[("kalshi", "singles")]
    assert k[2:6] == (10, 170_000, 1.7, 170_000 / 4_170_000)
    assert k[6] == round(170_000 - 0.0175 * 10 * 0.4 * 0.6 * ONE)        # Kalshi maker pays 0.0175
    assert got[("polymarket", "singles")][6] == round(180_000 + 0.0125 * 10 * 0.25 * ONE)  # PM maker gets a rebate
    assert got[("kalshi", "parlays")][6] is None
    assert ("polymarket", "parlays") not in got


def test_same_timestamp_events_are_netted_and_void_is_a_push():
    conn = db()
    ledger(conn, "2026-01-01T00:00:00.000000Z", 100 * ONE)
    conn.execute("INSERT INTO cash_ledger VALUES ('kalshi', 'f', '2026-01-01T00:00:00.000000Z', 'deposit_fee', -2000000)")
    assert [s[1] for s in habits.bankroll_series(conn)] == [98 * ONE]  # no dip below zero
    bet(conn, "v", "2026-01-02T00:00:00Z", ONE, closed="2026-01-03T00:00:00.000000Z", pnl=-10_000)
    conn.execute("UPDATE bets SET status = 'void' WHERE bet_id = 'v'")
    bet(conn, "n", "2026-01-04T00:00:00Z", ONE)
    assert {r["bet_id"]: r["tag"] for r in habits.bet_rows(conn)}["n"] == "after_push"


def test_bankroll_check_nets_partial_exits_of_open_bets(monkeypatch):
    conn = db()
    ledger(conn, "2026-01-01T00:00:00.000000Z", 100 * ONE)
    bet(conn, "p", "2026-01-02T00:00:00Z", 4 * ONE)  # bought 10 at 0.40, sold 5 at 0.50, still open
    conn.execute("UPDATE bets SET exit_proceeds = 2500000, exit_fees = 10000 WHERE bet_id = 'p'")
    cash = {"kalshi": 100 * ONE - 4 * ONE + 2_500_000 - 10_000, "polymarket": 0}
    monkeypatch.setattr(habits.reconcile, "reported_cash", lambda c, v: cash[v])
    assert habits.bankroll_check(conn) == 0


def test_cash_outs_compare_what_you_got_with_holding():
    conn = db()
    conn.executemany("INSERT INTO markets (venue, market_id, yes_value) VALUES ('kalshi', ?, ?)",
                     [("W", ONE), ("L", 0), ("P", None)])
    rows = [  # bet_id, market, outcome, status, exit_qty, exit_proceeds, exit_fees, pnl
        ("c1", "W", "yes", "closed_early", 10, 6_000_000, 100_000, 1_900_000),   # got 5.9; holding paid 10
        ("c2", "L", "yes", "closed_early", 10, 3_000_000, 100_000, -1_100_000),  # holding paid 0: helped 2.9
        ("c3", "L", "no", "settled", 4, 2_000_000, 0, 5_000_000),                # partial; NO side won: held 4
        ("c4", "P", "yes", "closed_early", 10, 1_000_000, 0, 500_000),           # pending
        ("c5", "W", "yes", "settled", 0, 0, 0, 5_000_000),                       # never sold: not a cash-out
    ]
    for b, m, o, s, xq, xp, xf, pnl in rows:
        conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, entry_qty, avg_entry,
                        stake, exit_qty, exit_proceeds, exit_fees, payout, realized_pnl, status, closed_ts)
                        VALUES (?, 'kalshi', ?, ?, 0, 't', 10, 500000, 5000000, ?, ?, ?, 0, ?, ?, ?)""",
                     (b, m, o, xq, xp, xf, pnl, s, f"2026-09-0{b[1]}T00:00:00.000000Z"))
    c = habits.cash_outs(conn)
    got = {r["bet_id"]: r for r in c["rows"]}
    assert set(got) == {"c1", "c2", "c3", "c4"}
    assert got["c1"]["value"] == 5_900_000 - 10_000_000 and got["c1"]["outcome"] == "would have won"
    assert got["c2"]["value"] == 2_900_000 and got["c3"]["partial"] and got["c3"]["value"] == 2_000_000 - 4_000_000
    assert got["c4"]["value"] is None and got["c4"]["outcome"] == "pending"
    s = c["summary"]["all"]
    assert (s["n"], s["known"], s["green"], s["would_win"]) == (4, 3, 2, 2)
    assert s["value"] == -4_100_000 + 2_900_000 - 2_000_000 and s["fees"] == 200_000
    assert c["summary"]["parlays"]["n"] == 0


def test_open_bet_with_a_partial_exit_is_a_cash_out_without_a_close_time():
    conn = db()
    conn.execute("INSERT INTO markets (venue, market_id) VALUES ('kalshi', 'M')")
    conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, entry_qty, avg_entry, stake,
                    exit_qty, exit_proceeds, exit_fees, payout, realized_pnl, status, closed_ts)
                    VALUES ('o1', 'kalshi', 'M', 'yes', 0, 't', 10, 500000, 5000000, 4, 2400000, 0, 0, NULL, 'open', NULL)""")
    r = habits.cash_outs(conn)["rows"][0]
    assert r["partial"] and r["closed_ts"] is None and r["outcome"] == "pending"


def test_bankroll_series_per_venue():
    conn = db()
    conn.executemany("INSERT INTO cash_ledger VALUES (?, ?, ?, 'deposit', ?)",
                     [("kalshi", "d1", "2026-09-01T00:00:00.000000Z", 50 * ONE),
                      ("polymarket", "d2", "2026-09-02T00:00:00.000000Z", 20 * ONE)])
    assert [b for _, b, _ in habits.bankroll_series(conn, ("kalshi",))] == [50 * ONE]
    assert [b for _, b, _ in habits.bankroll_series(conn)] == [50 * ONE, 70 * ONE]


def cost(conn, venue, fill_id, bet_id, role, qty, c, note=None):
    conn.execute("INSERT INTO fill_costs VALUES (?, ?, ?, ?, ?, NULL, ?, ?)", (venue, fill_id, bet_id, role, qty, c, note))


def test_spreads_per_venue_and_type_count_each_fill_once():
    conn = db()
    bet(conn, "S", "t", 1)                                  # no start: never in play
    bet(conn, "L", "t", 1)
    conn.execute("UPDATE bets SET is_live = 1 WHERE bet_id = 'L'")
    bet(conn, "X", "t", 1, is_parlay=1, venue="polymarket")
    fill(conn, "kalshi", "3", 1, 500_000, 0)                # fill_id kalshi3, ts 't'
    cost(conn, "kalshi", "f1", "S", "entry", 10, 10_000)
    cost(conn, "kalshi", "f2", "S", "exit", 10, 20_000)
    cost(conn, "kalshi", "f2", "L", "entry", 5, 40_000)     # flip: f2 counted once
    cost(conn, "kalshi", "kalshi3", "S", "entry", 1, None, "no quote")
    cost(conn, "polymarket", "p1", "X", "entry", 4, 50_000)
    got = habits.spreads(conn)
    rows = {(r[0], r[1]): r[2:] for r in got["rows"]}
    assert rows[("kalshi", "singles")] == (300_000, 200_000, 2.0, 2, 3)
    assert rows[("polymarket", "parlays")] == (200_000, 0, 5.0, 1, 1)
    assert habits.unpriced_fills(conn) == [("S", "t", "entry", "no quote")]


def test_cash_outs_carry_exit_spread_and_fair_exit():
    conn = db()
    conn.execute("INSERT INTO markets (venue, market_id, yes_value) VALUES ('kalshi', 'W', 1000000)")
    conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, entry_qty, avg_entry,
                    stake, exit_qty, exit_proceeds, exit_fees, payout, realized_pnl, status, closed_ts,
                    exit_spread_usd) VALUES ('c1', 'kalshi', 'W', 'yes', 0, 't', 10, 500000, 5000000, 10, 6000000,
                    0, 0, 1000000, 'closed_early', '2026-09-01T00:00:00.000000Z', 200000)""")
    c = habits.cash_outs(conn)
    r = c["rows"][0]
    assert (r["spread"], r["fair_exit"]) == (200_000, 620_000)    # sold at .60, 2c under mid
    assert (c["summary"]["all"]["spread"], c["summary"]["all"]["spread_known"]) == (200_000, 1)


def test_spread_timing_is_judged_per_fill_so_in_game_exits_of_pregame_bets_are_live():
    conn = db()
    bet(conn, "B", "2026-09-01T09:00:00Z", 1, start="2026-09-01T10:00:00.000000Z")   # placed pregame
    for fid, ts in (("in", "2026-09-01T09:00:00.12345Z"), ("out", "2026-09-01T11:00:00Z")):
        conn.execute("INSERT INTO fills VALUES ('kalshi', ?, 'o', 'B', 'yes', 10, 500000, 0, 1, ?)", (fid, ts))
    cost(conn, "kalshi", "in", "B", "entry", 10, 10_000)
    cost(conn, "kalshi", "out", "B", "exit", 10, -30_000)     # cashed out in play
    got = habits.spreads(conn)
    assert got["timing"] == {"pregame": 1.0, "live": -3.0}
    assert [r[1:] for r in got["rows"]] == [("singles", 100_000, 0, 1.0, 1, 1), ("singles in play", None, -3.0, -3.0, 1, 1)]
    conn.execute("""UPDATE bets SET exit_qty = 10, exit_proceeds = 5000000, status = 'closed_early', realized_pnl = 0,
                    closed_ts = '2026-09-01T11:00:00Z', exit_spread_usd = -300000 WHERE bet_id = 'B'""")
    s = habits.cash_outs(conn)["summary"]["all"]
    assert (s["spread"], s["spread_known"], s["in_play"]) == (0, 0, 1)    # in-play single cash-out left out of the total


def test_line_shop_summary():
    conn = db()
    rows = [("polymarket", "polymarket:a:1", "kalshi", "K-A", "yes", 0, 10.0, 330_000, 320_000, 310_000, 10_000, 10_000, None),
            ("polymarket", "polymarket:b:1", "kalshi", "K-B", "no", 0, 20.0, 500_000, 510_000, 500_000, 10_000, -10_000, None),
            ("polymarket", "polymarket:c:1", "kalshi", "K-C", "yes", 1, 5.0, 400_000, 380_000, 370_000, 10_000, 20_000, None),
            ("kalshi", "kalshi:d:1", "polymarket", None, None, 0, 1.0, 500_000, None, None, None, None, "no quote")]
    conn.executemany("INSERT INTO line_shop VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    s = habits.line_shop(conn)
    pm = next(r for r in s["rows"] if r[0] == "polymarket")
    assert pm[1:4] == (2, 2, 100_000)       # 2 of 2 pregame priced; $0.10 left (10 contracts x 1c)
    assert pm[4] == 0.0 and pm[6] == 1       # mean gap 0c; Kalshi cheaper on 1
    assert next(r for r in s["rows"] if r[0] == "kalshi")[1:3] == (0, 1)
    assert s["live"] == [("polymarket", 2.0, 1)]
    assert s["unmatched"] == [("kalshi:d:1", "no quote")]
