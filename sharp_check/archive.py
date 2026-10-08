"""Phase 1 raw archive: page every account endpoint into raw_pages (append-only).

Run: python -m sharp_check.archive
Identical pages are skipped via a content hash, so re-running is cheap and never rewrites history.
"""
import hashlib
import json
import re
import sqlite3
from functools import partial
import sys
from datetime import datetime, timezone
from pathlib import Path

from sharp_check import classify, clients, match

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sharp_check.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_pages (
    id INTEGER PRIMARY KEY,
    venue TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    params TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    body_sha TEXT NOT NULL,
    body_json TEXT NOT NULL,
    UNIQUE (venue, endpoint, body_sha)
);
"""

# (venue, endpoint, pager, page size). Account data only: these are what can disappear.
# Kalshi market records for traded tickers are archived separately by sync_kalshi_markets.
# Balances are point-in-time snapshots with no history endpoint; each changed one is a new page.
ENDPOINTS = [
    ("kalshi", "/historical/cutoff", None, None),
    ("kalshi", "/portfolio/balance", None, None),
    ("kalshi", "/portfolio/fills", clients.kalshi_pages, 1000),
    ("kalshi", "/historical/fills", clients.kalshi_pages, 1000),
    ("kalshi", "/portfolio/settlements", clients.kalshi_pages, 100),
    ("kalshi", "/portfolio/deposits", clients.kalshi_pages, 100),
    ("kalshi", "/portfolio/withdrawals", clients.kalshi_pages, 100),
    # Per-market realized P&L and fees, for reconciliation. The live tier hides settled positions by default.
    ("kalshi", "/portfolio/positions", partial(clients.kalshi_pages, params={"settlement_status": "all"}), 1000),
    ("kalshi", "/historical/positions", clients.kalshi_pages, 1000),
    # Moves between prediction markets ("event_contract") and perps ("margined"), plus shard rebalancing.
    ("kalshi", "/portfolio/intra_exchange_instance_transfers", clients.kalshi_pages, 500),
    # Every series' fee multiplier changes (public), for past line-shop fees; one unpaged list.
    ("kalshi", "/series/fee_changes", lambda endpoint, limit: kalshi_once(endpoint, {"show_historical": "true"}), None),
    ("polymarket", "/v1/portfolio/activities", clients.pm_pages, 100),
    ("polymarket", "/v1/portfolio/positions", clients.pm_pages, 100),
    ("polymarket", "/v1/account/balances", None, None),
]
GETTERS = {"kalshi": clients.kalshi_get, "polymarket": clients.pm_get}


def kalshi_once(endpoint, params):
    r = clients.kalshi_get(endpoint, params)
    r.raise_for_status()
    return [(params, r.json())]

# Bets that get market CLV (docs/pnl-rules.md). Voids are dropped later, once settled.
CLV_ELIGIBLE = ("is_parlay = 0 AND link = 'game' AND is_live = 0"
                " AND market_type IN ('moneyline', 'spread', 'total', 'prop')")
CLOSE_ENDPOINTS = {"kalshi": "/markets/{ticker}/candlesticks", "polymarket": "/v1/price-history"}
CLOSE_WINDOW = 3600  # seconds before start; an older quote is stale


def connect(path=DB_PATH):
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    return conn


def store(conn, venue, endpoint, params, body):
    """Insert one page. Returns True if it was new."""
    body_json = json.dumps(body, sort_keys=True)
    sha = hashlib.sha256(body_json.encode()).hexdigest()
    cur = conn.execute(
        "INSERT OR IGNORE INTO raw_pages (venue, endpoint, params, fetched_at, body_sha, body_json) VALUES (?, ?, ?, ?, ?, ?)",
        (venue, endpoint, json.dumps(params, sort_keys=True), datetime.now(timezone.utc).isoformat(), sha, body_json),
    )
    return cur.rowcount == 1


def records(conn, venue, endpoints, key):
    """Yield every record under `key` (a list, or one object) from the given endpoints' pages, oldest page first."""
    marks = ",".join("?" * len(endpoints))
    for (body,) in conn.execute(
        f"SELECT body_json FROM raw_pages WHERE venue = ? AND endpoint IN ({marks}) ORDER BY id",
        (venue, *endpoints),
    ):
        v = json.loads(body)[key]
        yield from v if isinstance(v, list) else [v]


