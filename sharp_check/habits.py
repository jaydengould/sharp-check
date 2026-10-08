"""Phase 6 habit metrics from `bets`, `fills` and `cash_ledger`. Rules: docs/pnl-rules.md."""
import bisect
import statistics

from sharp_check import archive, match, metrics, reconcile
from sharp_check.metrics import VENUES
from sharp_check.normalize import ONE, iso_us

BUCKETS = ((15 * 60, "< 15 min"), (3600, "15 min–1 h"), (6 * 3600, "1–6 h"), (24 * 3600, "6–24 h"), (None, "> 24 h"))
TAGS = ("after_win", "after_loss", "after_push", "first")


def bankroll_series(conn, venues=VENUES):
    """[(ts, bankroll, cumulative betting P&L)] over `venues` (default both), one point per timestamp. Placing a bet turns cash into
    position cost, so only ledger cash and closed bets' realized P&L move it. Events sharing a timestamp (a deposit
    and its fee) are netted, so the curve never shows a fee before its deposit."""
    marks = ",".join("?" * len(venues))
    ev = [(iso_us(ts), a, 0) for ts, a in conn.execute(f"SELECT ts, amount FROM cash_ledger WHERE venue IN ({marks})",
                                                          venues)]
    ev += [(c, p, p) for c, p in conn.execute(f"SELECT closed_ts, realized_pnl FROM bets WHERE closed_ts IS NOT NULL"
                                              f" AND venue IN ({marks})", venues)]
    out, bank, pnl = [], 0, 0
    for ts, d, p in sorted(ev, key=lambda e: e[0]):
        bank, pnl = bank + d, pnl + p
        if out and out[-1][0] == ts:
            out[-1] = (ts, bank, pnl)
        else:
            out.append((ts, bank, pnl))
    return out


def bankroll_check(conn):
    """Final bankroll - (reported cash + open position cost), micros. Open cost is net of partial exits, whose proceeds
    are already in cash. Only confirms the end point (much like reconcile), not the timing of each step."""
    series = bankroll_series(conn)
    open_cost = conn.execute("SELECT COALESCE(SUM(stake - exit_proceeds + exit_fees), 0) FROM bets"
                             " WHERE status = 'open'").fetchone()[0]
    return (series[-1][1] if series else 0) - sum(reconcile.reported_cash(conn, v) for v in VENUES) - open_cost


def bet_rows(conn):
    """Every bet with the bankroll just before it, its previous result and its lead time before kickoff.

    Previous result = the bet (any venue) closed latest strictly before this one opened: what was known at the click.
    """
    series = bankroll_series(conn)
    times = [s[0] for s in series]
    # A void is a push whatever it paid (pnl-rules: voids have no win/loss).
    closed = sorted(conn.execute("SELECT closed_ts, CASE status WHEN 'void' THEN 0 ELSE realized_pnl END FROM bets"
                                 " WHERE closed_ts IS NOT NULL"))
    ctimes = [c[0] for c in closed]
    rows = []
    for bet_id, opened, stake, start, is_live, clv_net in conn.execute(
            # Singles' CLV only: a parlay's is on another price scale and measured at each leg's own kickoff.
            "SELECT bet_id, opened_ts, stake, start_time, is_live, CASE is_parlay WHEN 0 THEN clv_net END FROM bets"):
        opened = iso_us(opened)
        i, j = bisect.bisect_left(times, opened), bisect.bisect_left(ctimes, opened)
        bank = series[i - 1][1] if i else 0
        prev = closed[j - 1][1] if j else None
        rows.append({
            "bet_id": bet_id, "opened": opened, "stake": stake, "bankroll": bank,
            "pct": stake / bank if bank > 0 else None,
            "tag": "first" if prev is None else "after_win" if prev > 0 else "after_loss" if prev < 0 else "after_push",
            "lead_s": archive.epoch(start) - archive.epoch(opened) if start and is_live == 0 else None,
            "clv_net": clv_net,
        })
    return sorted(rows, key=lambda r: r["opened"])


