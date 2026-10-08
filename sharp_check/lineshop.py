"""Phase 8b live line-shop checker: fetch on demand, compare all-in prices, store nothing (docs/pnl-rules.md)."""
import time
from datetime import datetime, timedelta, timezone

from sharp_check import clients, match

ONE = match.ONE
SLATE_HOURS = 48
SLATE_TTL = 600  # seconds
MAX_BOOKS = 10   # Polymarket book calls per game view
GROUPS = ("Moneyline", "Spread", "Total") + tuple(match.PROPS)
GAME_LINES = ("GAME", "SPREAD", "TOTAL")
_slate = {"at": 0.0, "games": []}
_fee_multiplier = {}


def live_theta(m):
    """A Polymarket market's fee coefficient; missing -> the latest known one, never fee-free (a 0 would make
    Polymarket look cheaper than it is)."""
    theta = m.get("feeCoefficient")
    return match.PM_THETA[-1][1] if theta is None else float(theta)


def micros(v):
    return None if v is None else round(float(v) * ONE)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _get(get, path, params=None):
    r = get(path, params)
    r.raise_for_status()
    return r.json()


def refresh_slate(trigger, selected):
    """Refresh re-prices the chosen game; it reloads the game list only when no game is chosen (the Polymarket
    gateway allows ~5 quick calls, which a slate reload would use up)."""
    return trigger == "ls-refresh" and not selected


def slate(now=None, kalshi_get=clients.kalshi_get, gateway_get=clients.gateway_get, refresh=False):
    """Games in the checker leagues starting in (now, now + 48 h] that both venues list, joined on Sportradar ID.
    A cached slate is re-filtered on every call, so a game drops out once it starts."""
    now_dt = utc(now) if now else datetime.now(timezone.utc)
    lo, hi = iso(now_dt), iso(now_dt + timedelta(hours=SLATE_HOURS))
    if not refresh and time.time() - _slate["at"] < SLATE_TTL:
        return [g for g in _slate["games"] if g["start"] > lo]
    games = []
    for league, (tag, competition, _) in match.LEAGUES.items():
        milestones, cursor = [], None
        while True:
            p = {"competition": competition, "minimum_start_date": lo, "maximum_start_date": hi, "limit": 200}
            if cursor:
                p["cursor"] = cursor
            b = _get(kalshi_get, "/milestones", p)
            milestones += b.get("milestones") or []
            cursor = b.get("cursor")
            if not cursor:
                break
        by_sr = {sid: m for m in milestones for sid in (m.get("source_ids") or {}).values()}
        offset = 0
        while True:
            evs = _get(gateway_get, "/v1/events", {"tagSlug": tag, "marketTypes": "moneyline", "startTimeMin": lo,
                                                   "startTimeMax": hi, "limit": 100, "offset": offset}).get("events") or []
            for e in evs:
                m = by_sr.get(e.get("sportradarGameId"))
                if m and e["startTime"] > lo:  # not started
                    games.append({"key": e["slug"], "league": league, "title": e.get("title") or m["title"],
                                  "start": e["startTime"], "pm_slug": e["slug"],
                                  "kalshi_event": m["details"]["main_game_event_ticker"], "milestone": m})
            if len(evs) < 100:
                break
            offset += 100
    games.sort(key=lambda g: g["start"])
    _slate.update(at=time.time(), games=games)
    return games


def kalshi_events(game):
    """Game-line events from the milestone, plus registry prop events built from the game code (NFL only)."""
    code = game["kalshi_event"].split("-", 1)[1]
    prefix = game["kalshi_event"].split("-")[0][:-len("GAME")]
    events = [f"{prefix}{kind}-{code}" for kind in GAME_LINES]
    if prefix == "KXNFL":
        events += [f"{s}-{code}" for series, _ in match.PROPS.values() for s in series]
    return events


def fee_multiplier(kalshi_get, series):
    if series not in _fee_multiplier:
        try:
            _fee_multiplier[series] = float(_get(kalshi_get, f"/series/{series}")["series"].get("fee_multiplier") or 1)
        except Exception:
            return 1.0
    return _fee_multiplier[series]


def kalshi_side(kalshi_get):
    """Fetch-time pricing for Kalshi refs: (all_in, ask, size, mid) for buying `side` of `market`."""
    def price(m, side):
        bid, ask = micros(m.get("yes_bid_dollars")), micros(m.get("yes_ask_dollars"))
        bid, ask = (bid if bid and bid > 0 else None), (ask if ask and ask < ONE else None)
        a, size = match.side_ask(bid, ask, float(m.get("yes_bid_size_fp") or 0), float(m.get("yes_ask_size_fp") or 0),
                                 side)
        if a is None:
            return None
        mid = None if bid is None or ask is None else (bid + ask) / 2 if side == "yes" else ONE - (bid + ask) / 2
        return a + round(match.kalshi_fee(a, fee_multiplier(kalshi_get, m["ticker"].split("-")[0]))), a, size, mid
    return price


def pm_book(gateway_get, slug):
    r = gateway_get(f"/v1/markets/{slug}/book")
    if r.status_code == 429:  # the gateway throttles bursts; wait as told, once
        time.sleep(min(float(getattr(r, "headers", {}).get("Retry-After") or 1), 5))
        r = gateway_get(f"/v1/markets/{slug}/book")
    r.raise_for_status()
    md = r.json()["marketData"]
    best = lambda levels, pick: pick(((micros(l["px"]["value"]), float(l["qty"])) for l in levels or []),
                                     key=lambda x: x[0], default=(None, None))
    return best(md.get("bids"), max), best(md.get("offers"), min)