def sync_kalshi_markets(conn, get=clients.kalshi_get):
    """Archive the market record for every traded ticker (settlement result and parlay legs), plus the legs of every
    parlay with an exit: pricing a cash-out's spread needs each leg's final result (phase 8a).

    Stored as endpoint "/markets/{ticker}" with body {"market": ...}. Finalized markets never change, so they're skipped.
    """
    fills = list(records(conn, "kalshi", ("/historical/fills", "/portfolio/fills"), "fills"))
    traded = {f["market_ticker"] for f in fills}
    sides = {}
    for f in fills:
        sides.setdefault(f["market_ticker"], set()).add(f.get("outcome_side"))
    stored = list(records(conn, "kalshi", ("/markets/{ticker}",), "market"))
    traded |= {leg["market_ticker"] for m in stored if len(sides.get(m["ticker"], ())) > 1
               for leg in m.get("mve_selected_legs") or []}
    done = {m["ticker"] for m in stored if m["status"] == "finalized"}
    new = missing = 0
    for ticker in sorted(traded - done):
        r = get(f"/markets/{ticker}")
        if r.status_code == 404:  # older than the historical cutoff
            r = get(f"/historical/markets/{ticker}")
        if r.status_code == 404:  # on neither tier: skip and retry next run rather than abort the archive
            missing += 1
            continue
        r.raise_for_status()
        new += store(conn, "kalshi", "/markets/{ticker}", {"ticker": ticker}, r.json())
    conn.commit()
    print(f"{'kalshi':10} {'/markets/{ticker}':28} fetched={len(traded - done)} new={new} missing={missing}")


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def milestones_by_event(conn):
    """Kalshi event ticker -> milestone (the game, with start_date). Latest page wins.

    Indexed only by each milestone's own related_event_tickers, so unrelated results can't link.
    """
    return {e: m for m in records(conn, "kalshi", ("/milestones",), "milestones") for e in m["related_event_tickers"]}


def kalshi_milestone(by_event, event):
    """Props aren't linked to milestones, but their game event is (same game code)."""
    return event and (by_event.get(event) or by_event.get(classify.game_event(event)))


def sync_kalshi_milestones(conn, get=clients.kalshi_get, now=None):
    """Archive the milestone for every traded Kalshi game event, parlay legs included.

    Stored as "/milestones" with params {"related_event_ticker": ...}. Only that filter works: `event_ticker` is
    silently ignored. Re-fetched until the game has started, so postponements are caught; after the start, while one
    of your markets on it is unsettled (two weeks at most), so a game postponed at or after its start is caught too.
    Bare parlay legs have no market record and stop at the start.
    """
    now = now or utc_now()
    markets = list(records(conn, "kalshi", ("/markets/{ticker}",), "market"))
    events = {m["event_ticker"] for m in markets if not m.get("mve_selected_legs")}
    events |= {leg["event_ticker"] for m in markets for leg in m.get("mve_selected_legs") or []}
    latest = {m["ticker"]: m for m in markets}  # oldest page first, so the latest page wins
    unsettled = {m["event_ticker"] for m in latest.values() if m.get("status") != "finalized"}
    by_event = milestones_by_event(conn)

    def started(e):
        m = kalshi_milestone(by_event, e)
        if m is None or m["start_date"] > now:
            return False
        return e not in unsettled or epoch(now) - epoch(m["start_date"]) > GIVE_UP_S

    todo = sorted(e for e in events if classify.is_kalshi_game(e) and not started(e))
    new = 0
    for e in todo:
        for q in filter(None, (e, classify.game_event(e))):
            r = get("/milestones", {"related_event_ticker": q, "limit": 5})
            r.raise_for_status()
            body = r.json()
            new += store(conn, "kalshi", "/milestones", {"related_event_ticker": q}, body)
            if any(q in m["related_event_tickers"] for m in body.get("milestones") or []):
                break
    conn.commit()
    print(f"{'kalshi':10} {'/milestones':28} events={len(todo)} new={new}")


GIVE_UP_S = 14 * 86_400  # a market not resolved two weeks after its start (cancelled, postponed) stops being re-fetched