def median(xs):
    return statistics.median(xs) if xs else None


def after_results(rows):
    """[(tag, n, median stake, median stake % of bankroll)]."""
    out = []
    for tag in TAGS:
        g = [r for r in rows if r["tag"] == tag]
        out.append((tag, len(g), median([r["stake"] for r in g]), median([r["pct"] for r in g if r["pct"] is not None])))
    return out


def stake_pct(rows):
    pcts = sorted(r["pct"] for r in rows if r["pct"] is not None)
    return {
        "median": median(pcts),
        "p90": statistics.quantiles(pcts, n=10)[-1] if len(pcts) > 1 else None,
        "max": pcts[-1] if pcts else None,
        "top": sorted((r for r in rows if r["pct"] is not None), key=lambda r: -r["pct"])[:5],
    }


def bucket(lead_s):
    return next(label for bound, label in BUCKETS if bound is None or lead_s < bound)


def timing(rows, min_n=30):
    """[(bucket, n, median stake, bets with CLV, mean CLV after fees in cents or None when n < min_n)], pregame only."""
    out = []
    for _, label in BUCKETS:
        g = [r for r in rows if r["lead_s"] is not None and bucket(r["lead_s"]) == label]
        clv = [r["clv_net"] / 10_000 for r in g if r["clv_net"] is not None]
        out.append((label, len(g), median([r["stake"] for r in g]), len(clv),
                    sum(clv) / len(clv) if len(clv) >= min_n else None))
    return out


# Hypothetical maker fee coefficient on qty x p(1-p) (checked 2026-10-03; no API reports it for past fills).
# Polymarket US: maker rebate 0.0125 (docs.polymarket.us/fees.md, effective 2026-10-01).
# Kalshi: 0.0175 on `quadratic_with_maker_fees` series, 0 on `quadratic`. Assumed everywhere: savings understated.
MAKER_THETA = {"kalshi": 0.0175, "polymarket": -0.0125}


def fees(conn):
    """[(venue, singles|parlays, contracts, fees, cents per contract, share of stake, maker saving or None)].

    Maker saving = actual fee - the same fill's maker fee, taker fills on singles only (Kalshi parlays are RFQ-quoted; Polymarket
    combos have their own schedule). Assumes a limit order fills at the same price.
    """
    parlays = set(conn.execute("SELECT DISTINCT venue, market_id FROM parlay_legs"))
    stakes = {(v, "parlays" if p else "singles"): s for v, p, s in
              conn.execute("SELECT venue, is_parlay, SUM(stake) FROM bets GROUP BY 1, 2")}
    acc = {}
    for venue, market, qty, price, fee, taker in conn.execute(
            "SELECT venue, market_id, qty, price, fee, is_taker FROM fills"):
        split = "parlays" if (venue, market) in parlays else "singles"
        a = acc.setdefault((venue, split), [0.0, 0, 0.0])
        a[0] += qty
        a[1] += fee
        if split == "singles" and taker:
            p = price / ONE
            a[2] += fee - MAKER_THETA[venue] * qty * p * (1 - p) * ONE
    return [(v, split, round(c, 4), f, round(f / c / 10_000, 2), f / stakes[(v, split)] if stakes.get((v, split)) else None,
             round(m) if split == "singles" else None)
            for (v, split), (c, f, m) in sorted(acc.items())]


def maker_fills(conn):
    return conn.execute("SELECT COUNT(*), (SELECT COUNT(*) FROM fills) FROM fills WHERE is_taker = 0").fetchone()


def in_play(start, ts):
    """A fill at or after kickoff. A single's spread then mixes the bid-ask gap with in-game moves inside the quote
    minute (a goal: +48.5¢), so live singles are summarised by median, not summed. Parlay legs are guarded by the leg
    rules (decided -> final result, else ≤ 5¢ wide), so parlays stay summed."""
    return bool(start and ts) and archive.epoch(ts) >= archive.epoch(start)


