"""Phase 2 headline metrics from `bets`, `cash_ledger` and balance snapshots. Rules: docs/pnl-rules.md."""
import json
import math
import statistics

from sharp_check import archive
from sharp_check.normalize import ONE
from sharp_check.reconcile import reported_cash

VENUES = ("kalshi", "polymarket")
SPLITS = {"all": (0, 1), "singles": (0,), "parlays": (1,)}
SEGMENTS = {"is_live": "Live vs pregame", "sport": "Sport", "market_type": "Market type"}


def headline(conn, venues=VENUES, split="all", where=None):
    """Betting P&L over closed bets, and actual vs expected wins over bets with a 0/1 outcome.

    Judged = held to a 0/1 settlement, or cashed out with a 0/1 market result (would it have won if held).
    Settled-only would be biased low: cash-outs are mostly winning positions. Voids and unresolved cash-outs
    count in P&L and ROI only. `where` = (column in SEGMENTS, value) narrows to one segment.
    """
    extra, args = "", ()
    if where:
        assert where[0] in SEGMENTS
        extra, args = f" AND b.{where[0]} = ?", (where[1],)
    rows = conn.execute(f"""
        SELECT b.status, b.stake, b.realized_pnl, b.avg_entry, b.payout, b.outcome, m.yes_value
        FROM bets b LEFT JOIN markets m USING (venue, market_id)
        WHERE b.venue IN ({','.join('?' * len(venues))}) AND b.is_parlay IN ({','.join('?' * len(SPLITS[split]))}){extra}
        """, (*venues, *SPLITS[split], *args)).fetchall()
    closed = [r for r in rows if r[0] != "open"]
    held = [(r[3], r[4] > 0) for r in rows if r[0] == "settled"]
    held += [(r[3], (r[6] == ONE) == (r[5] == "yes")) for r in rows if r[0] == "closed_early" and r[6] in (0, ONE)]
    stake = sum(r[1] for r in closed)
    pnl = sum(r[2] for r in closed)
    probs = [p / ONE for p, _ in held]
    expected = sum(probs)
    sd = math.sqrt(sum(p * (1 - p) for p in probs))  # wins are a sum of independent Bernoullis
    return {
        "bets": len(closed),
        "open": len(rows) - len(closed),
        "stake": stake,
        "pnl": pnl,
        "roi": pnl / stake if stake else None,
        "settled": len(held),
        "wins": sum(w for _, w in held),
        "expected": expected,
        "ci": (expected - 1.96 * sd, expected + 1.96 * sd),
    }


def segments(conn, column, min_n=30):
    """Headline per value of `column`, both venues. Slices with fewer than min_n closed bets are hidden (pnl-rules)."""
    assert column in SEGMENTS
    values = [v for (v,) in conn.execute(f"SELECT DISTINCT {column} FROM bets WHERE {column} IS NOT NULL ORDER BY 1")]
    rows = [(v, headline(conn, where=(column, v))) for v in values]
    shown = [r for r in rows if r[1]["bets"] >= min_n]
    return shown, len(rows) - len(shown)


def mean_ci(xs, groups=None):
    """Mean and 95% CI half-width (None when n < 2, or fewer than 2 groups).

    groups: a key per value (game). Values sharing a key are correlated, so the half-width is cluster-robust: each
    group's summed deviations are squared, with a G/(G-1) correction. One value per group = the plain formula.
    The wider of the two is returned: with few groups the clustered one is noisy and can come out narrower when
    deviations in a group cancel (decided 2026-10-08).
    """
    n = len(xs)
    mean = sum(xs) / n if n else None
    plain = 1.96 * statistics.stdev(xs) / math.sqrt(n) if n > 1 else None
    if groups is None:
        return mean, plain
    dev = {}
    for x, g in zip(xs, groups):
        dev[g] = dev.get(g, 0) + x - mean
    k = len(dev)
    return mean, (max(plain, 1.96 * math.sqrt(sum(d * d for d in dev.values()) * k / (k - 1)) / n) if k > 1 else None)


