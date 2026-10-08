"""Dashboard. Run: python -m sharp_check.app  (after archive + normalize), then open http://127.0.0.1:8050"""
from contextlib import closing
from datetime import datetime

from dash import ClientsideFunction, Dash, Input, Output, State, ctx, dcc, html, no_update
import plotly.graph_objects as go

from sharp_check import archive, habits, lineshop, metrics, normalize, reconcile
from sharp_check.normalize import ONE

SCOPES = {"Both": metrics.VENUES, "Kalshi": ("kalshi",), "Polymarket": ("polymarket",)}
THEMES = ("system", "light", "dark", "blue", "bears", "giants")
VENUE = {"kalshi": "Kalshi", "polymarket": "PM"}


def usd(micros):
    return f"{'-' if micros < 0 else ''}${abs(micros) / ONE:,.2f}"


def cents(micros):
    return f"{micros / 10_000:.1f}¢"


def signed_cents(micros):
    return f"{micros / 10_000:+.1f}¢"


def when(ts):
    d = datetime.fromisoformat(ts[:19])
    return f"{d:%b} {d.day} {d:%H:%M}"


def tone(text, x):
    """Gain/loss colour on a signed value (never the theme accent)."""
    return html.Span(text, className="" if not x else "pos" if x > 0 else "neg")


def is_num(c):
    return isinstance(c, (int, float)) and not isinstance(c, bool) or isinstance(c, str) and bool(c) and (c[0] in "$-+−0123456789" or c.endswith(("¢", "%")))


def cell_is_num(c):
    head = c.children[0] if isinstance(c, html.Div) and c.children else c
    return is_num(c) or isinstance(head, html.Span) and is_num(head.children)


def table(header, rows, minor=()):
    """Plain table; columns in `minor` hide on a phone. Wide tables scroll inside their own box, never the page.
    A column is right-aligned, header included, when most of its non-empty cells are numbers."""
    rows = [list(r) for r in rows]
    num = set()
    for i in range(len(header)):
        cells = [r[i] for r in rows if r[i] not in ("", None)]
        if cells and sum(map(cell_is_num, cells)) * 2 > len(cells):
            num.add(i)

    def cell(tag, i, c):
        cls = " ".join(x for x in ("minor" if i in minor else "", "num" if i in num else "") if x)
        return tag(c, className=cls or None)
    head = html.Thead(html.Tr([cell(html.Th, i, h) for i, h in enumerate(header)]))
    body = html.Tbody([html.Tr([cell(html.Td, i, c) for i, c in enumerate(r)]) for r in rows])
    return html.Div(html.Table([head, body]), className="scroll")


def boxed(children):
    """Each section (an H3 and everything up to the next H3) in its own card; anything before the first H3 stays bare."""
    out, card = [], None
    for c in children:
        if isinstance(c, html.H3):
            card = html.Section([c], className="card")
            out.append(card)
        elif card is None:
            out.append(c)
        else:
            card.children.append(c)
    return out


def fold(summary, *children):
    return html.Details([html.Summary(summary), *children])


def bet_cell(label, legs, phone_line=None, desktop_only=False):
    """Label, then parlay legs (or a single's full market question, desktop only: too tall on a phone)."""
    parts = [html.Div(label)]
    if legs:
        parts.append(html.Div(" · ".join(legs), className="sub desk-only" if desktop_only else "sub"))
    if phone_line:
        parts.append(html.Div(phone_line, className="sub phone-only"))
    return html.Div(parts)


# ---- headline tiles ----

def tile(key, value, sub, sign=None, cls=""):
    v = "tile-v" + ("" if not sign else " pos" if sign > 0 else " neg")
    return html.Div([html.Div(key, className="tile-k"), html.Div(value, className=v), html.Div(sub, className="tile-s")],
                    className=f"tile {cls}".strip())


def clv_tile(key, c, n_key):
    if c["net_cents"] is None:
        return tile(key, "–", "no bets with CLV yet")
    ci = "" if c["net_ci"] is None else f"CI {c['net_ci'][0]:+.1f} to {c['net_ci'][1]:+.1f} · "
    return tile(key, f"{c['net_cents']:+.1f}¢", f"after fees · {ci}n {c[n_key]}", c["net_cents"])


def tiles(conn, venues):
    h = metrics.headline(conn, venues)
    lo, hi = h["ci"]
    roi = "–" if h["roi"] is None else f"{h['roi']:+.1%}"
    return [
        tile("Betting P&L", usd(h["pnl"]), f"ROI {roi} · {h['bets']} closed bets", h["pnl"]),
        tile("ROI", roi, f"on {usd(h['stake'])} staked", h["roi"], "roi"),
        clv_tile("CLV · singles", metrics.clv(conn, venues), "with_close"),
        clv_tile("CLV · parlays", metrics.parlay_clv(conn, venues), "with_clv"),
        tile("Wins vs expected", f"{h['wins']} / {h['expected']:.1f}",
             f"CI {max(lo, 0):.1f}–{hi:.1f} · {h['settled']} judged"),
    ]


