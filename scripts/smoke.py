"""Phase 1 smoke test. Read-only GETs against both venues.

Prints status codes, field names, counts and date ranges. Never prints secrets.
Balance/account fields are masked before anything is printed or saved.
Writes the first few records of each endpoint to tests/fixtures/ (scrubbed).
"""
import json
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sharp_check.clients import gateway_get, kalshi_get, kalshi_pages, pm_get, pm_pages  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"

# Keys whose values are masked in printed samples and fixtures.
SCRUB = re.compile(r"(^id$|_id$|Id$|account|user|participant|balance|buyingpower|transactionid|subaccount)", re.I)


def scrub(obj):
    if isinstance(obj, dict):
        return {k: "<scrubbed>" if SCRUB.search(k) and not isinstance(v, (dict, list)) else scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return obj


def save_fixture(name, records):
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / f"{name}.json").write_text(json.dumps(scrub(records[:3]), indent=2))


def keys(obj, prefix=""):
    """Flattened field paths of a JSON object, for printing shapes without values."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.append(prefix + k)
            out += keys(v, prefix + k + ".")
    elif isinstance(obj, list) and obj:
        out += keys(obj[0], prefix + "[]")
    return out


def section(title):
    print(f"\n=== {title} ===")


# --- pagination ----------------------------------------------------------------

def kalshi_all(path, list_key, params=None):
    try:
        return [x for _, body in kalshi_pages(path, params, limit=1000 if "fills" in path else 100) for x in body.get(list_key, [])]
    except requests.HTTPError as e:
        print(f"  {path}: {e}")
        return []


def pm_all(path, list_key, params=None):
    items = []
    try:
        for _, body in pm_pages(path, params):
            page = body.get(list_key, [])
            items += list(page.values()) if isinstance(page, dict) else page
    except requests.HTTPError as e:
        print(f"  {path}: {e}")
    return items


def date_range(items, field):
    vals = sorted(str(i[field]) for i in items if i.get(field))
    return f"{vals[0]} .. {vals[-1]}" if vals else "n/a"


# --- Kalshi ----------------------------------------------------------------------

def smoke_kalshi():
    section("Kalshi")
    print("balance:", kalshi_get("/portfolio/balance").status_code)

    r = kalshi_get("/historical/cutoff")
    print("historical cutoff:", r.status_code, r.json() if r.ok else r.text[:200])

    live = kalshi_all("/portfolio/fills", "fills")
    hist = kalshi_all("/historical/fills", "fills")
    print(f"fills live={len(live)} historical={len(hist)}")
    print("  live range:", date_range(live, "created_time"))
    print("  historical range:", date_range(hist, "created_time"))
    fills = live + hist
    if fills:
        print("  fill fields:", keys(fills[0]))
        save_fixture("kalshi_fills", fills)
        print("  distinct markets:", len({f["ticker"] for f in fills}))

    settlements = kalshi_all("/portfolio/settlements", "settlements")
    print(f"settlements={len(settlements)} range:", date_range(settlements, "settled_time"))
    if settlements:
        print("  settlement fields:", keys(settlements[0]))
        save_fixture("kalshi_settlements", settlements)

    for path, key in [("/portfolio/deposits", "deposits"), ("/portfolio/withdrawals", "withdrawals")]:
        r = kalshi_get(path)
        n = len(r.json().get(key, [])) if r.ok else "-"
        print(f"{path}: HTTP {r.status_code} first-page count={n}", "" if r.ok else r.text[:200])
        if r.ok and r.json().get(key):
            print("  fields:", keys(r.json()[key][0]))

    if fills:
        ticker = fills[0]["ticker"]
        r = kalshi_get(f"/markets/{ticker}")
        if r.status_code == 404:
            r = kalshi_get(f"/historical/markets/{ticker}")
        print(f"market {ticker}: HTTP {r.status_code}")
        if r.ok:
            m = r.json().get("market", r.json())
            save_fixture("kalshi_market", [m])
            print("  time-like fields:", {k: v for k, v in m.items() if "time" in k or "date" in k or k.endswith("_ts")})
            print("  other fields:", [k for k in m if not ("time" in k or "date" in k)])
            ev = kalshi_get(f"/events/{m['event_ticker']}")
            print(f"  event {m['event_ticker']}: HTTP {ev.status_code}")
            if ev.ok:
                e = ev.json().get("event", {})
                print("  event fields:", list(e))
                print("  event time-like:", {k: v for k, v in e.items() if "time" in k or "date" in k})
            meta = kalshi_get(f"/events/{m['event_ticker']}/metadata")
            print(f"  event metadata: HTTP {meta.status_code}", keys(meta.json()) if meta.ok else "")


# --- Polymarket US --------------------------------------------------------------

def smoke_polymarket():
    section("Polymarket US")
    print("balances:", pm_get("/v1/account/balances").status_code)

    acts = pm_all("/v1/portfolio/activities", "activities", {"sortOrder": "SORT_ORDER_ASCENDING"})
    by_type = {}
    for x in acts:
        by_type.setdefault(x.get("type"), []).append(x)
    print(f"activities total={len(acts)}", {t: len(v) for t, v in by_type.items()})

    trades = [x["trade"] for x in by_type.get("ACTIVITY_TYPE_TRADE", []) if "trade" in x]
    if trades:
        print("  trade range:", date_range(trades, "createTime"))
        print("  trade fields:", keys(trades[0]))
        print("  sample trades (scrubbed):")
        for t in trades[:3]:
            print("   ", json.dumps(scrub(t)))
        save_fixture("pm_trades", trades)
    res = [x["positionResolution"] for x in by_type.get("ACTIVITY_TYPE_POSITION_RESOLUTION", []) if "positionResolution" in x]
    if res:
        print("  resolution fields:", keys(res[0]))
        print("  sample resolution (scrubbed):", json.dumps(scrub(res[0])))
        save_fixture("pm_resolutions", res)
    for t, v in by_type.items():
        if t not in ("ACTIVITY_TYPE_TRADE", "ACTIVITY_TYPE_POSITION_RESOLUTION"):
            print(f"  {t} fields:", keys(v[0]))

    positions = pm_all("/v1/portfolio/positions", "positions")
    print(f"open positions={len(positions)}")
    if positions:
        print("  position fields:", keys(positions[0]))
        save_fixture("pm_positions", positions)

    slug = next((t["marketSlug"] for t in reversed(trades) if not t["marketSlug"].startswith("caoc-")), None)  # combos 404 on market lookup
    if slug:
        r = gateway_get(f"/v1/market/slug/{slug}")
        print(f"market {slug}: HTTP {r.status_code}")
        if r.ok:
            m = r.json().get("market", r.json())
            save_fixture("pm_market", [m])
            print("  market fields:", list(m))
            print("  market time-like:", {k: v for k, v in m.items() if "time" in k.lower() or "date" in k.lower()})
            print("  marketSides:", json.dumps(m.get("marketSides"))[:600])
        event_slug = next((r_["afterPosition"]["marketMetadata"]["eventSlug"] for r_ in res
                           if r_.get("marketSlug") == slug), None) or (r.json().get("market", {}).get("eventSlug") if r.ok else None)
        if event_slug:
            e = gateway_get(f"/v1/events/slug/{event_slug}")
            print(f"event {event_slug}: HTTP {e.status_code}")
            if e.ok:
                ev = e.json().get("event", e.json())
                save_fixture("pm_event", [ev])
                print("  event time-like:", {k: v for k, v in ev.items() if "time" in k.lower() or "date" in k.lower()})
                print("  event ids:", {k: ev.get(k) for k in ("gameId", "sportradarGameId", "seriesSlug")})
                start = ev.get("startTime") or ev.get("startDate")
                if start:
                    from datetime import datetime
                    t0 = int(datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp())
                    ph = gateway_get("/v1/price-history", {"symbol": slug, "fidelity": 1,
                                                           "timestamp.startTimestamp": t0 - 3600, "timestamp.endTimestamp": t0 + 600})
                    pts = ph.json().get("history", []) if ph.ok else []
                    print(f"  price history around start: HTTP {ph.status_code} points={len(pts)}", pts[-1:] if pts else ph.text[:200])

    # Institutional executions search: different host + JWT auth. One probe to confirm it's not retail-accessible.
    r = requests.post("https://api.prod.polymarketexchange.com/v1/report/executions/search", json={"pageSize": 1}, timeout=30)
    print("institutional executions search (no auth):", r.status_code)


if __name__ == "__main__":
    smoke_kalshi()
    smoke_polymarket()