def parlay_groups(conn):
    """{bet_id: group} for parlays: parlays sharing any leg game are one group, chained (A-B and B-C -> one).
    A leg without a game links nothing (such parlays aren't CLV-eligible anyway)."""
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    bets = [b for (b,) in conn.execute("SELECT bet_id FROM bets WHERE is_parlay = 1")]
    for bet_id, game in conn.execute("""
            SELECT b.bet_id, m.game_id FROM bets b
            JOIN parlay_legs p ON p.venue = b.venue AND p.market_id = b.market_id
            JOIN markets m ON m.venue = p.venue AND m.market_id = p.leg_market_id
            WHERE b.is_parlay = 1 AND m.game_id IS NOT NULL"""):
        parent[find(("bet", bet_id))] = find(("game", game))
    return {b: find(("bet", b)) for b in bets}


def clv(conn, venues=VENUES):
    """Market CLV over eligible non-void bets. Means and beat rates use every bet with a close; dollar sums and
    P&L use only the closed ones, so they compare the same bets. Net = after entry fees; move = vs your close ask."""
    rows = conn.execute(f"""SELECT bet_id, start_time, status, clv, clv_usd, realized_pnl, clv_net, clv_move, entry_qty,
                                   game_id
                            FROM bets
                            WHERE venue IN ({','.join('?' * len(venues))}) AND {archive.CLV_ELIGIBLE}
                            AND status != 'void' ORDER BY start_time""", venues).fetchall()
    have = [r for r in rows if r[3] is not None]
    cents = [r[3] / 10_000 for r in have]
    net = [r[6] / 10_000 for r in have]
    move = [r[7] / 10_000 for r in have]
    n = len(cents)
    games = [r[9] or r[0] for r in have]  # bets on one game move together; no game = its own group
    mean, half = mean_ci(cents, games)
    net_mean, net_half = mean_ci(net, games)
    closed = [r for r in have if r[2] != "open"]
    return {
        "eligible": len(rows),
        "with_close": n,
        "clv_usd": sum(r[4] for r in closed),
        "net_usd": round(sum(r[8] * r[6] for r in closed)),
        "pnl": sum(r[5] for r in closed),
        "mean_cents": mean,
        "ci": None if half is None else (mean - half, mean + half),
        "beat": sum(c > 0 for c in cents) / n if n else None,
        "net_cents": net_mean,
        "net_ci": None if net_half is None else (net_mean - net_half, net_mean + net_half),
        "move_cents": mean_ci(move)[0],
        "move_beat": sum(m > 0 for m in move) / n if n else None,
        "missing": [(r[0], r[1]) for r in rows if r[3] is None],
    }


def clv_bets(conn, venues=VENUES):
    return conn.execute(f"""SELECT b.bet_id, b.start_time, COALESCE(m.title, b.market_id), b.market_type, b.outcome,
                                  b.avg_entry, b.close_mid, b.clv, b.clv_net, b.clv_move, b.clv_usd, b.realized_pnl
                           FROM bets b LEFT JOIN markets m USING (venue, market_id)
                           WHERE b.clv IS NOT NULL AND b.is_parlay = 0 AND b.venue IN ({','.join('?' * len(venues))})
                           ORDER BY b.start_time DESC""", venues).fetchall()


def parlay_clv(conn, venues=VENUES, now=None):
    """Parlay CLV over eligible parlays (archive.parlay_clv_legs). Markup = entry - fair price of the legs at placement
    (positive = overpaid); movement = close - fair entry; CLV = movement - markup. Dollar sums and P&L: closed only."""
    reasons = archive.parlay_clv_legs(conn)
    rows = conn.execute(f"""SELECT bet_id, status, clv, clv_usd, realized_pnl, clv_net, avg_entry, fair_entry, close_mid
                            FROM bets WHERE is_parlay = 1 AND venue IN ({','.join('?' * len(venues))})
                            ORDER BY bet_id""", venues).fetchall()
    eligible = [r for r in rows if reasons[r[0]][0] is None]
    have = [r for r in eligible if r[2] is not None]
    closed = [r for r in have if r[1] != "open"]
    groups = parlay_groups(conn)
    groups = [groups[r[0]] for r in have]
    mean, half = mean_ci([r[2] / 10_000 for r in have], groups)
    net, net_half = mean_ci([r[5] / 10_000 for r in have], groups)
    return {
        "eligible": len(eligible),
        "with_clv": len(have),
        "clv_usd": sum(r[3] for r in closed),
        "pnl": sum(r[4] for r in closed),
        "mean_cents": mean,
        "ci": None if half is None else (mean - half, mean + half),
        "net_cents": net,
        "net_ci": None if net_half is None else (net - net_half, net + net_half),
        "markup_cents": mean_ci([(r[6] - r[7]) / 10_000 for r in have])[0],
        "move_cents": mean_ci([(r[8] - r[7]) / 10_000 for r in have])[0],
        # Relative markup, price-weighted: cents understate it on longshots (4.8c paid for 1.3c of legs = +270%).
        "markup_pct": sum(r[6] - r[7] for r in have) / sum(r[7] for r in have) if have else None,
        "beat": sum(r[2] > 0 for r in have) / len(have) if have else None,
        "excluded": [(r[0], reasons[r[0]][0] or why_no_parlay_clv(reasons[r[0]][2], now)) for r in rows
                     if r not in have],
    }