def spreads(conn):
    """Spread paid (docs/pnl-rules.md, spread): per (venue, singles|parlays) entry and exit $, ¢ per priced contract and
    priced fills x of y; in-play single fills in their own row per venue as median ¢ per contract (entry, exit, all)
    instead of $; ¢ per contract pregame vs live; and each bet's entry and exit spread."""
    groups, timing = {}, {}
    for venue, is_parlay, start, ts, fill_id, role, qty, c in conn.execute(
            "SELECT c.venue, b.is_parlay, b.start_time, f.ts, c.fill_id, c.role, c.qty, c.cost"
            " FROM fill_costs c JOIN bets b USING (bet_id) LEFT JOIN fills f USING (venue, fill_id)"):
        live = in_play(start, ts)  # per fill: a pregame bet's in-play cash-out is live
        g = groups.setdefault((venue, "parlays" if is_parlay else "singles in play" if live else "singles"),
                              {"entry": 0.0, "exit": 0.0, "qty": 0.0, "priced": set(), "all": set(),
                               "per": {"entry": [], "exit": []}})
        g["all"].add(fill_id)
        if c is None:
            continue
        g["priced"].add(fill_id)
        g[role] += qty * c
        g["qty"] += qty
        g["per"][role].append(c / 10_000)
        if start and ts:
            t = timing.setdefault("live" if live else "pregame", [0.0, 0.0])
            t[0] += qty * c
            t[1] += qty
    cents = lambda total, qty: round(total / qty / 10_000, 2) if qty else None
    names = metrics.labels(conn)
    med = lambda xs: None if not xs else round(median(xs), 2)

    def row(v, split, g):
        if split == "singles in play":
            per = g["per"]
            return v, split, med(per["entry"]), med(per["exit"]), med(per["entry"] + per["exit"]), len(g["priced"]), len(g["all"])
        return (v, split, round(g["entry"]), round(g["exit"]), cents(g["entry"] + g["exit"], g["qty"]),
                len(g["priced"]), len(g["all"]))

    return {
        "rows": [row(v, split, g) for (v, split), g in sorted(groups.items())],
        "timing": {k: cents(*timing[k]) if k in timing else None for k in ("pregame", "live")},
        "bets": [{"bet_id": b, "label": names[b][0], "venue": v, "entry": e, "exit": x, "has_exit": bool(hx),
                  "note": n} for b, v, e, x, hx, n in conn.execute(
            """SELECT b.bet_id, b.venue, b.entry_spread_usd, b.exit_spread_usd, b.exit_qty > 0,
                      (SELECT group_concat(note, '; ') FROM fill_costs c
                       WHERE c.bet_id = b.bet_id AND c.cost IS NOT NULL AND c.note IS NOT NULL)
               FROM bets b WHERE b.bet_id IN (SELECT bet_id FROM fill_costs) ORDER BY b.opened_ts DESC""")],
    }


def unpriced_fills(conn):
    """[(bet_id, fill ts, role, reason)] for every fill without a spread, newest first."""
    return conn.execute("""SELECT c.bet_id, f.ts, c.role, c.note FROM fill_costs c JOIN fills f USING (venue, fill_id)
                           WHERE c.cost IS NULL ORDER BY f.ts DESC""").fetchall()


