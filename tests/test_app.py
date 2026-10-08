"""Dashboard renders. Run: python -m pytest tests/  (no network)."""
from sharp_check import app, archive, normalize


def ids(node, found=None):
    found = set() if found is None else found
    if getattr(node, "id", None):
        found.add(node.id)
    kids = getattr(node, "children", None)
    for k in kids if isinstance(kids, (list, tuple)) else [] if kids is None else [kids]:
        if hasattr(k, "to_plotly_json"):
            ids(k, found)
    return found


def test_empty_db_renders_every_tab_and_the_tiles():
    conn = archive.connect(":memory:")
    normalize.rebuild(conn)
    page = app.layout(conn)
    assert {"scope", "tiles", "overview-body", "theme", "tabs", "bankroll-overview", "bankroll-overview-data",
            "bankroll-habits", "bankroll-habits-data"} <= ids(page)
    tabs = [t for t in page.children if getattr(t, "id", None) == "tabs"][0].children
    assert [t.label for t in tabs] == ["Overview", "CLV", "Performance", "Bets", "Habits", "Line shop", "Data health"]
    for venues in app.SCOPES.values():
        assert len(app.tiles(conn, venues)) == 5


def test_headers_take_their_columns_alignment():
    t = app.table(("Venue", "P&L", "ROI", "Bets"), [("Kalshi", app.tone("$1.00", 1), "+2.0%", 3), ("PM", "-$3.00", "", 0)])
    head = t.children.children[0].children.children
    assert [th.className for th in head] == [None, "num", "num", "num"]


def test_sections_are_boxed_from_heading_to_heading():
    from dash import html
    out = app.boxed([html.Div("switch"), html.H3("A"), html.P("a"), html.H3("B"), html.P("b")])
    assert out[0].children == "switch"
    assert [(o.className, o.children[0].children) for o in out[1:]] == [("card", "A"), ("card", "B")]


def test_venue_switch_rerenders_the_whole_overview(monkeypatch):
    conn = archive.connect(":memory:")
    normalize.rebuild(conn)
    monkeypatch.setattr(app.archive, "connect", lambda: conn)
    monkeypatch.setattr(app, "closing", lambda c: __import__("contextlib").nullcontext(c))
    tiles, body, fig = app.scope_overview("Kalshi")
    assert len(tiles) == 5 and [c.className for c in body] == ["card", "card"] and "data" in fig


def test_clv_venue_switch_filters_the_bet_lists(monkeypatch):
    conn = archive.connect(":memory:")
    normalize.rebuild(conn)
    conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, entry_qty, avg_entry, stake,
                    exit_qty, exit_proceeds, exit_fees, payout, status, start_time, clv, clv_net, clv_move, close_mid, clv_usd)
                    VALUES ('kalshi:M:1', 'kalshi', 'M', 'yes', 0, 't', 1, 400000, 400000, 0, 0, 0, 0, 'open',
                            '2026-09-01T00:00:00.000000Z', 10000, 5000, 0, 410000, 10000)""")
    monkeypatch.setattr(app.archive, "connect", lambda: conn)
    monkeypatch.setattr(app, "closing", lambda c: __import__("contextlib").nullcontext(c))
    assert app.scope_clv_lists("Kalshi")[0] == "Show all 1 singles with CLV"
    assert app.scope_clv_lists("Polymarket")[0] == "Show all 0 singles with CLV"


def test_bets_tab_search_finds_a_bet_by_name(monkeypatch):
    conn = archive.connect(":memory:")
    normalize.rebuild(conn)
    conn.execute("INSERT INTO markets (venue, market_id, label_yes) VALUES ('polymarket', 'TD', 'Jahdae Walker 1+ touchdowns')")
    conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, entry_qty, avg_entry, stake,
                    exit_qty, exit_proceeds, exit_fees, payout, realized_pnl, status, closed_ts, is_live, sport, market_type)
                    VALUES ('polymarket:TD:1', 'polymarket', 'TD', 'yes', 0, '2026-09-13T17:26:43.007931Z', 94.6, 50000,
                            5000000, 0, 0, 0, 94600000, 89600000, 'settled', '2026-09-13T20:19:29.193580Z', 1, 'football', 'prop')""")
    monkeypatch.setattr(app.archive, "connect", lambda: conn)
    monkeypatch.setattr(app, "closing", lambda c: __import__("contextlib").nullcontext(c))
    assert app.filter_bets("Both", "all", "walker")[0] == "1 bet"
    assert app.filter_bets("Kalshi", "all", "walker")[0] == "0 bets"
    assert app.filter_bets("Both", "parlays", "")[0] == "0 bets"