def why_no_parlay_clv(legs, now=None):
    """An eligible parlay without CLV: a leg still to kick off, or a leg quote missing, too wide or too stale."""
    now = now or archive.utc_now()
    return "waiting for kickoff" if any(archive.epoch(s) > archive.epoch(now) for *_, s in legs) else "no usable leg quote"


def parlay_bets(conn, venues=VENUES):
    """Per parlay with CLV: placed, bet, legs, entry, fair entry, close, markup, movement, CLV after fees, P&L."""
    return conn.execute(f"""SELECT b.opened_ts, b.bet_id, (SELECT COUNT(*) FROM parlay_legs p WHERE p.venue = b.venue
                                  AND p.market_id = b.market_id), b.avg_entry, b.fair_entry, b.close_mid,
                                  b.avg_entry - b.fair_entry, b.close_mid - b.fair_entry, b.clv_net, b.realized_pnl
                           FROM bets b WHERE b.is_parlay = 1 AND b.clv IS NOT NULL
                                             AND b.venue IN ({','.join('?' * len(venues))})
                           ORDER BY b.opened_ts DESC""", venues).fetchall()


def linkage(conn):
    counts = dict(conn.execute("SELECT link, COUNT(*) FROM bets GROUP BY link"))
    unlinked = conn.execute("SELECT bet_id, opened_ts, stake FROM bets WHERE link = 'unlinked' ORDER BY opened_ts").fetchall()
    return counts, unlinked


def locked_bonus(conn, venue):
    """Polymarket `displayedBonus`, provisional (docs/polymarket-us.md). Kalshi exposes none."""
    if venue == "kalshi":
        return 0
    row = conn.execute("SELECT body_json FROM raw_pages WHERE venue = 'polymarket' AND endpoint = '/v1/account/balances'"
                       " ORDER BY id DESC LIMIT 1").fetchone()
    return round(json.loads(row[0])["balances"][0]["displayedBonus"] * ONE)


def own_money(conn, venue):
    """(account value - locked bonus) - net money put in. Bonuses and deposit fees land here, not in betting P&L."""
    # ponytail: open positions valued at cost, mark at mid once open bets are common
    open_cost = conn.execute("SELECT COALESCE(SUM(stake), 0) FROM bets WHERE venue = ? AND status = 'open'",
                             (venue,)).fetchone()[0]
    put_in = conn.execute("SELECT COALESCE(SUM(amount), 0) FROM cash_ledger WHERE venue = ?"
                          " AND kind IN ('deposit', 'withdrawal', 'perps_transfer')", (venue,)).fetchone()[0]
    return reported_cash(conn, venue) + open_cost - locked_bonus(conn, venue) - put_in


