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