def cash_outs(conn):
    """Every bet with an exit (full or partial): what you got vs what holding the sold part would have paid.

    value = (exit proceeds − exit fees) − exit_qty × final value of your side; positive = cashing out helped.
    Rows on a void or unresolved market are pending and left out of the totals. Rules: docs/pnl-rules.md.
    """
    names = metrics.labels(conn)
    live = {b for b, start, ts in conn.execute("""SELECT c.bet_id, b.start_time, f.ts FROM fill_costs c
                                                  JOIN bets b USING (bet_id) JOIN fills f USING (venue, fill_id)
                                                  WHERE c.role = 'exit' AND b.is_parlay = 0""") if in_play(start, ts)}
    rows = []
    for r in conn.execute("""SELECT b.bet_id, b.closed_ts, b.is_parlay, b.status, b.outcome, b.exit_qty, b.avg_entry,
                                    b.exit_proceeds, b.exit_fees, b.realized_pnl, b.exit_spread_usd,
                                    m.yes_value
                             FROM bets b LEFT JOIN markets m USING (venue, market_id)
                             WHERE b.exit_qty > 0 ORDER BY b.closed_ts DESC""").fetchall():
        bet_id, closed, is_parlay, status, outcome, qty, entry, proceeds, fees, pnl, spread, yes = r
        side = None if yes not in (0, ONE) else yes if outcome == "yes" else ONE - yes
        got = proceeds - fees
        held = None if side is None else round(qty * side)
        rows.append({"bet_id": bet_id, "closed_ts": closed, "label": names[bet_id][0], "is_parlay": is_parlay,
                     "partial": status != "closed_early", "exit_qty": qty, "avg_entry": entry,
                     "exit_price": round(proceeds / qty), "got": got, "held_value": held,
                     "value": None if held is None else got - held, "fees": fees,
                     "spread": spread, "in_play": bet_id in live,
                     "fair_exit": None if spread is None else round((proceeds + spread) / qty),
                     "green": status == "closed_early" and pnl > 0,
                     "outcome": "pending" if side is None else f"would have {'won' if side == ONE else 'lost'}"})

    def summary(rs):
        known = [x for x in rs if x["value"] is not None]
        mean, half = metrics.mean_ci([x["value"] / ONE for x in known])
        return {"n": len(rs), "known": len(known), "green": sum(x["green"] for x in rs),
                "would_win": sum(x["outcome"] == "would have won" for x in rs),
                "value": sum(x["value"] for x in known), "mean": mean,
                "ci": None if half is None else (mean - half, mean + half), "fees": sum(x["fees"] for x in rs),
                # in-play single exits (any exit fill at or after kickoff) are left out: see in_play
                "spread": sum(x["spread"] for x in rs if x["spread"] is not None and not x["in_play"]),
                "spread_known": sum(x["spread"] is not None and not x["in_play"] for x in rs),
                "in_play": sum(x["in_play"] for x in rs)}

    return {"rows": rows, "summary": {"all": summary(rows), "singles": summary([x for x in rows if not x["is_parlay"]]),
                                      "parlays": summary([x for x in rows if x["is_parlay"]])}}


def line_shop(conn):
    """Phase 8b, past singles vs the other venue (docs/pnl-rules.md, line shop). Pregame bets are summed per your
    venue; in-play bets are a median gap only (a score inside the quote minute swamps the comparison)."""
    bets = [dict(zip(("venue", "bet_id", "other", "live", "qty", "yours", "theirs", "gap", "note", "game"), r))
            for r in conn.execute("SELECT l.venue, l.bet_id, l.other_venue, l.is_live, l.qty, l.yours, l.theirs, l.gap,"
                                  " l.note, b.game_id FROM line_shop l LEFT JOIN bets b USING (bet_id) ORDER BY l.bet_id")]
    rows, live = [], []
    for v in VENUES:
        pre = [b for b in bets if b["venue"] == v and not b["live"]]
        gaps = [b for b in pre if b["gap"] is not None]
        mean, ci = metrics.mean_ci([b["gap"] / 10_000 for b in gaps], [b["game"] or b["bet_id"] for b in gaps])
        rows.append((v, len(gaps), len(pre), round(sum(max(b["gap"], 0) * b["qty"] for b in gaps)), mean, ci,
                     sum(b["gap"] >= match.NO_EDGE for b in gaps)))
        in_play = [b["gap"] / 10_000 for b in bets if b["venue"] == v and b["live"] and b["gap"] is not None]
        if in_play:
            live.append((v, median(in_play), len(in_play)))
    return {"rows": rows, "live": live, "bets": bets,
            "unmatched": [(b["bet_id"], b["note"]) for b in bets if b["note"]]}