def labels(conn):
    """{bet_id: (label, legs)}: readable market text, IDs only when nothing else exists. NO sides read "No: ..."
    unless the market names its NO side (a team, Under, the other spread)."""
    text = {(v, m): (ly, ln, t) for v, m, ly, ln, t in
            conn.execute("SELECT venue, market_id, label_yes, label_no, title FROM markets")}
    legs = {}
    for v, m, leg, side in conn.execute("SELECT venue, market_id, leg_market_id, leg_outcome FROM parlay_legs ORDER BY rowid"):
        legs.setdefault((v, m), []).append((leg, side))

    def side_label(v, m, side):
        ly, ln, title = text.get((v, m), (None, None, None))
        if side == "no" and ln:
            return ln
        t = ly or title or m
        return f"No: {t}" if side == "no" else t

    out = {}
    for bet_id, v, m, outcome, is_parlay in conn.execute("SELECT bet_id, venue, market_id, outcome, is_parlay FROM bets"):
        if not is_parlay:  # the market title (game or question) as context, when it adds something
            label, title = side_label(v, m, outcome), text.get((v, m), (None, None, None))[2]
            out[bet_id] = (label, [title] if title and title not in label else [])
            continue
        kalshi_title = v == "kalshi" and text.get((v, m), (None,))[0]
        if kalshi_title:  # "yes A,no B": the parlay title lists its legs
            ls = [p[4:] if p.startswith("yes ") else f"No: {p[3:]}" if p.startswith("no ") else p
                  for p in (x.strip() for x in kalshi_title.split(","))]
        else:
            ls = [side_label(v, leg, side) for leg, side in legs.get((v, m), [])]
        name = f"{len(ls)}-leg parlay" if ls else "Parlay"
        out[bet_id] = (f"No: {name}" if outcome == "no" else name, ls)
    return out


def bet_result(status, outcome, payout, yes_value):
    if status == "open":
        return "open"
    if status == "settled":
        return "won" if payout > 0 else "lost"
    if status == "void":
        return "void"
    if yes_value in (0, ONE):
        return f"cashed out · would have {'won' if (yes_value == ONE) == (outcome == 'yes') else 'lost'}"
    return "cashed out"


def recent_bets(conn, n=10, venues=VENUES):
    """The n most recently closed bets on `venues`, newest first."""
    names = labels(conn)
    rows = conn.execute(f"""SELECT b.bet_id, b.venue, b.closed_ts, b.status, b.outcome, b.payout, m.yes_value, b.is_live,
                                  b.stake, b.clv_net, b.realized_pnl, b.is_parlay
                           FROM bets b LEFT JOIN markets m USING (venue, market_id)
                           WHERE b.status != 'open' AND b.venue IN ({','.join('?' * len(venues))})
                           ORDER BY b.closed_ts DESC LIMIT ?""", (*venues, n)).fetchall()
    return [{"bet_id": r[0], "venue": r[1], "closed_ts": r[2], "label": names[r[0]][0], "legs": names[r[0]][1],
             "result": bet_result(r[3], r[4], r[5], r[6]), "is_live": r[7], "stake": r[8], "clv_net": r[9], "pnl": r[10],
             "is_parlay": r[11]}
            for r in rows]


def all_bets(conn, venues=VENUES, split="all"):
    """Every bet on `venues` (open included), newest placed first, for the Bets tab."""
    names = labels(conn)
    rows = conn.execute(f"""SELECT b.bet_id, b.venue, b.opened_ts, b.closed_ts, b.status, b.outcome, b.payout, m.yes_value,
                                   b.is_live, b.stake, b.clv_net, b.realized_pnl, b.is_parlay, b.sport, b.market_type
                            FROM bets b LEFT JOIN markets m USING (venue, market_id)
                            WHERE b.venue IN ({','.join('?' * len(venues))})
                              AND b.is_parlay IN ({','.join('?' * len(SPLITS[split]))})
                            ORDER BY b.opened_ts DESC""", (*venues, *SPLITS[split])).fetchall()
    return [{"bet_id": r[0], "venue": r[1], "opened_ts": r[2], "closed_ts": r[3], "label": names[r[0]][0],
             "legs": names[r[0]][1], "result": bet_result(r[4], r[5], r[6], r[7]), "is_live": r[8], "stake": r[9],
             "clv_net": r[10], "pnl": r[11], "is_parlay": r[12], "sport": r[13], "market_type": r[14]} for r in rows]


def open_bets(conn, venues=VENUES):
    names = labels(conn)
    rows = conn.execute(f"""SELECT bet_id, venue, opened_ts, start_time, stake, avg_entry,
                                  stake - exit_proceeds + exit_fees, is_parlay FROM bets
                           WHERE status = 'open' AND venue IN ({','.join('?' * len(venues))})
                           ORDER BY opened_ts DESC""", venues).fetchall()
    # cost = stake net of partial exits, as in the bankroll check
    return [{"bet_id": r[0], "venue": r[1], "opened_ts": r[2], "label": names[r[0]][0], "legs": names[r[0]][1],
             "start_time": r[3], "stake": r[4], "avg_entry": r[5], "cost": r[6], "is_parlay": r[7]} for r in rows]