# ---- header ----

def reconciled(conn):
    """True when both venues' fills and bets cash match the reported balance; None before any balance snapshot."""
    try:
        for venue in metrics.VENUES:
            reported = reconcile.reported_cash(conn, venue)
            for computed in (reconcile.computed_cash(conn, venue)[0], reconcile.bets_cash(conn, venue)):
                if abs(computed - reported) > reconcile.TOLERANCE:
                    return False
    except TypeError:  # no balance snapshot yet
        return None
    return True


def header(conn):
    synced = conn.execute("SELECT MAX(fetched_at) FROM raw_pages").fetchone()[0]
    ok = reconciled(conn)
    status = (html.Span("✓ reconciled", className="ok") if ok else
              html.Span("no balance yet", className="sub") if ok is None else
              html.Span("✗ cash mismatch (see Data health)", className="neg"))
    return html.Div([
        html.Div("sharp-check", className="brand"),
        html.Div([html.Span(f"Synced {when(synced)} UTC" if synced else "Never synced", className="sub"), status],
                 className="meta"),
        dcc.Dropdown(id="theme", options=[{"label": t.title(), "value": t} for t in THEMES], value="system",
                     clearable=False, searchable=False, persistence=True, persistence_type="local",
                     className="theme-pick"),
    ], className="hdr")


# ---- charts ----

