"""Phase 2 check: cash rebuilt from normalized tables must match each venue's reported cash balance.

Run: python -m sharp_check.reconcile   (after archive + normalize)
Prints only the difference, never balances.
"""
import json
from decimal import Decimal

from sharp_check import archive
from sharp_check.normalize import ONE, micros

TOLERANCE = 50_000  # 5 cents of rounding across fractional fills


def computed_cash(conn, venue):
    """Ledger + fill cash flows + settlement payouts. Returns (micro-dollars, unsettled markets with a position).

    A YES + NO pair in one market is always worth $1, so matched pairs count as $1 whether settled or not.
    """
    cash = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM cash_ledger WHERE venue = ?", (venue,)).fetchone()[0]
    open_markets = []
    for market, yes_q, no_q, spent, yes_value in conn.execute("""
        SELECT f.market_id,
               SUM(CASE f.outcome WHEN 'yes' THEN f.qty ELSE 0 END),
               SUM(CASE f.outcome WHEN 'no' THEN f.qty ELSE 0 END),
               SUM(f.qty * f.price + f.fee),
               s.yes_value
        FROM fills f LEFT JOIN settlements s USING (venue, market_id)
        WHERE f.venue = ?
        GROUP BY f.market_id ORDER BY f.market_id""", (venue,)):
        yes_q, no_q = round(yes_q, 4), round(no_q, 4)
        pairs = min(yes_q, no_q)
        cash += pairs * ONE - spent
        if yes_value is not None:
            cash += (yes_q - pairs) * yes_value + (no_q - pairs) * (ONE - yes_value)
        elif yes_q != no_q:
            open_markets.append(market)
    return round(cash), open_markets


def bets_cash(conn, venue):
    """Same cash rebuilt from bets instead of fills: proves every fill and payout landed in exactly one bet."""
    return conn.execute("""
        SELECT (SELECT COALESCE(SUM(amount), 0) FROM cash_ledger WHERE venue = :v)
             + COALESCE(SUM(exit_proceeds + payout - stake - exit_fees), 0)
        FROM bets WHERE venue = :v""", {"v": venue}).fetchone()[0]


def reported_cash(conn, venue):
    """Cash from the latest archived balance snapshot."""
    endpoint = {"kalshi": "/portfolio/balance", "polymarket": "/v1/account/balances"}[venue]
    row = conn.execute("SELECT body_json FROM raw_pages WHERE venue = ? AND endpoint = ? ORDER BY id DESC LIMIT 1",
                       (venue, endpoint)).fetchone()
    body = json.loads(row[0])
    if venue == "kalshi":
        return micros(body["balance_dollars"])
    return micros(Decimal(str(body["balances"][0]["currentBalance"])))  # float in the API


if __name__ == "__main__":
    conn = archive.connect()
    for venue in ("kalshi", "polymarket"):
        cash, open_markets = computed_cash(conn, venue)
        reported = reported_cash(conn, venue)
        for label, computed in (("fills", cash), ("bets", bets_cash(conn, venue))):
            diff = computed - reported
            verdict = "OK" if abs(diff) <= TOLERANCE else "MISMATCH"
            print(f"{venue:10} {verdict:8} {label:5} - reported = {diff / ONE:+.2f} USD; open positions: {len(open_markets)}")