def cell(price, sized=True):
    """thin = $ available at the ask when under the usual stake; None when enough, or size unknown (sized=False)."""
    if price is None:
        return None
    all_in, ask, size, _ = price
    usd = (size or 0) * ask / ONE
    return {"all_in": all_in, "ask": ask, "thin": usd if sized and usd < match.USUAL_STAKE / ONE else None}


def group(key):
    return {"ml": "Moneyline", "not": "Moneyline", "advance": "Moneyline", "spread": "Spread",
            "total": "Total"}.get(key[0]) or key[1]


def game_rows(game, kalshi_get=clients.kalshi_get, gateway_get=clients.gateway_get, now=None):
    errors, k_markets, pm_event = [], None, None
    try:
        k_markets = [m for e in kalshi_events(game)
                     for m in _get(kalshi_get, "/markets", {"event_ticker": e, "limit": 1000}).get("markets") or []]
    except Exception:
        errors.append("Kalshi unavailable")
    try:
        pm_event = _get(gateway_get, f"/v1/events/slug/{game['pm_slug']}")["event"]
    except Exception:
        errors.append("Polymarket unavailable")
    if pm_event is None and k_markets is None:
        return {"as_of": now or iso(datetime.now(timezone.utc)), "errors": errors, "rows": []}
    away, home = match.teams_from_pm_slug(game["pm_slug"])
    teams = match.teams_from_kalshi_event(game["kalshi_event"])
    k_out = match.kalshi_outcomes(k_markets or [], teams) if k_markets is not None else {}
    pm_markets = (pm_event or {}).get("markets") or []
    k_by = {m["ticker"]: m for m in k_markets or []}
    pm_by = {m["slug"]: m for m in pm_markets}
    kprice = kalshi_side(kalshi_get)

    def pm_price(slug, side, size=None):
        """From the event page's top of book (no size there); `size` is the book's, when fetched."""
        m = pm_by[slug]
        bid, ask = micros((m.get("bestBidQuote") or {}).get("value")), micros((m.get("bestAskQuote") or {}).get("value"))
        a, n = match.side_ask(bid, ask, size, size, side)
        if a is None:
            return None
        mid = None if bid is None or ask is None else (bid + ask) / 2 if side == "yes" else ONE - (bid + ask) / 2
        return a + round(match.pm_fee(a, live_theta(m))), a, n, mid

    def ml_disagreement(p_out):
        """Σ |Kalshi mid − Polymarket mid| over moneyline outcomes: small when the teams line up."""
        total = 0
        for key in (k for k in k_out.keys() & p_out.keys() if k[0] == "ml"):
            k = min(filter(None, (kprice(k_by[t], s) for t, s in k_out[key])), default=None)
            p = min(filter(None, (pm_price(s, side) for s, side in p_out[key])), default=None)
            if k and p and k[3] is not None and p[3] is not None:
                total += abs(k[3] - p[3])
        return total

    # Orient by team codes; when neither code matches, by which order makes the moneyline prices agree.
    o = match.orientation(teams, away, home)
    orders = [(away, home)] if o == "same" or k_markets is None else [(home, away)] if o == "swapped" \
        else [(away, home), (home, away)]
    away, home = min(orders, key=lambda ah: ml_disagreement(match.pm_outcomes(pm_markets, *ah)))
    p_out = match.pm_outcomes(pm_markets, away, home)
    names = {"away": away.upper(), "home": home.upper()}
    for t in (pm_event or {}).get("teams") or []:
        role = match.pm_role(t.get("abbreviation"), away, home)
        if role:
            names[role] = t.get("name") or names[role]
    # With one venue down, show the other venue's outcomes for keys the up venue lists.
    keys = (k_out.keys() & p_out.keys()) if k_markets is not None and pm_event is not None else \
        (k_out.keys() if pm_event is None else p_out.keys())
    rows = []
    for key in keys:
        k = min(filter(None, (kprice(k_by[t], s) for t, s in k_out.get(key, []))), default=None)
        p_refs = [(pm_price(s, side), s, side) for s, side in p_out.get(key, [])]
        p_ref = min((r for r in p_refs if r[0]), default=None, key=lambda r: r[0])
        p = p_ref and p_ref[0]
        if k and p and k[3] is not None and p[3] is not None and abs(k[3] - p[3]) > match.SIDE_CHECK:
            continue  # sides likely flipped
        gap = k[0] - p[0] if k and p else None
        best = None if gap is None or abs(gap) < match.NO_EDGE else "polymarket" if gap > 0 else "kalshi"
        rows.append({"group": group(key), "label": match.label(key, names), "kalshi": cell(k),
                     "polymarket": cell(p, sized=False), "gap": gap, "best": best, "key": key, "pm_ref": p_ref})
    # Polymarket sizes need one book call per market and its gateway throttles after ~5 quick calls, so only rows
    # Polymarket wins get one: that's where a thin book would mislead.
    for row in [r for r in rows if r["best"] == "polymarket"][:MAX_BOOKS]:
        _, slug, side = row["pm_ref"]
        book = _safe(pm_book, gateway_get, slug)
        if book:
            (bid, bsz), (ask, asz) = book
            row["polymarket"] = cell(pm_price(slug, side, bsz if side == "no" else asz))
    for row in rows:
        row.pop("pm_ref")
    rows.sort(key=lambda r: (GROUPS.index(r["group"]), str(r["key"])))
    return {"as_of": now or iso(datetime.now(timezone.utc)), "errors": errors, "rows": rows}


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None
