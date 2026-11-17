"""Computed cash from normalized tables. Run: python -m pytest tests/  (no network)."""
from sharp_check import archive, normalize, reconcile


def fill(conn, fill_id, market, outcome, qty, price, fee):
    conn.execute("INSERT INTO fills VALUES ('kalshi', ?, 'o', ?, ?, ?, ?, ?, 1, '2026-01-01T00:00:00.000000Z')",
                 (fill_id, market, outcome, qty, price, fee))


def test_computed_cash_nets_pairs_pays_settlements_and_reports_open():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    conn.execute("INSERT INTO cash_ledger VALUES ('kalshi', 'd1', 't', 'deposit', 100000000)")
    # M1: buy 10 YES @0.40, then sell 4 of them @0.30 (stored as acquire 4 NO @0.70). Settles YES.
    fill(conn, "f1", "M1", "yes", 10, 400_000, 100_000)
    fill(conn, "f2", "M1", "no", 4, 700_000, 50_000)
    conn.execute("INSERT INTO settlements VALUES ('kalshi', 'M1', 1000000, 't')")
    # M2: buy 5 NO @0.20, unsettled -> open position, its cost is out of cash.
    fill(conn, "f3", "M2", "no", 5, 200_000, 0)

    cash, open_markets = reconcile.computed_cash(conn, "kalshi")
    # 100 - 4.10 (buy) + 1.15 (sale proceeds net of fee) + 6 (6 YES pay $1) - 1.00 (M2 cost)
    assert cash == 102_050_000
    assert open_markets == ["M2"]


def test_flat_unsettled_market_returns_its_pairs_as_cash():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    fill(conn, "f1", "M1", "yes", 2.1, 400_000, 0)  # floats that don't sum exactly
    fill(conn, "f2", "M1", "no", 2.1, 650_000, 0)
    cash, open_markets = reconcile.computed_cash(conn, "kalshi")
    assert cash == round(2.1 * (1_000_000 - 400_000 - 650_000))  # sold at 0.35 after buying at 0.40
    assert open_markets == []


def novig_rebuilt():
    """Synthetic Novig account: one settled and one open cash straight, plus credit-bet cash (untracked_bet)."""
    import json as _json
    from pathlib import Path as _Path
    from sharp_check import archive as _archive, normalize as _normalize
    conn = _archive.connect(":memory:")
    for endpoint, params, body in _json.loads((_Path(__file__).parent / "fixtures" / "novig.json").read_text()):
        _archive.store(conn, "novig", endpoint, params, body)
    _normalize.rebuild(conn)
    return conn


def test_novig_reconciles_and_ledger_matches_balance():
    from sharp_check import habits, metrics
    conn = novig_rebuilt()
    assert reconcile.reported_cash(conn, "novig") == 84_420_000
    assert reconcile.novig_ledger_gap(conn) == 0
    assert reconcile.computed_cash(conn, "novig")[0] == 84_420_000
    assert reconcile.bets_cash(conn, "novig") == 84_420_000
    # own money = balance + open cost (2.06) + withdrawals (20) - deposits (100)
    assert metrics.own_money(conn, "novig") == 6_480_000
    assert habits.untracked_fees(conn) == 20_000


def test_novig_untracked_cash_moves_bankroll_and_pnl():
    from sharp_check import habits
    conn = novig_rebuilt()
    series = habits.bankroll_series(conn, ("novig",))
    assert series[-1][1:] == (86_480_000, 6_480_000)  # bankroll = cash + open cost; P&L = own money here


def test_venues_without_a_balance_are_skipped():
    conn = archive.connect(":memory:")
    normalize.rebuild(conn)
    archive.store(conn, "kalshi", "/portfolio/balance", {}, {"balance_dollars": "5.00"})
    assert reconcile.balanced(conn) == ("kalshi",)