def sync_pm_markets(conn, get=clients.gateway_get, now=None):
    """Archive the public market record of every traded single and parlay leg: type, line, start, sides, result.

    Re-fetched until resolved (or two weeks past its start), so cash-outs get their market's final result.
    Combo (parlay) slugs 404, so a parlay's result comes from its legs. A 404 leg isn't stored and is retried.
    """
    now = now or utc_now()
    trades = [a["trade"] for a in records(conn, "polymarket", ("/v1/portfolio/activities",), "activities") if a.get("trade")]
    slugs = {t["marketSlug"] for t in trades if not t.get("comboLegDetails")}
    slugs |= {l["slug"] for t in trades for l in t.get("comboLegDetails") or []}
    stored = {m["slug"]: m for m in records(conn, "polymarket", ("/v1/market/slug/{slug}",), "market")}

    def done(slug):
        m = stored.get(slug)
        if m is None:
            return False
        start = m.get("gameStartTime")
        return m.get("status") == "MARKET_STATUS_RESOLVED" or bool(start) and epoch(now) - epoch(start) > GIVE_UP_S

    todo = sorted(s for s in slugs if not done(s))
    new = missing = 0
    for slug in todo:
        r = get(f"/v1/market/slug/{slug}")
        if r.status_code == 404:
            missing += 1
            continue
        r.raise_for_status()
        new += store(conn, "polymarket", "/v1/market/slug/{slug}", {"slug": slug}, r.json())
    conn.commit()
    print(f"{'polymarket':10} {'/v1/market/slug/{slug}':28} fetched={len(todo)} new={new} missing={missing}")


