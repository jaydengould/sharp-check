"""Read-only probe for the Polymarket price-history outage seen 2026-10-05 (docs/polymarket-us.md).

Resolved markets returned empty history while open markets were fine. Run once per session until explained:
    python scripts/pm_history_check.py
Prints counts and public quote flags only, never balances or keys.
"""
import json
import time

from sharp_check import archive, clients

PINNED = 10_000


def points(market, end):
    body = archive.fetch_close("polymarket", market, end, None, clients.gateway_get)
    return body.get("history") or []


def pinned(p):
    return float(p["longPrice"]) <= PINNED / 1e6 or 1 - float(p["shortPrice"]) >= 1 - PINNED / 1e6


def main():
    conn = archive.connect()
    pages = [json.loads(b) for (b,) in conn.execute(
        "SELECT body_json FROM raw_pages WHERE venue = 'polymarket' AND endpoint = ? ORDER BY id",
        (archive.CLOSE_ENDPOINTS["polymarket"],))][::20]
    resolved = [(len(b.get("history") or []), len(points(b["market"], b["start_ts"]))) for b in pages]
    back = sum(now > 0 for _, now in resolved)
    print(f"resolved markets: {back} of {len(resolved)} return history again (stored, now): {resolved}")

    now = int(time.time())
    listing = clients.gateway_get("/v1/markets", {"limit": 20, "active": "true", "closed": "false"}).json()
    opened = [m["slug"] for m in listing.get("markets") or [] if m.get("status") == "MARKET_STATUS_OPEN"][:3]
    print(f"open markets, points in the last hour: {[len(points(s, now)) for s in opened]}")

    # Only meaningful once resolved history is back: does Polymarket keep in-game points, and what does a decided
    # parlay leg look like (pinned near 0/1, or no points)? Rule 2 in the spec's parlay-exit section depends on it.
    live = conn.execute("""SELECT b.market_id, MIN(f.ts), b.start_time FROM bets b
                           JOIN fills f ON f.venue = b.venue AND f.market_id = b.market_id
                           WHERE b.venue = 'polymarket' AND b.is_parlay = 0 AND b.is_live = 1 GROUP BY b.bet_id""").fetchall()
    got = [(round((archive.epoch(ts) - archive.epoch(s)) / 3600, 1), len(points(m, archive.epoch(ts) // 60 * 60 - 60)))
           for m, ts, s in live]
    print(f"live singles (hours after start, points before first fill): {got}")
    legs = archive.parlay_clv_legs(conn)
    for bet_id, market, outcome in conn.execute(
            "SELECT bet_id, market_id, outcome FROM bets WHERE venue = 'polymarket' AND is_parlay = 1 AND exit_qty > 0"):
        reason, _, bet_legs = legs[bet_id]
        if reason:
            continue
        (ts,) = conn.execute("SELECT MIN(ts) FROM fills WHERE venue = 'polymarket' AND market_id = ? AND outcome != ?",
                             (market, outcome)).fetchone()
        key = archive.epoch(ts) // 60 * 60 - 60
        rows = []
        for _, leg, _, start in bet_legs:
            hours = round((key - archive.epoch(start)) / 3600, 1)
            pts = points(leg, key) if hours > 0 else []
            rows.append((hours, len(pts), bool(pts) and pinned(pts[-1])))
        print(f"parlay exit legs (hours after start, points, latest pinned): {rows}")

    print("VERDICT:", "history is back" if resolved and back == len(resolved)
          else "still empty for resolved markets" if not back else "partial")


if __name__ == "__main__":
    main()