def text(node):
    if node is None:
        return ""
    if isinstance(node, (list, tuple)):
        return " ".join(text(n) for n in node)
    if not hasattr(node, "children"):
        return str(node)
    return text(node.children)


def test_habits_and_data_health_show_spread():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    conn.execute("""INSERT INTO bets (bet_id, venue, market_id, outcome, is_parlay, opened_ts, is_live, entry_qty,
                    avg_entry, stake, exit_qty, exit_proceeds, exit_fees, payout, status, entry_spread_usd)
                    VALUES ('kalshi:K:1', 'kalshi', 'K', 'yes', 0, '2026-09-01T10:00:00Z', 0, 10, 400000, 4000000,
                    0, 0, 0, 0, 'open', 100000)""")
    conn.execute("INSERT INTO fills VALUES ('kalshi', 'f1', 'o', 'K', 'yes', 10, 400000, 0, 1, '2026-09-01T10:00:00Z')")
    conn.execute("INSERT INTO fills VALUES ('kalshi', 'f2', 'o', 'K', 'yes', 1, 400000, 0, 1, '2026-09-01T10:01:00Z')")
    conn.execute("INSERT INTO fill_costs VALUES ('kalshi', 'f1', 'kalshi:K:1', 'entry', 10, 390000, 10000, NULL)")
    conn.execute("INSERT INTO fill_costs VALUES ('kalshi', 'f2', 'kalshi:K:1', 'entry', 1, NULL, NULL, 'stale quote')")
    habits_text = text(app.habits_tab(conn))
    assert "Spread at entry" in habits_text and "1 of 2" in habits_text
    assert "Fills without a spread" in text(app.data_tab(conn)) and "stale quote" in text(app.data_tab(conn))


def test_line_shop_tab_is_listed_and_renders_without_network():
    conn = archive.connect(":memory:")
    normalize.rebuild(conn)
    page = app.layout(conn)
    tabs = [t for t in page.children if getattr(t, "id", None) == "tabs"][0].children
    assert [t.label for t in tabs] == ["Overview", "CLV", "Performance", "Bets", "Habits", "Line shop", "Data health"]
    assert {"ls-game", "ls-refresh", "ls-body"} <= ids(page)


def test_line_shop_body_marks_the_cheaper_venue_and_thin_books():
    out = {"as_of": "2026-10-10T12:00:00Z", "errors": ["Polymarket unavailable"], "rows": [
        {"group": "Moneyline", "label": "Bears win", "gap": -20_000, "best": "kalshi",
         "kalshi": {"all_in": 700_000, "ask": 690_000, "thin": None},
         "polymarket": {"all_in": 720_000, "ask": 710_000, "thin": 3.2}}]}
    body = app.lineshop_body(out)
    text = str(body)
    assert "Polymarket unavailable" in text and "Bears win" in text and "thin: $3" in text
    assert "best" in text  # the cheaper cell carries the highlight class


def test_habits_has_a_line_shopping_section_on_an_empty_db():
    conn = archive.connect(":memory:")
    normalize.rebuild(conn)
    assert "Line shopping" in str(app.habits_tab(conn))