def bankroll_figure(series):
    """Unthemed figure dict: shape and hover only. The browser adds theme colours (assets/theme.js)."""
    fig = go.Figure()
    x = [ts[:19] for ts, _, _ in series]
    for name, ys in (("Bankroll", [b for _, b, _ in series]), ("Cumulative betting P&L", [p for _, _, p in series])):
        fig.add_trace(go.Scatter(x=x, y=[y / ONE for y in ys], name=name, mode="lines", line_shape="hv",
                                 line={"width": 2}, hovertemplate=f"{name}: $%{{y:,.2f}}<extra></extra>"))
    fig.update_layout(hovermode="x unified", height=320, margin={"l": 56, "r": 16, "t": 16, "b": 40},
                      font={"family": "system-ui"}, legend={"orientation": "h", "y": 1.1, "x": 0},
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", yaxis={"tickprefix": "$"})
    return fig.to_plotly_json()


def bankroll_chart(series, graph_id):
    """The graph plus a store holding its unthemed figure; the theme callback paints the store into the graph."""
    fig = bankroll_figure(series)
    return html.Div([dcc.Graph(id=graph_id, figure=fig, config={"displayModeBar": False, "responsive": True}),
                     dcc.Store(id=f"{graph_id}-data", data=fig)])


# ---- Overview ----

def clv_text(clv_net, is_live):
    if clv_net is not None:
        return tone(signed_cents(clv_net), clv_net)
    return html.Span("live" if is_live else "–", className="sub")


def overview_body(conn, venues):
    """Open positions and recent bets for the chosen venues (re-rendered by the venue switch)."""
    opens = metrics.open_bets(conn, venues)
    recent = metrics.recent_bets(conn, venues=venues)
    return boxed([
        html.H3(["Open positions ", html.Span(f"{len(opens)} · {usd(sum(b['cost'] for b in opens))} at cost",
                                              className="sub")]),
        table(("Placed", "Bet", "Venue", "Starts", "Stake", "Entry"),
              [(when(b["opened_ts"]), bet_cell(b["label"], b["legs"], desktop_only=not b["is_parlay"]), VENUE[b["venue"]],
                when(b["start_time"]) if b["start_time"] else "–", usd(b["stake"]), cents(b["avg_entry"]))
               for b in opens], minor=(0, 2, 5)) if opens else html.P("None.", className="sub"),
        html.H3(["Recent bets ", html.Span(f"last {len(recent)} closed", className="sub")]),
        table(("Closed", "Bet", "Venue", "Result", "Stake", "CLV after fees", "P&L"),
              [(when(b["closed_ts"]),
                bet_cell(b["label"], b["legs"], f"{when(b['closed_ts'])} · {b['result']}" + (" · live" if b["is_live"] else ""),
                         desktop_only=not b["is_parlay"]),
                VENUE[b["venue"]], b["result"] + (" · live" if b["is_live"] else ""), usd(b["stake"]),
                clv_text(b["clv_net"], b["is_live"]), tone(usd(b["pnl"]), b["pnl"])) for b in recent],
              minor=(0, 2, 3, 4)),
    ])


def overview(conn):
    """The venue switch drives everything here: tiles, open positions, recent bets and the bankroll chart."""
    return [
        dcc.RadioItems(id="scope", options=list(SCOPES), value="Both", inline=True, className="seg"),
        html.Div(tiles(conn, metrics.VENUES), id="tiles", className="tiles"),
        html.Div(overview_body(conn, metrics.VENUES), id="overview-body"),
        html.H3("Bankroll"),
        bankroll_chart(habits.bankroll_series(conn), "bankroll-overview"),
    ]


# ---- CLV ----

def mean_text(m, ci=None):
    return "" if m is None else f"{m:+.1f}¢" + ("" if ci is None else f" ({ci[0]:+.1f} to {ci[1]:+.1f})")


def pct(x):
    return "" if x is None else f"{x:.0%}"


def singles_list(conn, venues):
    """(summary text, table) of single bets with CLV on `venues`."""
    names = metrics.labels(conn)
    singles = metrics.clv_bets(conn, venues)
    return f"Show all {len(singles)} singles with CLV", table(
        ("Start (UTC)", "Bet", "Type", "Entry", "Close mid", "After fees", "Vs mid", "Move", "CLV $", "P&L"),
        [(when(s), names[b][0], mt, cents(e), cents(c), tone(signed_cents(n), n), signed_cents(v), signed_cents(mv),
          usd(u), "" if p is None else tone(usd(p), p))
         for b, s, _, mt, _, e, c, v, n, mv, u, p in singles], minor=(0, 2, 4, 6, 7, 8))


def parlays_list(conn, venues):
    """(summary text, table) of parlays with CLV on `venues`."""
    names = metrics.labels(conn)
    parlays = metrics.parlay_bets(conn, venues)
    return f"Show all {len(parlays)} parlays with CLV", table(
        ("Placed (UTC)", "Bet", "Entry", "Fair at entry", "Fair at close", "Markup", "Markup %", "Movement",
         "After fees", "P&L"),
        [(when(o), bet_cell(names[b][0], names[b][1]), cents(e), cents(f), cents(c), signed_cents(mk),
          f"{mk / f:+.0%}", signed_cents(mv), tone(signed_cents(net), net), "" if p is None else tone(usd(p), p))
         for o, b, _, e, f, c, mk, mv, net, p in parlays], minor=(0, 3, 4, 5, 7))


def listed(key, summary_and_table):
    """A fold whose summary and contents the CLV venue switch replaces, so an open list stays open."""
    summary, body = summary_and_table
    return html.Details([html.Summary(summary, id=f"{key}-sum"), html.Div(body, id=f"{key}-list")])


def clv_tab(conn):
    rows = []
    for scope, venues in SCOPES.items():
        c = metrics.clv(conn, venues)
        rows.append((scope, f"{c['with_close']} / {c['eligible']}", mean_text(c["net_cents"], c["net_ci"]),
                     mean_text(c["mean_cents"], c["ci"]), pct(c["beat"]), mean_text(c["move_cents"]), pct(c["move_beat"]),
                     usd(c["net_usd"]), usd(c["clv_usd"]), usd(c["pnl"])))
    out = [
        dcc.RadioItems(id="clv-scope", options=list(SCOPES), value="Both", inline=True, className="seg"),
        html.Span(" filters the bet lists", className="sub"),
        html.H3("Singles"),
        table(("Venue", "With close / eligible", "After fees (95% CI)", "Vs mid (95% CI)", "Beat mid", "Price move",
               "Moved for you", "CLV $ after fees", "CLV $", "P&L"), rows, minor=(3, 4, 5, 6, 7, 8)),
        html.P("Close = the same venue's price at game start. After fees = close mid − all-in cost per contract"
               " (the +EV check). Vs mid = close mid − entry price. Price move = your side's close ask − entry price"
               " (timing only: a fair bet with no move scores 0). Dollar columns and P&L cover closed bets only."
               " Live bets, futures, other markets and voids have no CLV.", className="note"),
        listed("clv-singles", singles_list(conn, metrics.VENUES)),
    ]
    rows = []
    for scope, venues in SCOPES.items():
        c = metrics.parlay_clv(conn, venues)
        rows.append((scope, f"{c['with_clv']} / {c['eligible']}", mean_text(c["net_cents"], c["net_ci"]),
                     mean_text(c["mean_cents"], c["ci"]), mean_text(c["markup_cents"]),
                     "" if c["markup_pct"] is None else f"{c['markup_pct']:+.0%}", mean_text(c["move_cents"]),
                     pct(c["beat"]), usd(c["clv_usd"]), usd(c["pnl"])))
    out += [
        html.H3("Parlays"),
        table(("Venue", "With CLV / eligible", "After fees (95% CI)", "Vs mid (95% CI)", "Markup", "Markup %",
               "Leg movement", "Beat mid", "CLV $", "P&L"), rows, minor=(3, 4, 6, 7, 8)),
        html.P("Fair price = product of the legs' mids on your side. Markup = what you paid over the legs' fair price"
               " when you placed the parlay (positive = overpaid); Markup % = total markup ÷ total fair price. Leg"
               " movement = fair price at each leg's own kickoff minus at placement. CLV = movement − markup. Only"
               " pregame parlays whose legs are all from different games: same-game legs are correlated, so"
               " multiplying them would understate the parlay.", className="note"),
        listed("clv-parlays", parlays_list(conn, metrics.VENUES)),
    ]
    return out


# ---- Performance ----

LIVE_LABEL = {0: "pregame", 1: "live"}


def headline_cells(h):
    lo, hi = h["ci"]
    return (h["bets"], usd(h["stake"]), tone(usd(h["pnl"]), h["pnl"]), "" if h["roi"] is None else f"{h['roi']:+.1%}",
            f"{h['wins']} / {h['settled']}", f"{h['expected']:.1f} ({max(lo, 0):.1f}–{hi:.1f})")


def own_money_rows(conn):
    total_own = total_bet = 0
    for scope, venues in list(SCOPES.items())[1:]:
        own = metrics.own_money(conn, venues[0])
        bet = metrics.headline(conn, venues)["pnl"]
        total_own, total_bet = total_own + own, total_bet + bet
        yield scope, usd(own), usd(bet), usd(own - bet)
    yield "Both", usd(total_own), usd(total_bet), usd(total_own - total_bet)


def performance_tab(conn):
    heads = [(scope, split, metrics.headline(conn, venues, split)) for scope, venues in SCOPES.items()
             for split in metrics.SPLITS]
    rows = [(scope, split, *headline_cells(h), h["open"]) for scope, split, h in heads]
    out = [
        html.H3("Betting P&L"),
        table(("Venue", "Type", "Closed bets", "Stake", "P&L", "ROI", "Wins / judged", "Expected wins (95% CI)", "Open"),
              rows, minor=(2, 3, 8)),
        html.P("Wins count bets held to settlement plus cash-outs judged by how their market finished (would it have"
               " won if held); voids and unresolved cash-outs are in P&L and ROI only.", className="note"),
        html.H3("Own-money P&L"),
    ]
    try:
        out.append(table(("Venue", "Own money", "Betting", "Bonus contribution"), list(own_money_rows(conn))))
        out.append(html.P("Own money = account value − locked bonus − net deposits. Locked bonus is provisional.",
                          className="note"))
    except TypeError:  # no balance snapshot yet
        out.append(html.P("Needs a balance snapshot (run the archive).", className="sub"))
    header = ("Segment", "Closed bets", "Stake", "P&L", "ROI", "Wins / judged", "Expected wins (95% CI)")
    for column, title in metrics.SEGMENTS.items():
        shown, hidden = metrics.segments(conn, column)
        out.append(html.H3(title))
        out.append(table(header, [(LIVE_LABEL.get(v, v) if column == "is_live" else v, *headline_cells(h))
                                  for v, h in shown], minor=(1, 2)))
        if hidden:
            out.append(html.P(f"{hidden} segment{'s' * (hidden > 1)} hidden (n < 30).", className="sub"))
    out.append(html.P("Live = first fill at or after game start. Futures and unlinked bets have no live/pregame.",
                      className="note"))
    return out


# ---- Bets ----

SPLIT_LABEL = {"all": "All", "singles": "Singles", "parlays": "Parlays"}


def bets_table(rows):
    return table(("Placed", "Bet", "Venue", "Type", "Result", "Stake", "CLV after fees", "P&L"),
                 [(when(b["opened_ts"]),
                   bet_cell(b["label"], b["legs"], f"{when(b['opened_ts'])} · {b['result']}" + (" · live" if b["is_live"] else ""),
                            desktop_only=not b["is_parlay"]),
                   VENUE[b["venue"]], " · ".join(x for x in (b["sport"], b["market_type"]) if x) or "–",
                   b["result"] + (" · live" if b["is_live"] else ""), usd(b["stake"]),
                   clv_text(b["clv_net"], b["is_live"]), "–" if b["pnl"] is None else tone(usd(b["pnl"]), b["pnl"]))
                  for b in rows], minor=(0, 2, 3, 4, 5))


def filter_bets(scope, split, query):
    """(count text, table) for the Bets tab: venue, singles/parlays, and a case-insensitive search over label, legs,
    venue, type and result."""
    with closing(archive.connect()) as conn:
        rows = metrics.all_bets(conn, SCOPES[scope], split)
    q = (query or "").strip().lower()
    if q:
        rows = [b for b in rows if q in " ".join([b["label"], *b["legs"], VENUE[b["venue"]], b["venue"], b["result"],
                                                    b["sport"] or "", b["market_type"] or ""]).lower()]
    return f"{len(rows)} bet{'' if len(rows) == 1 else 's'}", bets_table(rows)


def bets_tab(conn):
    rows = metrics.all_bets(conn)
    return [
        html.Div([
            dcc.RadioItems(id="bets-scope", options=list(SCOPES), value="Both", inline=True, className="seg"),
            dcc.RadioItems(id="bets-split", options=[{"label": v, "value": k} for k, v in SPLIT_LABEL.items()],
                           value="all", inline=True, className="seg"),
            dcc.Input(id="bets-search", type="search", placeholder="Search: player, team, parlay, won…",
                      debounce=0.3, className="search"),
        ], className="controls"),
        html.H3(["All bets ", html.Span(f"{len(rows)} bets", id="bets-count", className="sub")]),
        html.Div(bets_table(rows), id="bets-list"),
    ]


# ---- Habits ----

TAG_LABEL = {"after_win": "After a win", "after_loss": "After a loss", "after_push": "After a push",
             "first": "Nothing settled yet"}


def pct_cell(x):
    return "" if x is None else f"{x:.1%}"


def cash_out_section(conn):
    c = habits.cash_outs(conn)

    def value_cell(s):
        if not s["known"]:
            return "–"
        lo, hi = (None, None) if s["ci"] is None else (s["ci"][0] * s["known"], s["ci"][1] * s["known"])
        ci = None if lo is None else f"CI {lo:+,.0f} to {hi:+,.0f} $"
        return html.Div([tone(usd(s["value"]), s["value"])] + ([html.Div(ci, className="sub")] if ci else []))

    rows = [(label, s["n"], s["green"], f"{s['would_win']} of {s['known']}", value_cell(s),
             html.Div([usd(s["spread"]), html.Div(f"{s['spread_known']} of {s['n'] - s['in_play']} priced" + (f", {s['in_play']} live singles left out" if s["in_play"] else ""), className="sub")]),
             usd(s["fees"]))
            for label, s in (("All", c["summary"]["all"]), ("Singles", c["summary"]["singles"]),
                             ("Parlays", c["summary"]["parlays"]))]
    return [
        html.H3("Cash-outs"),
        table(("Type", "Cash-outs", "Sold in profit", "Won if held", "Value vs holding", "Exit spread",
               "Exit fees"), rows, minor=(1, 2, 6)),
        html.P("Value vs holding = what you got for the part you sold (after exit fees) minus what holding it would have"
               " paid. Positive = cashing out helped. Mostly luck at this sample size: cashing out at a fair price"
               " breaks even on average; its real cost is exit fees plus the exit spread (fair exit = your side's mid in"
               " the minute before the sale). Exit spread leaves out singles cashed out in play (still listed below): a score"
               " inside the quote minute swamps the spread; their typical cost is in the spread table.",
               className="note"),
        fold(f"Show all {len(c['rows'])} cash-outs", table(
            ("Closed", "Bet", "Entry", "Exit", "Fair exit", "Spread", "If held", "Got", "Holding paid", "Value"),
            [(when(r["closed_ts"]) if r["closed_ts"] else "open", r["label"] + (" (partial)" if r["partial"] else ""), cents(r["avg_entry"]),
              cents(r["exit_price"]), "–" if r["fair_exit"] is None else cents(r["fair_exit"]),
              "–" if r["spread"] is None else usd(r["spread"]), r["outcome"], usd(r["got"]),
              "–" if r["held_value"] is None else usd(r["held_value"]),
              "–" if r["value"] is None else tone(usd(r["value"]), r["value"])) for r in c["rows"]],
            minor=(0, 2, 3, 4, 7, 8))),
    ]


def spread_section(conn):
    s = habits.spreads(conn)
    t = s["timing"]
    per = lambda c: "–" if c is None else f"{c:+.2f}¢"
    return [
        table(("Venue", "Type", "Spread at entry", "Spread at exit", "Per contract", "Priced fills"),
              [(v, split, usd(e), usd(x), per(c), f"{n} of {total}") if split != "singles in play" else
               (v, split, f"median {per(e)}", f"median {per(x)}", f"median {per(c)}", f"{n} of {total}")
               for v, split, e, x, c, n, total in s["rows"]], minor=(4, 5)),
        html.P(f"Spread = fill price minus the mid in the minute before the fill, your side, fees excluded; positive ="
               f" paid over mid, so it adds to the fees above. Parlay entries use the legs' fair price (the markup on"
               f" the CLV tab). Singles traded at or after kickoff are in the \"singles in play\" rows as medians per"
               f" contract: the mid is up to 1 minute old (2 on Polymarket) and a score inside that minute swamps the"
               f" spread, so a dollar total would be mostly news. Per contract (mean), by fill time: pregame {per(t['pregame'])}, in play {per(t['live'])}. Unpriced fills are listed on Data health.", className="note"),
        fold(f"Show all {len(s['bets'])} bets", table(
            ("Bet", "Entry spread", "Exit spread", "Note"),
            [(b["label"], "–" if b["entry"] is None else usd(b["entry"]),
              ("–" if b["exit"] is None else usd(b["exit"])) if b["has_exit"] else "", b["note"] or "")
             for b in s["bets"]])),
    ]


def line_shop_section(conn):
    s = habits.line_shop(conn)
    names = metrics.labels(conn)
    rows = [(VENUE[v], f"{n} of {e}", usd(left), mean_text(m, ci and (m - ci, m + ci)) or "–", f"{ch} of {n}")
            for v, n, e, left, m, ci, ch in s["rows"]]
    rows += [(VENUE[v], f"in play: {n}", "", f"median {m:+.1f}¢", "") for v, m, n in s["live"]]
    return [
        html.H3("Line shopping"),
        table(("Your venue", "Priced (pregame)", "Left on the table", "Mean gap (95% CI)", "Other venue cheaper"), rows),
        html.P("Gap = your all-in price per contract (stake ÷ contracts) minus the other venue's ask + modeled taker fee"
               " in the minute before your first fill; positive = the other venue was cheaper. Left on the table sums"
               " the positive gaps × your contracts. Pregame singles on moneylines, spreads, totals and NFL TD/yardage"
               " props both venues listed; in-play bets are a median only, because a score inside that minute swamps"
               " the comparison. Unpriced singles are listed on Data health.", className="note"),
        fold(f"Show all {len(s['bets'])} singles", table(
            ("Bet", "Yours", "Other venue", "Gap", "Note"),
            [(names.get(b["bet_id"], (b["bet_id"],))[0], "–" if b["yours"] is None else cents(b["yours"]),
              "–" if b["theirs"] is None else f"{cents(b['theirs'])} ({VENUE[b['other']]})",
              "–" if b["gap"] is None else signed_cents(b["gap"]),
              b["note"] or ("in play" if b["live"] else "")) for b in s["bets"]])),
    ]


def habits_tab(conn):
    rows = habits.bet_rows(conn)
    s = habits.stake_pct(rows)
    names = metrics.labels(conn)
    makers, total = habits.maker_fills(conn)
    return [
        html.H3("Bankroll"),
        bankroll_chart(habits.bankroll_series(conn), "bankroll-habits"),
        html.P("Bankroll = cash + open position cost, both venues. Deposits move bankroll, not betting P&L.",
               className="note"),
        *cash_out_section(conn),
        html.H3("Stake after a win vs a loss"),
        table(("Previous result", "Bets", "Median stake", "Median % of bankroll"),
              [(TAG_LABEL[t], n, "" if m is None else usd(round(m)), pct_cell(p))
               for t, n, m, p in habits.after_results(rows)]),
        html.P("Previous result = the last bet (either venue) settled or closed before this one was placed.",
               className="note"),
        html.H3("Stake as % of bankroll"),
        html.P(f"Median {pct_cell(s['median'])}, 90th percentile {pct_cell(s['p90'])}, max {pct_cell(s['max'])}."
               " Near 100% usually means a deposit placed straight into one bet, so this reflects funding habits as"
               " much as sizing."),
        fold("Show the 5 largest", table(("Placed (UTC)", "Bet", "Stake", "Bankroll before", "% of bankroll"),
                                         [(when(r["opened"]), names[r["bet_id"]][0], usd(r["stake"]),
                                           usd(r["bankroll"]), pct_cell(r["pct"])) for r in s["top"]], minor=(0, 3))),
        html.H3("Time placed before kickoff"),
        table(("Before kickoff", "Bets", "Median stake", "Bets with CLV", "Mean CLV after fees"),
              [(label, n, "" if m is None else usd(round(m)), nc, "hidden (n < 30)" if c is None else f"{c:+.1f}¢")
               for label, n, m, nc, c in habits.timing(rows)], minor=(3,)),
        html.P("Pregame bets with a known start time.", className="note"),
        html.H3("Execution cost"),
        table(("Venue", "Type", "Contracts", "Fees", "Per contract", "% of stake", "Est. maker saving"),
              [(v, split, f"{c:,.0f}", usd(f), f"{pc:.2f}¢", pct_cell(ps), "" if m is None else usd(m))
               for v, split, c, f, pc, ps, m in habits.fees(conn)], minor=(2, 4)),
        html.P(f"Maker fills: {makers} of {total}. Maker saving = taker fee paid minus the same fill's fee as a maker"
               " (Kalshi 0.0175 × C × p(1−p), assumed on every series; Polymarket rebate 0.0125 × C × p(1−p) from the"
               " schedule effective 2026-10-01, which postdates most fills), singles only. It assumes the limit order"
               " fills at the same price, ignoring the spread you'd also save and orders that never fill.",
               className="note"),
        *spread_section(conn),
        *line_shop_section(conn),
    ]


# ---- Line shop ----

def lineshop_tab(conn):
    return [
        html.H3("Line shop"),
        html.P("Pregame NFL, college football and MLB games in the next 48 hours that both venues list. All-in = ask +"
               " taker fee per contract at a $10 stake; the cheaper venue is bold. \"thin\" = less than $10 offered"
               " at that price (Polymarket's book is checked only where it's cheaper). Live prices, fetched when you"
               " pick a game; nothing is stored.", className="note"),
        html.Div([dcc.Dropdown(id="ls-game", placeholder="Pick a game", clearable=False, className="ls-game"),
                  html.Button("Refresh", id="ls-refresh", n_clicks=0, className="ls-refresh")], className="ls-controls"),
        dcc.Loading(html.Div(id="ls-body")),
    ]


def ls_cell(c, best):
    if c is None:
        return "–"
    text = cents(c["all_in"]) + (f" (thin: ${c['thin']:.0f})" if c["thin"] is not None else "")
    return html.Span(text, className="best" if best else None)


def lineshop_body(out):
    parts = [html.P(f"Prices as of {when(out['as_of'])} UTC.", className="sub")]
    parts += [html.P(e, className="note") for e in out["errors"]]
    if not out["rows"]:
        return parts + [html.P("No outcome both venues list.", className="sub")]
    for g in dict.fromkeys(r["group"] for r in out["rows"]):
        rows = [r for r in out["rows"] if r["group"] == g]
        parts += [html.H4(g), table(("Outcome", "Kalshi", "Polymarket", "Gap"), [
            (r["label"], ls_cell(r["kalshi"], r["best"] == "kalshi"), ls_cell(r["polymarket"], r["best"] == "polymarket"),
             "–" if r["gap"] is None else "no clear edge" if r["best"] is None else cents(abs(r["gap"])))
            for r in rows])]
    return parts


# ---- Data health ----

def data_tab(conn):
    names = metrics.labels(conn)
    out = [html.H3("Cash reconcile")]
    try:
        rows = []
        for venue in metrics.VENUES:
            reported = reconcile.reported_cash(conn, venue)
            for label, computed in (("fills", reconcile.computed_cash(conn, venue)[0]),
                                    ("bets", reconcile.bets_cash(conn, venue))):
                diff = computed - reported
                rows.append((VENUE[venue], label, "OK" if abs(diff) <= reconcile.TOLERANCE else "MISMATCH", usd(diff)))
        out.append(table(("Venue", "Check", "Status", "Computed − reported"), rows))
        diff = habits.bankroll_check(conn)
        out.append(html.P(f"Bankroll end point vs reported cash + open cost: "
                          f"{'OK' if abs(diff) <= reconcile.TOLERANCE else 'MISMATCH'} ({usd(diff)}).", className="note"))
    except TypeError:
        out.append(html.P("No balance snapshot yet (run the archive).", className="sub"))
    counts, unlinked = metrics.linkage(conn)
    out += [html.H3("Game linkage"),
            html.P(", ".join(f"{k}: {n}" for k, n in sorted(counts.items(), key=lambda kv: str(kv[0])))
                   or "No bets yet.")]
    if unlinked:
        out.append(table(("Unlinked bet", "Opened (UTC)", "Stake"),
                         [(names[b][0], when(ts), usd(s)) for b, ts, s in unlinked]))
    missing = metrics.clv(conn)["missing"]
    if missing:
        out += [html.H3("Singles with no close"),
                table(("Bet", "Start (UTC)"), [(names.get(b, (b,))[0], when(s)) for b, s in missing])]
    excluded = metrics.parlay_clv(conn)["excluded"]
    if excluded:
        counts = {}
        for _, reason in excluded:
            counts[reason] = counts.get(reason, 0) + 1
        out += [html.H3("Parlays without CLV"),
                html.P(", ".join(f"{r} {n}" for r, n in sorted(counts.items())) + ".")]
        stuck = [b for b, r in excluded if r == "no usable leg quote"]
        if stuck:
            out.append(table(("Parlay with a missing, wide (> 5¢) or stale (> 15 min) leg quote",),
                             [(bet_cell(names[b][0], names[b][1]),) for b in stuck]))
    unpriced = habits.unpriced_fills(conn)
    if unpriced:
        out += [html.H3("Fills without a spread"),
                table(("Bet", "Fill (UTC)", "Role", "Reason"),
                      [(names.get(b, (b,))[0], when(ts), role, note) for b, ts, role, note in unpriced])]
    unmatched = habits.line_shop(conn)["unmatched"]
    if unmatched:
        out += [html.H3("Singles without a line-shop price"),
                table(("Bet", "Reason"), [(names.get(b, (b,))[0], note) for b, note in unmatched])]
    other = conn.execute("""SELECT venue, substr(market_id, 1, instr(market_id, '-') - 1), COUNT(*)
                            FROM markets WHERE market_type = 'other' GROUP BY 1, 2""").fetchall()
    if other:
        out += [html.H3("Unclassified market codes"),
                table(("Venue", "Code", "Markets"), [(VENUE[v], c, n) for v, c, n in other]),
                html.P("Extend classify.py if a code here is really a main line or prop.", className="note")]
    return out


# ---- page ----

def layout(conn=None):
    if conn is None:
        conn = archive.connect()  # read fresh on every page load
        conn.executescript(normalize.SCHEMA)  # a fresh clone has no rebuilt tables yet: render an empty dashboard
    tabs = (("Overview", "overview", overview), ("CLV", "clv", clv_tab), ("Performance", "performance", performance_tab),
            ("Bets", "bets", bets_tab),
            ("Habits", "habits", habits_tab), ("Line shop", "lineshop", lineshop_tab), ("Data health", "data", data_tab))
    return html.Div([
        header(conn),
        dcc.Tabs(id="tabs", value="overview", className="tabs", children=[
            dcc.Tab(label=label, value=value, children=html.Div(boxed(build(conn)), className="pane"),
                    className="tab", selected_className="tab--on") for label, value, build in tabs]),
        dcc.Store(id="theme-applied"),
    ], className="page")


app = Dash(__name__, title="sharp-check")
app.layout = layout


@app.callback(Output("tiles", "children"), Output("overview-body", "children"), Output("bankroll-overview-data", "data"),
              Input("scope", "value"), prevent_initial_call=True)
def scope_overview(scope):
    with closing(archive.connect()) as conn:
        venues = SCOPES[scope]
        return tiles(conn, venues), overview_body(conn, venues), bankroll_figure(habits.bankroll_series(conn, venues))


@app.callback(Output("clv-singles-sum", "children"), Output("clv-singles-list", "children"),
              Output("clv-parlays-sum", "children"), Output("clv-parlays-list", "children"),
              Input("clv-scope", "value"), prevent_initial_call=True)
def scope_clv_lists(scope):
    with closing(archive.connect()) as conn:
        return (*singles_list(conn, SCOPES[scope]), *parlays_list(conn, SCOPES[scope]))


@app.callback(Output("bets-count", "children"), Output("bets-list", "children"),
              Input("bets-scope", "value"), Input("bets-split", "value"), Input("bets-search", "value"),
              prevent_initial_call=True)
def bets_filter(scope, split, query):
    return filter_bets(scope, split, query)

@app.callback(Output("ls-game", "options"), Input("tabs", "value"), Input("ls-refresh", "n_clicks"),
              State("ls-game", "value"))
def ls_slate(tab, n, selected):
    if tab != "lineshop" or (ctx.triggered_id == "ls-refresh" and selected):
        return no_update
    try:
        games = lineshop.slate(refresh=lineshop.refresh_slate(ctx.triggered_id, selected))
    except Exception:  # venue down: empty list; never print response details
        return []
    return [{"label": f"{g['league']} · {g['title']} · {when(g['start'])} UTC", "value": g["key"]} for g in games]


@app.callback(Output("ls-body", "children"), Input("ls-game", "value"), Input("ls-refresh", "n_clicks"))
def ls_game(key, n):
    try:
        game = next((g for g in lineshop.slate() if g["key"] == key), None) if key else None
    except Exception:
        return html.P("Couldn't load the game list; try Refresh.", className="sub")
    if game is None:
        return html.P("Pick a game.", className="sub")
    return lineshop_body(lineshop.game_rows(game))


app.clientside_callback(  # assets/theme.js
    ClientsideFunction("sharp", "theme"),
    Output("theme-applied", "data"), Output("bankroll-overview", "figure"), Output("bankroll-habits", "figure"),
    Input("theme", "value"), Input("bankroll-overview-data", "data"), Input("bankroll-habits-data", "data"),
)

if __name__ == "__main__":
    app.run(debug=False)
