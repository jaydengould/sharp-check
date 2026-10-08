"""Headline metrics from bets. Run: python -m pytest tests/  (no network)."""
from sharp_check import archive, metrics, normalize


def bet(conn, n, is_parlay, status, stake, pnl, avg_entry, payout, venue="kalshi", market="M", outcome="yes", closed=None):
    conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, entry_qty, avg_entry,
                    stake, exit_qty, exit_proceeds, exit_fees, payout, realized_pnl, status, closed_ts)
                    VALUES (?, ?, ?, ?, ?, 't', 1, ?, ?, 0, 0, 0, ?, ?, ?, ?)""",
                 (f"b{n}", venue, market, outcome, is_parlay, avg_entry, stake, payout, pnl, status, closed))


def market(conn, mid, label_yes=None, label_no=None, yes_value=None, title=None, venue="kalshi"):
    conn.execute("INSERT INTO markets (venue, market_id, title, label_yes, label_no, yes_value) VALUES (?, ?, ?, ?, ?, ?)",
                 (venue, mid, title, label_yes, label_no, yes_value))


def test_headline_scores_pnl_on_closed_bets_and_wins_on_settled_only():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    bet(conn, 1, 0, "settled", 400_000, 600_000, 400_000, 1_000_000)   # won
    bet(conn, 2, 0, "settled", 500_000, -500_000, 500_000, 0)          # lost
    bet(conn, 3, 0, "closed_early", 200_000, 100_000, 200_000, 0)      # P&L only
    bet(conn, 4, 1, "settled", 100_000, -100_000, 100_000, 0)          # parlay
    bet(conn, 5, 0, "open", 300_000, None, 300_000, 0)                 # excluded

    s = metrics.headline(conn, ("kalshi",), "singles")
    assert (s["bets"], s["open"], s["stake"], s["pnl"]) == (3, 1, 1_100_000, 200_000)
    assert (s["wins"], s["settled"]) == (1, 2)
    assert abs(s["expected"] - 0.9) < 1e-9
    assert abs(s["ci"][1] - s["expected"] - 1.96 * (0.4 * 0.6 + 0.5 * 0.5) ** 0.5) < 1e-9
    assert metrics.headline(conn, ("kalshi",), "parlays")["bets"] == 1
    assert metrics.headline(conn, ("polymarket",))["roi"] is None


def test_segments_hide_small_slices_and_linkage_lists_unlinked():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    for i in range(30):
        bet(conn, i, 0, "settled", 100_000, -100_000, 500_000, 0)
    for i in range(30, 35):
        bet(conn, i, 1, "settled", 100_000, -100_000, 500_000, 0)
    conn.execute("UPDATE bets SET market_type = CASE is_parlay WHEN 1 THEN 'parlay' ELSE 'prop' END, link = 'game'")
    conn.execute("UPDATE bets SET link = 'unlinked' WHERE bet_id = 'b31'")

    shown, hidden = metrics.segments(conn, "market_type")
    assert [(v, h["bets"]) for v, h in shown] == [("prop", 30)] and hidden == 1
    counts, unlinked = metrics.linkage(conn)
    assert counts == {"game": 34, "unlinked": 1} and [u[0] for u in unlinked] == ["b31"]


def test_clv_aggregates_bets_with_a_close_and_lists_missing():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    for n, (clv, status, pnl) in enumerate([(20_000, "settled", 600_000), (-10_000, "settled", -400_000),
                                            (None, "settled", 100_000), (30_000, "open", None)]):
        bet(conn, n, 0, status, 400_000, pnl, 400_000, 0)
        conn.execute("UPDATE bets SET link = 'game', is_live = 0, market_type = 'prop', start_time = 'S',"
                     " clv = ?, clv_usd = ?, clv_net = ?, clv_move = ? WHERE bet_id = ?",
                     (clv, None if clv is None else clv * 10, None if clv is None else clv - 10_000,
                      None if clv is None else clv + 5_000, f"b{n}"))
    c = metrics.clv(conn)
    assert (c["eligible"], c["with_close"], c["clv_usd"], c["pnl"]) == (4, 3, 100_000, 200_000)
    assert abs(c["mean_cents"] - 4 / 3) < 1e-9 and abs(c["beat"] - 2 / 3) < 1e-9
    assert c["ci"][0] < c["mean_cents"] < c["ci"][1]
    assert abs(c["net_cents"] - 1 / 3) < 1e-9 and c["net_ci"][0] < c["net_cents"] < c["net_ci"][1]
    assert c["net_usd"] == -10_000  # closed bets, qty 1
    assert abs(c["move_cents"] - 11 / 6) < 1e-9 and abs(c["move_beat"] - 2 / 3) < 1e-9
    assert c["missing"] == [("b2", "S")]
    assert metrics.clv(conn, ("polymarket",))["mean_cents"] is None


def test_parlay_clv_aggregates_breakdown_and_reasons():
    from test_archive import parlay_with_quotes
    conn = parlay_with_quotes()
    c = metrics.parlay_clv(conn)
    assert (c["eligible"], c["with_clv"], c["clv_usd"], c["pnl"]) == (2, 1, 170_000, -260_000)
    assert (c["mean_cents"], c["net_cents"], c["markup_cents"], c["move_cents"]) == (17.0, 16.0, -5.0, 12.0)
    assert abs(c["markup_pct"] - (250_000 - 300_000) / 300_000) < 1e-12
    assert c["excluded"] == [("FU", "leg without a game"), ("P2", "no usable leg quote"), ("SG", "same-game legs")]
    later = metrics.parlay_clv(conn, now="2026-09-01T12:00:00Z")  # P2's second leg hasn't kicked off yet
    assert ("P2", "waiting for kickoff") in later["excluded"]
    assert [r[1] for r in metrics.parlay_bets(conn)] == ["P1"]
    assert metrics.clv_bets(conn) == []  # singles table stays singles-only


def test_wins_judge_cash_outs_by_how_their_market_finished():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    market(conn, "W", yes_value=1_000_000)
    market(conn, "L", yes_value=0)
    market(conn, "P")                                                                         # unresolved
    bet(conn, 1, 0, "closed_early", 300_000, 200_000, 300_000, 0, market="W")                 # would have won
    bet(conn, 2, 0, "closed_early", 400_000, 100_000, 400_000, 0, market="L")                 # green, would have lost
    bet(conn, 3, 0, "closed_early", 500_000, -100_000, 500_000, 0, market="L", outcome="no")  # NO side: would have won
    bet(conn, 4, 0, "closed_early", 200_000, -50_000, 200_000, 0, market="P")                 # unknown: excluded
    bet(conn, 5, 0, "settled", 600_000, -600_000, 600_000, 0, market="L")                     # held, lost
    h = metrics.headline(conn)
    assert (h["settled"], h["wins"]) == (4, 2)
    assert abs(h["expected"] - 1.8) < 1e-9
    assert h["bets"] == 5 and h["pnl"] == -450_000  # P&L still covers every closed bet


def test_headline_segment_filter_still_works_with_the_markets_join():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    market(conn, "M")
    conn.execute("UPDATE markets SET market_type = 'moneyline'")
    bet(conn, 1, 0, "settled", 100_000, -100_000, 500_000, 0)
    conn.execute("UPDATE bets SET market_type = 'prop'")
    assert metrics.headline(conn, where=("market_type", "prop"))["bets"] == 1


def test_labels_for_singles_and_parlays_on_both_venues():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    market(conn, "GAME", label_yes="LSU", label_no="Ole Miss", venue="polymarket")
    market(conn, "TD", label_yes="Kyle Monangai 1+ touchdowns", venue="polymarket")
    market(conn, "caoc-1", venue="polymarket")
    market(conn, "leg-nolookup", title="NY Jets vs. CHI Bears", venue="polymarket")
    market(conn, "KXMVE-1", label_yes="yes Joey Cantillo: 8+,no Purdue wins by over 10.5 Points")
    conn.executemany("INSERT INTO parlay_legs VALUES (?, ?, ?, ?)", [
        ("polymarket", "caoc-1", "TD", "no"), ("polymarket", "caoc-1", "leg-nolookup", "yes"),
        ("polymarket", "caoc-1", "GAME", "no"),
        ("kalshi", "KXMVE-1", "A", "yes"), ("kalshi", "KXMVE-1", "B", "no")])
    bet(conn, 1, 0, "settled", 1, 0, 1, 0, venue="polymarket", market="GAME", outcome="no")
    bet(conn, 2, 0, "settled", 1, 0, 1, 0, venue="polymarket", market="TD", outcome="no")
    bet(conn, 3, 1, "settled", 1, 0, 1, 0, venue="polymarket", market="caoc-1")
    bet(conn, 4, 1, "settled", 1, 0, 1, 0, market="KXMVE-1")
    got = metrics.labels(conn)
    assert got["b1"] == ("Ole Miss", [])
    assert got["b2"] == ("No: Kyle Monangai 1+ touchdowns", [])
    assert got["b3"] == ("3-leg parlay", ["No: Kyle Monangai 1+ touchdowns", "NY Jets vs. CHI Bears", "Ole Miss"])
    assert got["b4"] == ("2-leg parlay", ["Joey Cantillo: 8+", "No: Purdue wins by over 10.5 Points"])


def test_recent_and_open_bets():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    market(conn, "W", label_yes="A", yes_value=1_000_000)
    for i in range(12):
        bet(conn, i, 0, "settled", 100_000, 900_000 if i % 2 else -100_000, 100_000, 1_000_000 if i % 2 else 0,
            market="W", closed=f"2026-09-{10 + i:02d}T00:00:00.000000Z")
    bet(conn, 20, 0, "closed_early", 100_000, 50_000, 100_000, 0, market="W", closed="2026-09-30T00:00:00.000000Z")
    bet(conn, 21, 0, "open", 100_000, None, 100_000, 0, market="W")
    recent = metrics.recent_bets(conn)
    assert len(recent) == 10 and recent[0]["bet_id"] == "b20"
    assert recent[0]["result"] == "cashed out · would have won" and recent[1]["result"] == "won"
    assert [b["bet_id"] for b in metrics.open_bets(conn)] == ["b21"]


def test_recent_and_open_bets_filter_by_venue():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    bet(conn, 1, 0, "settled", 1, 0, 1, 0, closed="2026-09-01T00:00:00.000000Z")
    bet(conn, 2, 0, "settled", 1, 0, 1, 0, venue="polymarket", closed="2026-09-02T00:00:00.000000Z")
    bet(conn, 3, 0, "open", 1, None, 1, 0, venue="polymarket")
    assert [b["bet_id"] for b in metrics.recent_bets(conn, venues=("kalshi",))] == ["b1"]
    assert [b["bet_id"] for b in metrics.recent_bets(conn)] == ["b2", "b1"]
    assert metrics.open_bets(conn, ("kalshi",)) == [] and len(metrics.open_bets(conn, ("polymarket",))) == 1


def test_all_bets_include_open_and_filter_by_venue_and_split():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    market(conn, "TD", label_yes="Jahdae Walker 1+ touchdowns", yes_value=1_000_000, venue="polymarket")
    bet(conn, 1, 0, "settled", 5_000_000, 89_600_000, 50_000, 94_600_000, venue="polymarket", market="TD",
        closed="2026-09-13T20:19:29.193580Z")
    bet(conn, 2, 1, "open", 1, None, 1, 0)
    conn.execute("UPDATE bets SET opened_ts = CASE bet_id WHEN 'b1' THEN '2026-09-13T17:26:43.007931Z'"
                 " ELSE '2026-10-01T00:00:00.000000Z' END")
    rows = metrics.all_bets(conn)
    assert [r["bet_id"] for r in rows] == ["b2", "b1"] and rows[0]["result"] == "open"
    assert [r["label"] for r in metrics.all_bets(conn, ("polymarket",), "singles")] == ["Jahdae Walker 1+ touchdowns"]
    assert metrics.all_bets(conn, ("kalshi",), "singles") == []


def test_mean_ci_clusters_by_group():
    import statistics
    xs = [1.0, 3.0, -2.0, 0.5]
    assert metrics.mean_ci(xs, ["a", "b", "c", "d"]) == metrics.mean_ci(xs)  # one per group = plain formula
    assert abs(metrics.mean_ci(xs)[1] - 1.96 * statistics.stdev(xs) / 2) < 1e-12
    dup = [1.0, 1.0, -1.0, -1.0, 2.0]
    assert metrics.mean_ci(dup, ["g1", "g1", "g2", "g2", "g3"])[1] > metrics.mean_ci(dup)[1]  # same-game pairs widen it
    assert metrics.mean_ci([1.0, 2.0], ["g", "g"]) == (1.5, None)  # one group: no interval
    cancel = [1.0, -1.0, 1.0, -1.0]  # opposite moves within each game: clustered is narrower, plain wins
    assert metrics.mean_ci(cancel, ["g1", "g1", "g2", "g2"]) == metrics.mean_ci(cancel)


def test_parlay_groups_chain_through_shared_games():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    legs = {"A": ("g1", "g2"), "B": ("g2", "g3"), "C": ("g3", "g4"), "D": ("g5", "g6")}
    for n, (pid, games) in enumerate(legs.items()):
        bet(conn, n, 1, "settled", 100_000, 0, 100_000, 0, market=pid)
        for g in games:
            leg = f"{pid}-{g}"  # different markets on a shared game still link
            conn.execute("INSERT INTO parlay_legs (venue, market_id, leg_market_id, leg_outcome) VALUES ('kalshi', ?, ?, 'yes')", (pid, leg))
            conn.execute("INSERT INTO markets (venue, market_id, game_id) VALUES ('kalshi', ?, ?)", (leg, g))
    g = metrics.parlay_groups(conn)
    assert g["b0"] == g["b1"] == g["b2"] != g["b3"]
