"""Phase 2: rebuild normalized tables from raw_pages. Derived tables are dropped and refilled every run.

Run: python -m sharp_check.normalize
"""
import csv
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal

from sharp_check import archive, classify, match
from sharp_check.archive import records

SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    venue TEXT NOT NULL,
    fill_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    outcome TEXT NOT NULL,      -- yes | no (Polymarket long | short)
    qty REAL NOT NULL,          -- contracts, fractional allowed
    price INTEGER NOT NULL,     -- micro-dollars per contract of `outcome`
    fee INTEGER NOT NULL,       -- micro-dollars, negative = rebate
    is_taker INTEGER NOT NULL,
    ts TEXT NOT NULL,           -- ISO 8601 UTC, microseconds
    PRIMARY KEY (venue, fill_id)
);
CREATE TABLE IF NOT EXISTS cash_ledger (
    venue TEXT NOT NULL,
    txn_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,         -- deposit | deposit_fee | withdrawal | bonus | perps_transfer
    amount INTEGER NOT NULL,    -- micro-dollars, signed: + into the account
    PRIMARY KEY (venue, txn_id, kind)
);
CREATE TABLE IF NOT EXISTS parlay_legs (
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,    -- the parlay's market
    leg_market_id TEXT NOT NULL,
    leg_outcome TEXT NOT NULL,  -- yes | no
    PRIMARY KEY (venue, market_id, leg_market_id)
);
CREATE TABLE IF NOT EXISTS settlements (
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    yes_value INTEGER NOT NULL, -- micro-dollars paid per YES contract; NO pays ONE - yes_value
    settled_ts TEXT NOT NULL,
    PRIMARY KEY (venue, market_id)
);
CREATE TABLE IF NOT EXISTS bets (
    bet_id TEXT PRIMARY KEY,    -- venue:market_id:n, n = position episode in that market
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    outcome TEXT NOT NULL,      -- yes | no, the side held
    is_parlay INTEGER NOT NULL,
    game_id TEXT,               -- phase 3
    opened_ts TEXT NOT NULL,
    is_live INTEGER,            -- phase 3
    entry_qty REAL NOT NULL,
    avg_entry INTEGER NOT NULL, -- micro-dollars per contract, fees excluded
    stake INTEGER NOT NULL,     -- entry cost + entry fees
    exit_qty REAL NOT NULL,
    exit_proceeds INTEGER NOT NULL, -- gross, before exit fees
    exit_fees INTEGER NOT NULL,
    payout INTEGER NOT NULL,
    realized_pnl INTEGER,       -- NULL while open
    status TEXT NOT NULL,       -- open | closed_early | settled | void
    closed_ts TEXT,             -- settlement, or last exit fill if closed early; NULL while open
    start_time TEXT,            -- game start; earliest game leg for parlays
    link TEXT,                  -- game | no_start | unlinked
    sport TEXT,
    market_type TEXT,
    close_mid INTEGER,          -- phase 4: venue mid at start_time, your side
    clv INTEGER,                -- close_mid - avg_entry, micro-dollars per contract
    clv_usd INTEGER,            -- entry_qty * clv, micro-dollars
    clv_net INTEGER,            -- close_mid - stake / entry_qty: CLV after entry fees, per contract
    clv_move INTEGER,           -- your side's close ask - avg_entry: did the price move for you (singles)
    fair_entry INTEGER,         -- parlays: product of leg mids at placement, your side of each leg
    entry_spread_usd INTEGER,   -- phase 8a: Σ qty × (price − mid before the fill) over entry fills; NULL if any unpriced
    exit_spread_usd INTEGER     -- same over exit fills; NULL if any unpriced or no exits
);
CREATE TABLE IF NOT EXISTS games (
    game_id TEXT PRIMARY KEY,   -- kalshi:<milestone id> | polymarket:<event slug>
    venue TEXT NOT NULL,
    league TEXT,
    title TEXT,
    start_time TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS markets (
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    event_id TEXT,
    title TEXT,
    sport TEXT,
    league TEXT,
    market_type TEXT,           -- moneyline | spread | total | prop | future | parlay | other; NULL for PM legs without a lookup
    line REAL,
    game_id TEXT,
    label_yes TEXT,             -- short text for the YES side
    label_no TEXT,              -- NO side when the market names it (two teams, over/under, spread); else NULL
    yes_value INTEGER,          -- final YES value from market data, NULL until resolved. Not cash: settlements drive cash
    PRIMARY KEY (venue, market_id)
);
CREATE TABLE IF NOT EXISTS quotes (
    venue TEXT NOT NULL,
    market_id TEXT NOT NULL,
    at_time TEXT NOT NULL,      -- a game start (close) or a parlay's placement (fair entry)
    quote_ts TEXT NOT NULL,
    yes_bid INTEGER NOT NULL,   -- micro-dollars
    yes_ask INTEGER NOT NULL,
    yes_mid INTEGER NOT NULL,
    PRIMARY KEY (venue, market_id, at_time)
);
CREATE TABLE IF NOT EXISTS fill_costs (
    venue TEXT NOT NULL,
    fill_id TEXT NOT NULL,
    bet_id TEXT NOT NULL,
    role TEXT NOT NULL,         -- entry | exit; a fill that flips a position has a row per bet
    qty REAL NOT NULL,          -- contracts of this fill in this role
    mid_before INTEGER,         -- mid of the fill's outcome just before it, micro-dollars; NULL when not computable
    cost INTEGER,               -- price - mid_before per contract, fees excluded; positive = paid over mid
    note TEXT,                  -- why cost is NULL, or how many parlay legs were priced as decided
    PRIMARY KEY (venue, fill_id, bet_id)
);
CREATE TABLE IF NOT EXISTS line_shop (
    venue TEXT NOT NULL,        -- your venue
    bet_id TEXT PRIMARY KEY,
    other_venue TEXT NOT NULL,
    other_market TEXT,          -- the other venue's cheapest priced ref; NULL when unpriced
    other_side TEXT,            -- yes | no: the side bought there
    is_live INTEGER,
    qty REAL NOT NULL,          -- your entry contracts
    yours INTEGER,              -- stake / entry qty: your all-in per contract, micro-dollars
    theirs INTEGER,             -- their ask + modeled taker fee at your first fill's quote key; NULL when unpriced
    their_ask INTEGER,
    their_fee INTEGER,
    gap INTEGER,                -- yours - theirs; positive = the other venue was cheaper
    note TEXT                   -- why theirs is NULL
);
"""

ONE = 1_000_000

# Hand-entered cash the APIs don't expose (e.g. Kalshi rewards). Gitignored; the one input outside raw_pages.
MANUAL_CASH = archive.DB_PATH.parent / "manual_cash.csv"
MANUAL_KINDS = {"bonus"}
MANUAL_SKIP = {"perps_bonus"}  # recorded for completeness; perps cash is out of scope

# Every fill becomes "acquire `outcome`". Selling long = acquiring short; exits net out in bets.
PM_INTENT = {
    "ORDER_INTENT_BUY_LONG": "yes",
    "ORDER_INTENT_SELL_SHORT": "yes",
    "ORDER_INTENT_BUY_SHORT": "no",
    "ORDER_INTENT_SELL_LONG": "no",
}


# Polymarket balance-change activity type -> (kind, sign).
PM_CASH = {
    "ACTIVITY_TYPE_ACCOUNT_DEPOSIT": ("deposit", 1),
    "ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL": ("withdrawal", -1),
    "ACTIVITY_TYPE_REFERRAL_BONUS": ("bonus", 1),  # incentive credit released as positions change
}


def micros(dollars):
    return int(Decimal(dollars) * ONE)


def iso_us(ts):
    """Fixed 6-digit fractions: Polymarket sends nanoseconds, Kalshi 5 or 6 digits, and fills are ordered as text."""
    head, _, frac = ts.rstrip("Z").partition(".")
    return f"{head}.{frac[:6].ljust(6, '0')}Z"


def iso_epoch(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def kalshi_fills(conn):
    for f in records(conn, "kalshi", ("/historical/fills", "/portfolio/fills"), "fills"):
        side = f["outcome_side"]  # never the deprecated side/action
        yield ("kalshi", f["fill_id"], f["order_id"], f["market_ticker"], side, float(f["count_fp"]),
               micros(f[f"{side}_price_dollars"]), micros(f["fee_cost"]), int(f["is_taker"]), iso_us(f["created_time"]))


def pm_fills(conn):
    for a in records(conn, "polymarket", ("/v1/portfolio/activities",), "activities"):
        t = a.get("trade")
        if t is None:
            continue
        if t["state"] != "TRADE_STATE_NEW":
            raise ValueError(f"Polymarket trade {t['id']}: unhandled state {t['state']}")
        mine = t["aggressorExecution"] if t["isAggressor"] else t["passiveExecution"]
        intent = mine["order"]["intent"]
        if intent not in PM_INTENT:
            raise ValueError(f"Polymarket trade {t['id']}: unhandled intent {intent}")
        outcome = PM_INTENT[intent]
        long_px = micros(mine["lastPx"]["value"])  # always quoted as the long price
        yield ("polymarket", t["id"], mine["order"]["id"], t["marketSlug"], outcome, float(mine["lastShares"]),
               long_px if outcome == "yes" else ONE - long_px,
               micros(mine["commissionNotionalCollected"]["value"]), int(t["isAggressor"]), iso_us(t["createTime"]))


def kalshi_cash(conn):
    for endpoint, kind, sign in (("/portfolio/deposits", "deposit", 1), ("/portfolio/withdrawals", "withdrawal", -1)):
        for r in records(conn, "kalshi", (endpoint,), endpoint.rsplit("/", 1)[1]):
            if r["status"] == "failed":
                continue
            if r["status"] != "applied":
                raise ValueError(f"Kalshi {kind} {r['id']}: unhandled status {r['status']}")
            ts = iso_epoch(r["created_ts"])
            yield ("kalshi", r["id"], ts, kind, sign * r["amount_cents"] * 10_000)
            if r["fee_cents"]:
                yield ("kalshi", r["id"], ts, f"{kind}_fee", -r["fee_cents"] * 10_000)


def kalshi_transfers(conn):
    """Prediction <-> perps moves. Perps are out of scope, so they're cash leaving or entering this ledger.

    Pending ones count: the reported balance already reflects them (reconciled to the cent, 2026-10-02).
    """
    for t in records(conn, "kalshi", ("/portfolio/intra_exchange_instance_transfers",), "transfers"):
        sign = (t["destination"] == "event_contract") - (t["source"] == "event_contract")
        if sign:  # event_contract -> event_contract is shard rebalancing, net zero
            yield ("kalshi", t["transfer_id"], iso_epoch(t["created_ts"]), "perps_transfer", sign * micros(t["amount"]))


def manual_cash(path):
    """Rows: venue,date,kind,amount,note. Amount in dollars, signed (+ into the account)."""
    if path is None or not path.exists():
        return
    with path.open(newline="") as f:
        for i, r in enumerate(csv.DictReader(f), start=2):  # line numbers; header is line 1
            if r["kind"] in MANUAL_SKIP:
                continue
            if r["kind"] not in MANUAL_KINDS:
                raise ValueError(f"{path.name} line {i}: unhandled kind {r['kind']}")
            yield (r["venue"], f"manual-{i}", f"{r['date']}T00:00:00.000000Z", r["kind"], micros(r["amount"]))


def pm_cash(conn):
    for a in records(conn, "polymarket", ("/v1/portfolio/activities",), "activities"):
        b = a.get("accountBalanceChange")
        if b is None:
            continue
        if a["type"] not in PM_CASH:
            raise ValueError(f"Polymarket balance change {b['transactionId']}: unhandled type {a['type']}")
        if b["status"] == "ACCOUNT_BALANCE_CHANGE_STATUS_REJECTED":
            continue
        # currentBalance includes a pending deposit but still holds a pending withdrawal (as a reservation)
        if b["status"] == "ACCOUNT_BALANCE_CHANGE_STATUS_PENDING" and a["type"] == "ACTIVITY_TYPE_ACCOUNT_WITHDRAWAL":
            continue
        if b["status"] not in ("ACCOUNT_BALANCE_CHANGE_STATUS_COMPLETED", "ACCOUNT_BALANCE_CHANGE_STATUS_PENDING"):
            raise ValueError(f"Polymarket balance change {b['transactionId']}: unhandled status {b['status']}")
        kind, sign = PM_CASH[a["type"]]
        yield ("polymarket", b["transactionId"], iso_us(b["createTime"]), kind, sign * micros(b["amount"]["value"]))


PM_LEG_SIDE = {"OUTCOME_SIDE_YES": "yes", "OUTCOME_SIDE_NO": "no"}


def kalshi_legs(conn):
    for m in records(conn, "kalshi", ("/markets/{ticker}",), "market"):
        for leg in m.get("mve_selected_legs") or []:
            yield ("kalshi", m["ticker"], leg["market_ticker"], leg["side"])


def pm_legs(conn):
    for a in records(conn, "polymarket", ("/v1/portfolio/activities",), "activities"):
        t = a.get("trade")
        for leg in (t or {}).get("comboLegDetails") or []:
            if leg["outcomeSide"] not in PM_LEG_SIDE:
                raise ValueError(f"Polymarket combo {t['marketSlug']}: unhandled leg side {leg['outcomeSide']}")
            yield ("polymarket", t["marketSlug"], leg["slug"], PM_LEG_SIDE[leg["outcomeSide"]])


def kalshi_settlements(conn):
    for m in records(conn, "kalshi", ("/markets/{ticker}",), "market"):
        if m["status"] == "finalized":
            yield ("kalshi", m["ticker"], micros(m["settlement_value_dollars"]), m["settlement_ts"])


PM_WINNER = {"POSITION_RESOLUTION_SIDE_LONG": ONE, "POSITION_RESOLUTION_SIDE_SHORT": 0}


def pm_settlements(conn):
    for a in records(conn, "polymarket", ("/v1/portfolio/activities",), "activities"):
        r = a.get("positionResolution")
        if r is None:
            continue
        yes = micros(json.loads(r["market"]["outcomePrices"])[0])  # long side first
        if yes in (0, ONE) and PM_WINNER.get(r["side"]) != yes:
            raise ValueError(f"Polymarket resolution {r['marketSlug']}: side {r['side']} disagrees with price {yes}")
        yield ("polymarket", r["marketSlug"], yes, iso_us(r["updateTime"]))


def derive_bets(conn, allocs=None):
    """One bet per position episode: first entry until flat or settled.

    Fills are all "acquire `outcome`", so an opposite-side fill is an exit at ONE - price. One that overshoots
    flat flips the position: the remainder opens a new bet, fee split by qty.
    allocs, if given, gets (venue, fill_id, bet_id, entry|exit, qty) per fill per bet it touches.
    """
    allocs = [] if allocs is None else allocs
    parlays = {r[0] for r in conn.execute("SELECT venue || ':' || market_id FROM parlay_legs")}
    settled = {(v, m): (y, t) for v, m, y, t in conn.execute("SELECT venue, market_id, yes_value, settled_ts FROM settlements")}
    bets, cur, n = [], None, {}

    def close(status, closed_ts=None):
        b = cur
        held = round(b["entry_qty"] - b["exit_qty"], 4)
        if status == "open":
            pnl = None
        else:
            if held and status != "closed_early":
                y = settled[(b["venue"], b["market_id"])][0]
                b["payout"] = held * (y if b["outcome"] == "yes" else ONE - y)
            pnl = b["exit_proceeds"] + b["payout"] - b["stake"] - b["exit_fees"]
        key = f"{b['venue']}:{b['market_id']}"
        bets.append((f"{key}:{b['n']}", b["venue"], b["market_id"], b["outcome"], key in parlays, None,
                     b["opened_ts"], None, b["entry_qty"], round(b["cost"] / b["entry_qty"]), round(b["stake"]),
                     b["exit_qty"], round(b["exit_proceeds"]), round(b["exit_fees"]), round(b["payout"]),
                     None if pnl is None else round(pnl), status, closed_ts))

    def finish():
        y, t = settled.get((cur["venue"], cur["market_id"]), (None, None))
        close("open" if y is None else "settled" if y in (0, ONE) else "void", t and iso_us(t))

    rows = conn.execute("SELECT venue, fill_id, market_id, outcome, qty, price, fee, ts FROM fills"
                        " ORDER BY venue, market_id, ts")
    for venue, fill_id, market, outcome, qty, price, fee, ts in rows:
        if cur and (cur["venue"], cur["market_id"]) != (venue, market):
            finish()
            cur = None
        if cur and outcome != cur["outcome"]:
            held = round(cur["entry_qty"] - cur["exit_qty"], 4)
            out = min(qty, held)
            allocs.append((venue, fill_id, f"{venue}:{market}:{cur['n']}", "exit", out))
            cur["exit_qty"] += out
            cur["exit_proceeds"] += out * (ONE - price)
            cur["exit_fees"] += fee * out / qty
            if round(held - out, 4) == 0:
                close("closed_early", iso_us(ts))
                cur = None
            qty, fee = round(qty - out, 4), fee * (qty - out) / qty
            if not qty:
                continue
        if cur is None:
            k = n[(venue, market)] = n.get((venue, market), 0) + 1
            cur = dict(venue=venue, market_id=market, outcome=outcome, n=k, opened_ts=ts, entry_qty=0.0, cost=0.0,
                       stake=0.0, exit_qty=0.0, exit_proceeds=0.0, exit_fees=0.0, payout=0.0)
        allocs.append((venue, fill_id, f"{venue}:{market}:{cur['n']}", "entry", qty))
        cur["entry_qty"] += qty
        cur["cost"] += qty * price
        cur["stake"] += qty * price + fee
    if cur:
        finish()
    return bets


DERIVED = ("fills", "cash_ledger", "parlay_legs", "settlements", "bets", "games", "markets", "quotes",
           "fill_costs", "line_shop", "closing_lines")  # closing_lines: old name of quotes, dropped from older DBs


def kalshi_games_markets(conn):
    """Yields ("game", row) and ("market", row). Traded markets first, so their titles beat bare leg rows."""
    by_event = archive.milestones_by_event(conn)
    for m in {m["id"]: m for m in by_event.values()}.values():
        yield "game", (f"kalshi:{m['id']}", "kalshi", m["details"].get("league"), m["title"], iso_us(m["start_date"]))
    # Latest page per ticker wins: a market re-fetched until finalized must take its final title and result.
    markets = list({m["ticker"]: m for m in records(conn, "kalshi", ("/markets/{ticker}",), "market")}.values())
    rows = [(m["ticker"], m.get("event_ticker"), m.get("title"), m.get("floor_strike"), m.get("yes_sub_title") or m.get("title"),
             micros(m["settlement_value_dollars"]) if m["status"] == "finalized" else None) for m in markets]
    rows += [(l["market_ticker"], l["event_ticker"], None, None, None, None)
             for m in markets for l in m.get("mve_selected_legs") or []]
    for ticker, event, title, line, label, yes in rows:  # a parlay's label is its title: "yes A,no B"
        league, sport, mtype = classify.kalshi_market(ticker)
        if mtype == "prop" and title:  # yes_sub_title drops the stat; "Game: Anytime Touchdown Scorer: X" -> "X: Anytime ..."
            parts = title.split(": ")
            label = f"{parts[-1]}: {parts[-2]}" if len(parts) > 2 else title
        ms = archive.kalshi_milestone(by_event, event) if mtype not in ("future", "parlay") else None
        yield "market", ("kalshi", ticker, event, title, sport, league, mtype, line, ms and f"kalshi:{ms['id']}",
                         label, None, yes)


def pm_games_markets(conn):
    """Games are yielded last: a start from a market lookup (re-fetched until resolved, so it sees postponements) beats
    one copied into a parlay trade when it was placed; among equals the later record wins."""
    stored = {m["slug"]: m for m in records(conn, "polymarket", ("/v1/market/slug/{slug}",), "market")}
    games = {}

    def game_row(game, league, event, start, from_lookup):
        if from_lookup or not games.get(game, (0,))[0]:
            games[game] = (from_lookup, (game, "polymarket", league, event, iso_us(start)))
    for a in records(conn, "polymarket", ("/v1/portfolio/activities",), "activities"):
        t = a.get("trade")
        if t is None:
            continue
        slug, legs = t["marketSlug"], t.get("comboLegDetails") or []
        if legs:
            yield "market", ("polymarket", slug, None, None, None, None, "parlay", None, None, None, None, None)
            for l in legs:  # game markets carrying their own start time; type, sides and result from the leg's lookup
                league, sport = classify.pm_league(l["slug"])
                game = l.get("eventStartTime") and f"polymarket:{l['eventSlug']}"
                m = stored.get(l["slug"])
                if game:
                    looked_up = (m or {}).get("gameStartTime")
                    game_row(game, league, l["eventSlug"], looked_up or l["eventStartTime"], bool(looked_up))
                if m is None:
                    yield "market", ("polymarket", l["slug"], l.get("eventSlug"), l.get("title"), sport, league, None,
                                     None, game or None, None, None, None)
                    continue
                league, sport, mtype = classify.pm_market(l["slug"], m.get("sportsMarketType"),
                                                          m.get("gameStartTime") or l.get("eventStartTime"))
                yield "market", ("polymarket", l["slug"], l.get("eventSlug"), m.get("question") or l.get("title"), sport,
                                 league, mtype, m.get("line"), game or None, *pm_labels(m), pm_yes_value(m))
            continue
        mine = t["aggressorExecution"] if t["isAggressor"] else t["passiveExecution"]
        event = mine["order"]["marketMetadata"]["eventSlug"]
        m = stored.get(slug, {})
        start = m.get("gameStartTime")
        league, sport, mtype = classify.pm_market(slug, m.get("sportsMarketType"), start)
        game = f"polymarket:{event}" if start and mtype != "future" else None
        if game:
            game_row(game, league, event, start, True)
        yield "market", ("polymarket", slug, event, m.get("question"), sport, league, mtype, m.get("line"), game,
                         *(pm_labels(m) if m else (None, None)), pm_yes_value(m))
    for _, row in games.values():
        yield "game", row


def pm_side_label(side, line):
    d, team = side.get("description") or "", (side.get("team") or {}).get("name")
    if d[:1] in ("+", "-") and team:
        return f"{team} {float(d):+g}"
    if d in ("Over", "Under"):
        return d if line is None else f"{d} {line:g}"
    return team or d


def pm_labels(m):
    """(YES label, NO label or None). Two named sides (teams, over/under, spread) label both; Yes/No markets one."""
    sides = m.get("marketSides") or []
    if len(sides) == 2 and [s.get("description") for s in sides] != ["Yes", "No"]:
        return pm_side_label(sides[0], m.get("line")), pm_side_label(sides[1], m.get("line"))
    team = sides and (sides[0].get("team") or {}).get("name")
    title, smt = m.get("title"), m.get("sportsMarketType") or ""
    if title and "_player_" in smt and not any(c.isdigit() for c in title):  # bare player name: add line and stat
        stat = smt.split("_player_")[1].replace("_", " ")
        title = f"{title} {stat}" if m.get("line") is None else f"{title} {m['line']:g}+ {stat}"
    return title or team or m.get("question"), None


def pm_yes_value(m):
    if m.get("status") != "MARKET_STATUS_RESOLVED" or not m.get("outcomePrices"):
        return None
    prices = m["outcomePrices"]
    return micros((json.loads(prices) if isinstance(prices, str) else prices)[0])  # long (YES) side first


def parlay_results(conn):
    """PM parlay yes_value from its legs: 0 once any leg lost, ONE once all won, else unknown (pending or void leg).
    Kalshi parlay markets settle themselves (finalized market record)."""
    yes = dict(conn.execute("SELECT market_id, yes_value FROM markets WHERE venue = 'polymarket'"))
    legs = {}
    for m, leg, side in conn.execute(
            "SELECT market_id, leg_market_id, leg_outcome FROM parlay_legs WHERE venue = 'polymarket'"):
        y = yes.get(leg)
        legs.setdefault(m, []).append(None if y not in (0, ONE) else (y == ONE) == (side == "yes"))
    conn.executemany("UPDATE markets SET yes_value = ? WHERE venue = 'polymarket' AND market_id = ?",
                     [(0 if False in won else ONE if all(won) else None, m) for m, won in legs.items()])


def link_rule(opened_ts, leg_starts):
    """(link, start_time, is_live) for a bet. leg_starts: start per game leg (a single is one leg), None if not found.

    Live if any leg had started when the bet opened; unknown legs only matter if none had.
    """
    if not leg_starts:
        return "no_start", None, None
    known = [s for s in leg_starts if s]
    start = min(known) if known else None
    if any(s <= opened_ts for s in known):
        return "game", start, 1
    if len(known) < len(leg_starts):
        return "unlinked", None, None
    return "game", start, 0


def link_bets(conn):
    markets = {(v, m): (g, s, t) for v, m, g, s, t in
               conn.execute("SELECT venue, market_id, game_id, sport, market_type FROM markets")}
    starts = dict(conn.execute("SELECT game_id, start_time FROM games"))
    legs = {}
    for v, m, leg in conn.execute("SELECT venue, market_id, leg_market_id FROM parlay_legs"):
        legs.setdefault((v, m), []).append(markets.get((v, leg), (None, None, None)))
    updates = []
    for bet_id, v, m, opened, is_parlay in conn.execute(
            "SELECT bet_id, venue, market_id, opened_ts, is_parlay FROM bets").fetchall():
        if is_parlay:
            info = legs.get((v, m), [])
            sports = {s for _, s, _ in info if s}
            game_id, sport, mtype = None, sports.pop() if len(sports) == 1 else "multi", "parlay"
        else:
            game_id, sport, mtype = markets.get((v, m), (None, None, None))
            info = [(game_id, sport, mtype)]
        leg_starts = [starts.get(g) for g, _, t in info if t != "future"]
        updates.append((game_id, sport, mtype, *link_rule(iso_us(opened), leg_starts), bet_id))  # same format as starts
    conn.executemany("UPDATE bets SET game_id = ?, sport = ?, market_type = ?, link = ?, start_time = ?, is_live = ?"
                     " WHERE bet_id = ?", updates)


def pick_close(start_s, points, window=archive.CLOSE_WINDOW):
    """Latest two-sided quote in [start - window, start]. points: (unix_s, yes_bid, yes_ask), micros or None."""
    ok = [p for p in points if start_s - window <= p[0] <= start_s
          and p[1] is not None and p[2] is not None and 0 < p[1] <= p[2] < ONE]
    return max(ok, default=None)


def kalshi_close(side):
    """Candle bid/ask close. Historical tier says `close`, live `close_dollars`; null on the synthetic candle."""
    side = side or {}
    v = side.get("close_dollars", side.get("close"))
    return None if v is None else micros(v)


def price_points(venue, body):
    if venue == "kalshi":
        return [(c["end_period_ts"], kalshi_close(c.get("yes_bid")), kalshi_close(c.get("yes_ask")))
                for c in body.get("candlesticks") or []]
    # Polymarket points are asks on both sides: YES bid = 1 - short ask.
    return [(p["timestamp"], None if p.get("shortPrice") is None else ONE - micros(str(p["shortPrice"])),
             None if p.get("longPrice") is None else micros(str(p["longPrice"]))) for p in body.get("history") or []]


def quotes(conn):
    """Latest usable YES quote at or before each archived (market, time). Times are whole seconds."""
    latest = {}
    for venue, endpoint in archive.CLOSE_ENDPOINTS.items():
        for (body,) in conn.execute("SELECT body_json FROM raw_pages WHERE venue = ? AND endpoint = ? ORDER BY id",
                                    (venue, endpoint)):
            b = json.loads(body)
            latest[(venue, b["market"], iso_epoch(b["start_ts"]))] = pick_close(b["start_ts"], price_points(venue, b))
    for (venue, market, at), q in latest.items():  # latest page wins, even if it has no usable quote
        if q:
            yield venue, market, at, iso_epoch(q[0]), q[1], q[2], round((q[1] + q[2]) / 2)


# One wide or stale leg quote can dominate a product (a Kalshi leg at 10/28 made a +71% markup), so leg quotes
# beyond these limits leave the parlay without CLV. Singles keep phase 4's looser rule (any quote in the hour).
LEG_MAX_SPREAD = 50_000  # 5 cents
LEG_MAX_AGE = 15 * 60    # seconds before the quote's time
SPREAD_MAX_AGE = 5 * 60  # seconds: an older quote says nothing about a fill (phase 8a)
PINNED = 10_000          # a leg quoted within 1c of 0 or 1 is decided


def parlay_close_mids(conn):
    """Eligible parlays: close_mid = product of leg mids at each leg's own start, fair_entry = product at placement.

    Each leg on the parlay's side (NO leg = ONE - YES mid). Any leg missing a usable quote at either time leaves the
    parlay NULL.
    """
    quotes = {(v, m, t): (q, bid, ask, mid) for v, m, t, q, bid, ask, mid in conn.execute("SELECT * FROM quotes")}

    def usable_mid(key):
        q = quotes.get(key)
        if q and q[2] - q[1] <= LEG_MAX_SPREAD and archive.epoch(key[2]) - archive.epoch(q[0]) <= LEG_MAX_AGE:
            return q[3]

    conn.execute("UPDATE bets SET close_mid = NULL, fair_entry = NULL, clv = NULL, clv_usd = NULL, clv_net = NULL"
                 " WHERE is_parlay = 1")
    at = lambda t: iso_epoch(archive.epoch(t))  # quotes are keyed by whole seconds
    updates = []
    for bet_id, (reason, opened, legs) in archive.parlay_clv_legs(conn).items():
        if reason:
            continue
        close = fair = 1.0
        for venue, leg, outcome, start in legs:
            c, e = usable_mid((venue, leg, at(start))), usable_mid((venue, leg, at(iso_us(opened))))
            if c is None or e is None:
                break
            close *= (c if outcome == "yes" else ONE - c) / ONE
            fair *= (e if outcome == "yes" else ONE - e) / ONE
        else:
            updates.append((round(close * ONE), round(fair * ONE), bet_id))
    conn.executemany("UPDATE bets SET close_mid = ?, fair_entry = ? WHERE bet_id = ?", updates)


def price_index(conn):
    """Every archived quote page merged per market, and the archived (venue, market, start_ts) pages (empty included).

    points: {(venue, market): [(unix_s, yes_bid, yes_ask)]} sorted by time; a later page wins on the same timestamp.
    Points with neither side quoted (Kalshi's synthetic candle) are skipped.
    """
    merged, pages = {}, set()
    for venue, endpoint in archive.CLOSE_ENDPOINTS.items():
        for (body,) in conn.execute("SELECT body_json FROM raw_pages WHERE venue = ? AND endpoint = ? ORDER BY id",
                                    (venue, endpoint)):
            b = json.loads(body)
            pages.add((venue, b["market"], b["start_ts"]))
            pts = merged.setdefault((venue, b["market"]), {})
            for t, bid, ask in price_points(venue, b):
                if bid is not None or ask is not None:
                    pts[t] = (bid, ask)
    return {k: sorted((t, *q) for t, q in v.items()) for k, v in merged.items()}, pages


def covered_points(points, pages, venue, market, key):
    """The market's points, or [] unless an archived page's window [s - CLOSE_WINDOW, s] covers the key: a merged
    point from a page that ended earlier could miss the minutes just before this fill."""
    ok = any(v == venue and m == market and s - archive.CLOSE_WINDOW <= key <= s for v, m, s in pages)
    return points.get((venue, market), []) if ok else []


def fill_bid_ask(points, key):
    """The latest YES quote at or before a key: ((bid, ask), None), or (None, why). It must be two-sided and at most 5
    minutes old; a one-sided latest point (Kalshi asks 1.00 once nobody offers) is never replaced by an older quote."""
    recent = [p for p in points if key - archive.CLOSE_WINDOW <= p[0] <= key]
    if not recent:
        return None, "no quote"
    t, bid, ask = recent[-1]
    if key - t > SPREAD_MAX_AGE:
        return None, "stale quote"
    if bid is None or ask is None or not 0 < bid <= ask < ONE:
        return None, "one-sided quote"
    return (bid, ask), None


def fill_quote(points, key):
    """A single's YES mid just before a fill: (mid, None), or (None, why). Width isn't limited: it's what we measure."""
    q, why = fill_bid_ask(points, key)
    return (None, why) if why else (round((q[0] + q[1]) / 2), None)


def leg_value(points, key, fetched, started, yes_value):
    """A parlay leg's YES value at an exit fill: (value, decided) or None. Rules: docs/pnl-rules.md (parlay exits).

    The latest point in the hour decides: pinned within 1c of 0/1 -> the final result, if known and agreeing (never
    an older in-play quote); none at all on a fetched (kept empty) page after kickoff -> the final result (market
    closed); otherwise it must be two-sided, at most 5c wide and 5 minutes old.
    """
    final = yes_value in (0, ONE)
    recent = [p for p in points if key - archive.CLOSE_WINDOW <= p[0] <= key]
    if not recent:
        return (yes_value, True) if fetched and started and final else None
    t, bid, ask = recent[-1]
    high, low = bid is not None and bid >= ONE - PINNED, ask is not None and ask <= PINNED
    if high or low:
        return (yes_value, True) if final and yes_value == (ONE if high else 0) else None
    if (bid is None or ask is None or not 0 < bid <= ask < ONE or ask - bid > LEG_MAX_SPREAD
            or key - t > SPREAD_MAX_AGE):
        return None
    return round((bid + ask) / 2), False


def parlay_exit_mid(clv_legs, ts, points, pages, final):
    """Mid of the side an exit fill acquires (the parlay's opposite): ONE − Π leg values on the parlay's side."""
    reason, _, legs = clv_legs
    if reason:
        return None, f"parlay not eligible: {reason}"
    fair, decided = 1.0, 0
    for venue, leg, side, start in legs:
        key = archive.fill_key(venue, ts)
        r = leg_value(covered_points(points, pages, venue, leg, key), key, (venue, leg, key) in pages, archive.epoch(start) < key,
                      final.get((venue, leg)))
        if r is None:
            return None, "parlay leg unpriced"
        fair *= (r[0] if side == "yes" else ONE - r[0]) / ONE
        decided += r[1]
    return round((1 - fair) * ONE), f"{decided} legs decided" if decided else None


def spread_costs(conn, allocs):
    """Price each fill against the mid just before it and sum per bet (docs/pnl-rules.md, spread). Needs clv_bets
    first: a parlay entry's mid is its fair_entry."""
    points, pages = price_index(conn)
    fills = {(v, f): (m, o, p, ts) for v, f, m, o, p, ts in conn.execute(
        "SELECT venue, fill_id, market_id, outcome, price, ts FROM fills")}
    bets = {b: (p, fe) for b, p, fe in conn.execute("SELECT bet_id, is_parlay, fair_entry FROM bets")}
    final = {(v, m): y for v, m, y in conn.execute("SELECT venue, market_id, yes_value FROM markets")}
    clv_legs = archive.parlay_clv_legs(conn)
    rows = []
    for venue, fill_id, bet_id, role, qty in allocs:
        market, outcome, price, ts = fills[(venue, fill_id)]
        is_parlay, fair_entry = bets[bet_id]
        if not is_parlay:
            key = archive.fill_key(venue, ts)
            mid, note = fill_quote(covered_points(points, pages, venue, market, key), key)
            mid = mid if mid is None or outcome == "yes" else ONE - mid
        elif role == "entry":
            mid, note = (fair_entry, None) if fair_entry is not None else (None, "no fair entry")
        else:
            mid, note = parlay_exit_mid(clv_legs[bet_id], ts, points, pages, final)
        rows.append((venue, fill_id, bet_id, role, qty, mid, None if mid is None else price - mid, note))
    conn.executemany("INSERT INTO fill_costs VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    sums = {}
    for _, _, bet_id, role, qty, _, cost, _ in rows:
        s = sums.setdefault((bet_id, role), [0.0, True])
        s[0] += 0 if cost is None else qty * cost
        s[1] &= cost is not None
    total = lambda b, r: round(sums[(b, r)][0]) if (b, r) in sums and sums[(b, r)][1] else None
    conn.executemany("UPDATE bets SET entry_spread_usd = ?, exit_spread_usd = ? WHERE bet_id = ?",
                     [(total(b, "entry"), total(b, "exit"), b) for b in {b for b, _ in sums}])

def line_shop_rows(conn):
    """The other venue's all-in price for each eligible single at its first fill (docs/pnl-rules.md, line shop).
    Needs spread_costs first: the side check compares with your side's mid before that fill."""
    points, pages = price_index(conn)
    pairs = archive.line_shop_counterparts(conn)
    own_mid = {}
    for bet_id, mid in conn.execute("""SELECT c.bet_id, c.mid_before FROM fill_costs c
                                       JOIN fills f ON f.venue = c.venue AND f.fill_id = c.fill_id
                                       WHERE c.role = 'entry' ORDER BY f.ts"""):
        own_mid.setdefault(bet_id, mid)
    fee_changes = {(c["series_ticker"], c["scheduled_ts"], c["fee_multiplier"]) for c in records(
        conn, "kalshi", ("/series/fee_changes",), "series_fee_change_arr")}
    rows = []
    for bet_id, venue, _, _, _, opened, _, _, _, stake, qty, is_live in archive.line_shop_bets(conn):
        reason, other, refs, oriented = pairs[bet_id]
        best, why = None, []
        key = archive.fill_key(other, opened)
        for market, side in refs:  # cheapest priced ref wins (a team win is YES on its market or NO on the other's)
            q, note = fill_bid_ask(covered_points(points, pages, other, market, key), key)
            if note:
                why.append(note)
                continue
            ask, _ = match.side_ask(*q, None, None, side)
            mult = match.kalshi_multiplier_at(fee_changes, market.split("-")[0], opened) if other == "kalshi" else None
            fee = round(match.kalshi_fee(ask, mult, stake=stake) if other == "kalshi"
                        else match.pm_fee(ask, match.pm_theta_at(opened)))
            mid = (q[0] + q[1]) / 2 if side == "yes" else ONE - (q[0] + q[1]) / 2
            best = min(best, (ask + fee, ask, fee, market, side, mid)) if best else (ask + fee, ask, fee, market, side, mid)
        if reason is None and best is None:
            reason = why[0] if why else "no quote"
        if best and own_mid.get(bet_id) is not None and abs(own_mid[bet_id] - best[5]) > match.SIDE_CHECK:
            best, reason = None, "side check failed"  # sides likely flipped
        elif best and own_mid.get(bet_id) is None and not oriented:
            best, reason = None, "team order unclear"  # no team code matched and no own mid to confirm the side
        yours = round(stake / qty) if qty else None
        theirs, ask, fee, market, side = best[:5] if best else (None,) * 5
        rows.append((venue, bet_id, other, market, side, is_live, qty, yours, theirs, ask, fee,
                     None if theirs is None else yours - theirs, reason))
    conn.executemany(f"INSERT INTO line_shop VALUES ({', '.join('?' * 13)})", rows)



def clv_bets(conn):
    """Market CLV: entry vs the venue's own mid at start, your side. NULL when not eligible, void, or no close."""
    conn.execute(f"""UPDATE bets SET close_mid = (
                         SELECT CASE bets.outcome WHEN 'yes' THEN c.yes_mid ELSE {ONE} - c.yes_mid END
                         FROM quotes c WHERE c.venue = bets.venue AND c.market_id = bets.market_id
                                         AND c.at_time = bets.start_time)
                     WHERE {archive.CLV_ELIGIBLE} AND status != 'void'""")
    parlay_close_mids(conn)
    # clv_move needs a closing ask, so the subquery is NULL for parlays (no quote on the parlay market).
    conn.execute(f"""UPDATE bets SET clv = close_mid - avg_entry,
                         clv_usd = CAST(ROUND(entry_qty * (close_mid - avg_entry)) AS INTEGER),
                         clv_net = CAST(ROUND(close_mid - stake / entry_qty) AS INTEGER),
                         clv_move = (SELECT CASE bets.outcome WHEN 'yes' THEN c.yes_ask ELSE {ONE} - c.yes_bid END
                                     FROM quotes c WHERE c.venue = bets.venue AND c.market_id = bets.market_id
                                                     AND c.at_time = bets.start_time) - avg_entry
                     WHERE close_mid IS NOT NULL""")


def rebuild(conn, manual_path=None):
    with conn:
        for table in DERIVED:  # dropped, not emptied, so schema changes between phases apply
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA)
    with conn:
        # Later pages repeat records with refreshed market metadata; last write wins.
        for rows in (kalshi_fills(conn), pm_fills(conn)):
            conn.executemany("INSERT OR REPLACE INTO fills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", list(rows))
        for rows in (kalshi_cash(conn), kalshi_transfers(conn), pm_cash(conn), manual_cash(manual_path)):
            conn.executemany("INSERT OR REPLACE INTO cash_ledger VALUES (?, ?, ?, ?, ?)", list(rows))
        for rows in (kalshi_legs(conn), pm_legs(conn)):  # every fill of a parlay repeats its legs
            conn.executemany("INSERT OR REPLACE INTO parlay_legs VALUES (?, ?, ?, ?)", list(rows))
        for rows in (kalshi_settlements(conn), pm_settlements(conn)):
            conn.executemany("INSERT OR REPLACE INTO settlements VALUES (?, ?, ?, ?)", list(rows))
        allocs = []
        conn.executemany(f"INSERT INTO bets VALUES ({', '.join('?' * 30)})",
                         (b + (None,) * 12 for b in derive_bets(conn, allocs)))
        for source in (kalshi_games_markets(conn), pm_games_markets(conn)):
            for kind, row in list(source):
                table = "games" if kind == "game" else "markets"
                conn.execute(f"INSERT OR {'REPLACE' if kind == 'game' else 'IGNORE'} INTO {table}"
                             f" VALUES ({', '.join('?' * len(row))})", row)
        parlay_results(conn)
        link_bets(conn)
        conn.executemany("INSERT INTO quotes VALUES (?, ?, ?, ?, ?, ?, ?)", list(quotes(conn)))
        clv_bets(conn)
        spread_costs(conn, allocs)
        line_shop_rows(conn)


if __name__ == "__main__":
    conn = archive.connect()
    rebuild(conn, MANUAL_CASH)
    for venue, n in conn.execute("SELECT venue, COUNT(*) FROM fills GROUP BY venue"):
        print(f"{venue:10} fills={n}")
    for venue, kind, n in conn.execute("SELECT venue, kind, COUNT(*) FROM cash_ledger GROUP BY venue, kind"):
        print(f"{venue:10} cash_ledger {kind}={n}")
    for venue, parlays, legs in conn.execute(
            "SELECT venue, COUNT(DISTINCT market_id), COUNT(*) FROM parlay_legs GROUP BY venue"):
        print(f"{venue:10} parlay_legs parlays={parlays} legs={legs}")
    for venue, n in conn.execute("SELECT venue, COUNT(*) FROM settlements GROUP BY venue"):
        print(f"{venue:10} settlements={n}")
    for venue, status, n in conn.execute("SELECT venue, status, COUNT(*) FROM bets GROUP BY venue, status"):
        print(f"{venue:10} bets {status}={n}")
    for venue, link, n in conn.execute("SELECT venue, link, COUNT(*) FROM bets GROUP BY venue, link"):
        print(f"{venue:10} bets link {link}={n}")
    # extend classify.py if a code here is really a main line or prop
    for code, n in conn.execute("""SELECT substr(market_id, 1, instr(market_id, '-') - 1), COUNT(*) FROM markets
                                   WHERE venue = 'kalshi' AND market_type = 'other' GROUP BY 1"""):
        print(f"kalshi     market_type other: {code}={n}")
    smt = {m["slug"]: m.get("sportsMarketType") for m in records(conn, "polymarket", ("/v1/market/slug/{slug}",), "market")}
    pm_other = Counter(smt.get(slug) for slug, in conn.execute(
        "SELECT market_id FROM markets WHERE venue = 'polymarket' AND market_type = 'other'"))
    for code, n in pm_other.items():
        if not classify.pm_expected_other(code):
            print(f"polymarket market_type other: {code}={n}")