def epoch(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def fetch_close(venue, market, start, kalshi_get, gateway_get):
    """1-minute prices for [start - CLOSE_WINDOW, start]. Kalshi markets past the cutoff 404 on the live tier."""
    lo = start - CLOSE_WINDOW
    if venue == "kalshi":
        p = {"start_ts": lo, "end_ts": start, "period_interval": 1, "include_latest_before_start": "true"}
        r = kalshi_get(f"/series/{market.split('-')[0]}/markets/{market}/candlesticks", p)
        if r.status_code == 404:
            r = kalshi_get(f"/historical/markets/{market}/candlesticks", p)
    else:  # the docs' bare startTimestamp/endTimestamp return 400
        r = gateway_get("/v1/price-history", {"symbol": market, "fidelity": 1,
                                              "timestamp.startTimestamp": lo, "timestamp.endTimestamp": start})
    r.raise_for_status()
    return r.json()


def parlay_clv_legs(conn):
    """{bet_id: (reason, opened_ts, [(venue, leg, leg_outcome, leg_start)])} for every parlay. reason None = eligible.

    Eligible: pregame, not void, every leg a game market from a different game. Same-game legs are correlated, so the
    product of their prices would understate the parlay (docs/pnl-rules.md).
    """
    out = {}
    for bet_id, venue, opened, is_live, status, leg, outcome, game, start in conn.execute("""
            SELECT b.bet_id, b.venue, b.opened_ts, b.is_live, b.status, p.leg_market_id, p.leg_outcome, m.game_id,
                   g.start_time
            FROM bets b JOIN parlay_legs p ON p.venue = b.venue AND p.market_id = b.market_id
            LEFT JOIN markets m ON m.venue = p.venue AND m.market_id = p.leg_market_id
            LEFT JOIN games g ON g.game_id = m.game_id
            WHERE b.is_parlay = 1 ORDER BY b.bet_id, p.leg_market_id"""):
        b = out.setdefault(bet_id, {"status": status, "is_live": is_live, "opened": opened, "legs": []})
        b["legs"].append((venue, leg, outcome, start, game))
    result = {}
    for bet_id, b in out.items():
        games = [g for *_, g in b["legs"]]
        reason = ("void" if b["status"] == "void" else "live" if b["is_live"] == 1
                  else "no start time" if b["is_live"] is None
                  else "leg without a game" if None in games or any(s is None for _, _, _, s, _ in b["legs"])
                  else "same-game legs" if len(set(games)) < len(games) else None)
        result[bet_id] = (reason, b["opened"], [l[:4] for l in b["legs"]])
    return result


def fill_key(venue, ts):
    """Quote time for pricing a fill: the end of the minute before it, so the quote can't include the fill itself.

    Kalshi candles are stamped at their minute's end; a Polymarket point may mark either end, so its key is a minute
    earlier (docs/pnl-rules.md, spread).
    """
    return epoch(ts) // 60 * 60 - (60 if venue == "polymarket" else 0)


def spread_targets(conn):
    """(wanted, keep_empty): (venue, market, key) for every single's fill and, per leg, every exit fill of an eligible
    parlay. keep_empty = the Kalshi exit-leg ones: no candles there means the leg's market had closed (decided)."""
    want = {(v, m, fill_key(v, ts)) for v, m, ts in conn.execute(
        "SELECT venue, market_id, ts FROM fills WHERE (venue, market_id) NOT IN"
        " (SELECT venue, market_id FROM parlay_legs)")}
    keep, legs = set(), parlay_clv_legs(conn)
    for bet_id, venue, market, outcome, opened, closed in conn.execute(
            "SELECT bet_id, venue, market_id, outcome, opened_ts, closed_ts FROM bets"
            " WHERE is_parlay = 1 AND exit_qty > 0"):
        reason, _, bet_legs = legs[bet_id]
        if reason:
            continue
        lo, hi = epoch(opened), closed and epoch(closed)
        for (ts,) in conn.execute("SELECT ts FROM fills WHERE venue = ? AND market_id = ? AND outcome != ?",
                                  (venue, market, outcome)):
            if epoch(ts) < lo or (hi and epoch(ts) > hi):  # epochs: fill and closed_ts fractions differ in length
                continue
            for leg_venue, leg, _, _ in bet_legs:
                t = (leg_venue, leg, fill_key(leg_venue, ts))
                want.add(t)
                if leg_venue == "kalshi":
                    keep.add(t)
    return want, keep


def sync_closes(conn, kalshi_get=clients.kalshi_get, gateway_get=clients.gateway_get, now=None):
    """Archive prices for every CLV-eligible bet, once per (market, time). Needs a fresh `bets`.

    Singles: at game start. Eligible parlays: each leg at its own game start and at the parlay's placement.
    Fills: every single's fill and every eligible parlay exit's legs, at fill_key (phase 8a spread).
    A response with no points isn't stored, so it's retried next run. The body carries market and start_ts:
    raw_pages dedups on body alone, and price-history doesn't name its symbol.
    """
    cutoff = epoch(now or utc_now()) - 300  # let the last pre-start minute close
    done = set(conn.execute("SELECT venue, json_extract(body_json, '$.market'), json_extract(body_json, '$.start_ts')"
                            f" FROM raw_pages WHERE endpoint IN ({','.join('?' * len(CLOSE_ENDPOINTS))})",
                            tuple(CLOSE_ENDPOINTS.values())))
    want = {(v, m, epoch(s)) for v, m, s in conn.execute(
        f"SELECT venue, market_id, start_time FROM bets WHERE {CLV_ELIGIBLE}")}
    for reason, opened, legs in parlay_clv_legs(conn).values():
        if reason is None:
            want |= {(v, leg, epoch(t)) for v, leg, _, start in legs for t in (start, opened)}
    fills, keep = spread_targets(conn)
    want |= fills | line_shop_targets(conn)
    todo = sorted(want - done)
    todo = [t for t in todo if t[2] <= cutoff]
    ok, new = True, 0
    for venue, market, start in todo:
        try:
            body = fetch_close(venue, market, start, kalshi_get, gateway_get)
            if not (body.get("candlesticks") or body.get("history")) and (venue, market, start) not in keep:
                continue  # venue data can lag; not stored, so the next run retries
            new += store(conn, venue, CLOSE_ENDPOINTS[venue], {"market": market, "start_ts": start},
                         {**body, "market": market, "start_ts": start})
            conn.commit()
        except Exception as e:  # report and continue; never print response headers
            ok = False
            print(f"{venue:10} close {market} FAILED: {type(e).__name__}: {str(e)[:200]}")
    print(f"{'both':10} {'closes':28} fetched={len(todo)} new={new}")
    return ok


# ---- phase 8b: the other venue's price at each past single (docs/pnl-rules.md, line shop) ----

LINE_SHOP_ELIGIBLE = "is_parlay = 0 AND link = 'game' AND market_type IN ('moneyline', 'spread', 'total', 'prop')"
LS_WINDOW = 3 * 3600  # seconds either side of a start time when looking for the Kalshi milestone of a Polymarket game


def qkey(q):
    return json.dumps(q, sort_keys=True)


def stored_queries(conn, venue, endpoint):
    """{query: body} for line-shop pages: each body carries the query it answers, because raw_pages dedups on body
    alone and an empty answer (game not listed) must still be remembered. Latest page wins."""
    out = {}
    for (body,) in conn.execute("SELECT body_json FROM raw_pages WHERE venue = ? AND endpoint = ? ORDER BY id",
                                (venue, endpoint)):
        b = json.loads(body)
        if "query" in b:
            out[qkey(b["query"])] = b
    return out


def milestones_by(conn):
    ms = list(records(conn, "kalshi", ("/milestones",), "milestones"))
    return {m["id"]: m for m in ms}, {sid: m for m in ms for sid in (m.get("source_ids") or {}).values()}


def line_shop_bets(conn):
    return conn.execute("SELECT bet_id, venue, market_id, outcome, game_id, opened_ts, start_time, market_type, sport,"
                        f" stake, entry_qty, is_live FROM bets WHERE {LINE_SHOP_ELIGIBLE} ORDER BY bet_id").fetchall()


def pm_event_queries(game_id):
    """A Polymarket game's event lookups, light first: the full event (every market) only when the light one is empty."""
    slug = game_id.split(":", 1)[1]
    return {"slug": slug, "marketTypes": "moneyline", "limit": 1}, {"slug": slug, "limit": 1}


def kalshi_line_events(milestone, mtype, pm_smt):
    """Kalshi events that can hold the counterpart of a Polymarket market of this type."""
    main = milestone["details"]["main_game_event_ticker"]  # KXNFLGAME-…, or KXWCADVANCE-… for a knockout match
    prefix, code = re.sub(r"(GAME|ADVANCE)$", "", main.split("-")[0]), main.split("-", 1)[1]
    if mtype == "prop":
        kind = match.PROP_BY_PM.get(pm_smt)
        return [f"{s}-{code}" for s in match.PROPS[kind][0]] if kind else []
    kind = {"spread": "SPREAD", "total": "TOTAL"}.get(mtype) or (
        "ADVANCE" if pm_smt == "soccer_game_to_advance" else "GAME")
    return [f"{prefix}{kind}-{code}"]


def covered_type(venue, market, mtype, own):
    """False for props outside the registry: nothing to fetch or pair."""
    if mtype != "prop":
        return True
    return market.split("-")[0] in match.PROP_BY_SERIES if venue == "kalshi" else \
        (own or {}).get("sportsMarketType") in match.PROP_BY_PM


def find_milestone(kalshi_get, sr, start):
    lo, hi = epoch(start) - LS_WINDOW, epoch(start) + LS_WINDOW
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cursor = None
    while True:
        p = {"minimum_start_date": iso(lo), "maximum_start_date": iso(hi), "limit": 200}
        if cursor:
            p["cursor"] = cursor
        r = kalshi_get("/milestones", p)
        r.raise_for_status()
        b = r.json()
        hit = next((m for m in b.get("milestones") or [] if sr in (m.get("source_ids") or {}).values()), None)
        cursor = b.get("cursor")
        if hit or not cursor:
            return hit


def sync_line_shop(conn, kalshi_get=clients.kalshi_get, gateway_get=clients.gateway_get, now=None):
    """Archive the other venue's game and markets for every eligible single whose game has started (markets are
    final then), once per game: a stored answer, empty included, is never re-fetched. Needs a fresh `bets`.

    Kalshi bet -> Polymarket `/v1/events?sportradarGameId=` (full event, every market).
    Polymarket bet -> light `/v1/events?slug=` (its sportradarGameId) -> the Kalshi milestone holding that ID (paged by
    start window, then stored as `/milestones?related_event_ticker=` like sync_kalshi_milestones; an empty marker when
    none) -> Kalshi `/markets?event_ticker=` for the events that can hold the counterpart.
    """
    now = epoch(now or utc_now())
    pm_q, k_q = stored_queries(conn, "polymarket", "/v1/events"), stored_queries(conn, "kalshi", "/markets")
    m_q = stored_queries(conn, "kalshi", "/milestones")
    by_id, by_sr = milestones_by(conn)
    own_p = {m["slug"]: m for m in records(conn, "polymarket", ("/v1/market/slug/{slug}",), "market")}
    ok, new = True, 0

    def fetch(venue, get, endpoint, q, path=None):
        nonlocal new
        r = get(path or endpoint, q)
        r.raise_for_status()
        body = {**r.json(), "query": q}
        new += store(conn, venue, endpoint, q, body)
        return body

    for bet_id, venue, market, _, game_id, _, start, mtype, *_ in line_shop_bets(conn):
        if not start or epoch(start) > now or not covered_type(venue, market, mtype, own_p.get(market)):
            continue
        try:
            if venue == "kalshi":
                m = by_id.get(game_id.split(":", 1)[1])
                ids = list((m or {}).get("source_ids", {}).values())
                answers = [pm_q.get(qkey({"sportradarGameId": i, "limit": 1})) for i in ids]
                if any(a and a.get("events") for a in answers) or all(answers):
                    continue  # found, or every ID answered empty (not listed)
                for i, a in zip(ids, answers):
                    if a is None:  # an ID whose lookup failed last run is asked again
                        q = {"sportradarGameId": i, "limit": 1}
                        pm_q[qkey(q)] = body = fetch("polymarket", gateway_get, "/v1/events", q)
                        if body.get("events"):
                            break
                continue
            light = None
            for q in pm_event_queries(game_id):  # light first; soccer group games have no moneyline-type market
                light = pm_q.get(qkey(q)) or fetch("polymarket", gateway_get, "/v1/events", q)
                pm_q[qkey(q)] = light
                if light.get("events"):
                    break
            sr = ((light.get("events") or [{}])[0]).get("sportradarGameId")
            if not sr:
                continue
            m = by_sr.get(sr)
            if m is None:
                mq = {"sportradarGameId": sr}
                if qkey(mq) in m_q:
                    continue
                m = find_milestone(kalshi_get, sr, start)
                if m is None:
                    new += store(conn, "kalshi", "/milestones", mq, {"milestones": [], "query": mq})
                    m_q[qkey(mq)] = {}
                    continue
                main = m["details"]["main_game_event_ticker"]
                r = kalshi_get("/milestones", {"related_event_ticker": main, "limit": 5})
                r.raise_for_status()
                new += store(conn, "kalshi", "/milestones", {"related_event_ticker": main}, r.json())
                by_id[m["id"]] = m
                by_sr.update({sid: m for sid in (m.get("source_ids") or {}).values()})
            for e in kalshi_line_events(m, mtype, own_p.get(market, {}).get("sportsMarketType")):
                q = {"event_ticker": e, "limit": 1000}
                if qkey(q) in k_q:
                    continue
                r = kalshi_get("/markets", q)
                r.raise_for_status()
                body = r.json()
                if not body.get("markets"):  # past the historical cutoff; a failure raises, so nothing is remembered
                    r = kalshi_get("/historical/markets", q)
                    r.raise_for_status()
                    body = r.json() if r.json().get("markets") else body
                k_q[qkey(q)] = body = {**body, "query": q}
                new += store(conn, "kalshi", "/markets", q, body)
        except Exception as e:  # report and continue; never print response headers
            ok = False
            print(f"{venue:10} line shop {bet_id} FAILED: {type(e).__name__}: {str(e)[:200]}")
        conn.commit()
    print(f"{'both':10} {'line shop':28} new={new}")
    return ok


def line_shop_counterparts(conn):
    """{bet_id: (reason, other venue, [(market, side)], oriented)} for every eligible single; reason None = paired.
    oriented = False when the team order couldn't be checked by team code (see match.orientation) and the outcome
    names a team: line_shop_rows then needs your own mid to confirm the side.

    The bet's own outcome key comes from its own market record and side; the other venue's refs for the same key
    from the pages sync_line_shop stored.
    """
    pm_q, k_q = stored_queries(conn, "polymarket", "/v1/events"), stored_queries(conn, "kalshi", "/markets")
    m_q = stored_queries(conn, "kalshi", "/milestones")
    by_id, by_sr = milestones_by(conn)
    # Full events only: a Polymarket bet's moneyline-only page for the same game must not stand in for them.
    pm_by_sr = {e["sportradarGameId"]: e for k, b in pm_q.items() if "sportradarGameId" in json.loads(k)
                for e in b.get("events") or []}
    own_k = {m["ticker"]: m for m in records(conn, "kalshi", ("/markets/{ticker}",), "market")}
    own_p = {m["slug"]: m for m in records(conn, "polymarket", ("/v1/market/slug/{slug}",), "market")}
    out = {}
    for bet_id, venue, market, outcome, game_id, _, _, mtype, sport, *_ in line_shop_bets(conn):
        other = "polymarket" if venue == "kalshi" else "kalshi"
        missing = "prop not listed" if mtype == "prop" else "no matching line"
        own = (own_k if venue == "kalshi" else own_p).get(market)
        if not covered_type(venue, market, mtype, own):
            out[bet_id] = ("prop type not covered", other, [], True)
            continue
        if own is None:
            out[bet_id] = ("not fetched yet", other, [], True)
            continue
        if venue == "kalshi":
            k_teams = match.teams_from_kalshi_event(own["event_ticker"])
            mine = match.kalshi_outcomes([own], k_teams, three_way=sport == "soccer")
            ids = list((by_id.get(game_id.split(":", 1)[1]) or {}).get("source_ids", {}).values())
            ev = next((pm_by_sr[i] for i in ids if i in pm_by_sr), None)
            if ev is None:
                asked = bool(ids) and all(qkey({"sportradarGameId": i, "limit": 1}) in pm_q for i in ids)
                out[bet_id] = ("game not on other venue" if asked else "not fetched yet", other, [], True)
                continue
            away, home = match.teams_from_pm_slug(ev["slug"])
            o = match.orientation(k_teams, away, home)
            theirs = match.pm_outcomes(ev["markets"], *((home, away) if o == "swapped" else (away, home)))
        else:
            pages = [pm_q.get(qkey(q)) for q in pm_event_queries(game_id)]
            light = next((p for p in pages if p and p.get("events")), pages[-1])
            sr = light and ((light.get("events") or [{}])[0]).get("sportradarGameId")
            m = sr and by_sr.get(sr)
            if not m:
                gone = light is not None and (not sr or qkey({"sportradarGameId": sr}) in m_q)
                out[bet_id] = ("game not on other venue" if gone else "not fetched yet", other, [], True)
                continue
            events = [qkey({"event_ticker": e, "limit": 1000}) for e in kalshi_line_events(m, mtype, own.get("sportsMarketType"))]
            if any(e not in k_q for e in events):
                out[bet_id] = ("not fetched yet", other, [], True)
                continue
            k_teams = match.teams_from_kalshi_event(m["details"]["main_game_event_ticker"])
            away, home = match.teams_from_pm_slug(game_id.split(":", 1)[1])
            o = match.orientation(k_teams, away, home)
            mine = match.pm_outcomes([own], *((home, away) if o == "swapped" else (away, home)))
            theirs = match.kalshi_outcomes([mk for e in events for mk in k_q[e].get("markets") or []], k_teams)
        key = next((k for k, refs in mine.items() if (market, outcome) in refs), None)
        if key is None:
            out[bet_id] = (missing, other, [], True)
            continue
        refs = theirs.get(key)
        oriented = o is not None or key[0] not in match.ROLE_KINDS
        out[bet_id] = (None, other, refs, oriented) if refs else (missing, other, [], True)
    return out


def line_shop_targets(conn):
    """(venue, market, key) quote windows for each paired single's refs, at its first fill on the other venue."""
    opened = {b[0]: b[5] for b in line_shop_bets(conn)}
    return {(other, m, fill_key(other, opened[b])) for b, (reason, other, refs, _) in line_shop_counterparts(conn).items()
            if reason is None for m, _ in refs}


def sync(conn, endpoints=ENDPOINTS):
    """Archive every endpoint. One failing endpoint doesn't stop the others. Returns True if all succeeded."""
    ok = True
    for venue, endpoint, pager, limit in endpoints:
        pages = new = 0
        try:
            if pager is None:
                r = GETTERS[venue](endpoint)
                r.raise_for_status()
                results = [({}, r.json())]
            else:
                results = pager(endpoint, limit=limit)
            for params, body in results:
                pages += 1
                new += store(conn, venue, endpoint, params, body)
            conn.commit()
            print(f"{venue:10} {endpoint:28} pages={pages} new={new}")
        except Exception as e:  # report and continue; never print response headers (they carry auth)
            conn.rollback()
            ok = False
            print(f"{venue:10} {endpoint:28} FAILED: {type(e).__name__}: {str(e)[:200]}")
    return ok


if __name__ == "__main__":
    conn = connect()
    ok = sync(conn)
    sync_kalshi_markets(conn)  # after fills, so newly traded tickers are included
    sync_kalshi_milestones(conn)  # after market records: needs their event tickers and legs
    sync_pm_markets(conn)
    from sharp_check import normalize  # late: normalize imports archive
    normalize.rebuild(conn, normalize.MANUAL_CASH)  # closes need fresh bets (start times, eligibility)
    ok = sync_line_shop(conn) and ok
    ok = sync_closes(conn) and ok
    normalize.rebuild(conn, normalize.MANUAL_CASH)  # so new closes reach bets in this run
    sys.exit(0 if ok else 1)
