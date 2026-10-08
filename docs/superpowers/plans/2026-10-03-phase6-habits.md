# Phase 6 Habits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bankroll curve, stake after wins/losses, stake % of bankroll, timing before kickoff, and fee cost with an estimated maker saving.

**Architecture:** `normalize.derive_bets` gains `closed_ts`. A new `sharp_check/habits.py` computes every view at page load from `bets`, `fills`, `cash_ledger` and `parlay_legs`. `app.py` adds a "Habits" section with one Plotly chart.

**Tech Stack:** Python 3.12, SQLite, Plotly Dash, pytest.

**Spec:** `docs/superpowers/specs/2026-10-03-phase6-habits-design.md`

## Global Constraints

- Read-only; no new archiving.
- Money in integer micro-dollars (`ONE = 1_000_000`); timestamps compared only after `normalize.iso_us` (Kalshi fills/settlements sometimes have 5 fractional digits).
- Bankroll = account cash + open position cost, both venues combined.
- Maker θ: Kalshi 0.0175, Polymarket −0.0125, on `qty × p(1−p)`. Singles only.
- Segment rule: performance per bucket only when n ≥ 30.

## Review Focus

- A bet that settles after the next bet opens must not be that bet's previous result.
- Bankroll ≤ 0 before a bet (only possible before the first deposit) must not divide by zero: pct is None.
- Kalshi 5-digit timestamps must sort correctly against 6-digit ones (closed_ts vs opened_ts).
- A closed-early bet's `closed_ts` is its last exit fill, not its settlement.
- Empty DB (no bets) renders without errors.

---

### Task 1: `bets.closed_ts`

**Files:** Modify `sharp_check/normalize.py` (SCHEMA, `derive_bets`, `rebuild` insert width); Test `tests/test_normalize.py`.

**Interfaces:** Produces `bets.closed_ts TEXT` (iso_us format) right after `status`; NULL for open.

- [ ] Failing test: extend `test_derive_bets_matches_known_app_pnl` style with a new test:

```python
def test_closed_ts_is_settlement_or_last_exit_fill():
    conn = archive.connect(":memory:")
    conn.executescript(normalize.SCHEMA)
    bet_fill(conn, "kalshi", "a1", "S", "yes", 1, 400_000, 0, "2026-01-01T00:00:00.00001Z")
    conn.execute("INSERT INTO settlements VALUES ('kalshi', 'S', 1000000, '2026-01-02T00:00:00.5Z')")
    bet_fill(conn, "kalshi", "b1", "E", "yes", 2, 400_000, 0, "2026-01-01T00:00:00Z")
    bet_fill(conn, "kalshi", "b2", "E", "no", 1, 500_000, 0, "2026-01-01T01:00:00Z")
    bet_fill(conn, "kalshi", "b3", "E", "no", 1, 500_000, 0, "2026-01-01T02:00:00.12345Z")
    bet_fill(conn, "kalshi", "c1", "O", "yes", 1, 400_000, 0, "2026-01-01T00:00:00Z")
    got = {b[2]: b[17] for b in normalize.derive_bets(conn)}
    assert got == {"S": "2026-01-02T00:00:00.500000Z", "E": "2026-01-01T02:00:00.123450Z", "O": None}
```

- [ ] Implement: `settled` dict maps to `(yes_value, settled_ts)`; `close(status, closed_ts)` appends `closed_ts` as the 18th tuple item; closed-early passes `iso_us(ts)` of the current fill; `finish()` passes `iso_us(settled_ts)` or None. SCHEMA: `closed_ts TEXT, -- settlement or last exit fill; NULL while open` after `status`. Insert `27` placeholders, pad `(None,) * 9`.
- [ ] Full suite passes; commit "Phase 6: closed_ts on bets".

### Task 2: `habits.py` bankroll, tags, stake %, timing

**Files:** Create `sharp_check/habits.py`; Test `tests/test_habits.py`.

**Interfaces:**
- `bankroll_series(conn) -> list[(ts, bankroll, betting_pnl)]`
- `bankroll_check(conn) -> int` (final bankroll − (Σ reported cash + open stake), micros)
- `bet_rows(conn) -> list[dict]` keys `bet_id, opened, stake, bankroll, pct, tag, lead_s, clv_net`
- `after_results(rows) -> list[(tag, n, median_stake, median_pct)]` in order `after_win, after_loss, after_push, first`
- `stake_pct(rows) -> dict(median, p90, max, top=[row...5])`
- `timing(rows, min_n=30) -> list[(label, n, median_stake, n_clv, mean_clv_cents|None)]`

- [ ] Failing tests (in-memory DB with SCHEMA; helper inserts bets with opened/closed/pnl and ledger rows):

```python
def test_bankroll_moves_on_cash_and_closed_bets_only():
    conn = db()
    ledger(conn, "2026-01-01T00:00:00.000000Z", 100 * ONE)
    bet(conn, "a", "2026-01-02T00:00:00Z", 10 * ONE, closed="2026-01-03T00:00:00.000000Z", pnl=5 * ONE)
    bet(conn, "b", "2026-01-04T00:00:00Z", 20 * ONE)                       # open: no move
    assert [s[1:] for s in habits.bankroll_series(conn)] == [(100 * ONE, 0), (105 * ONE, 5 * ONE)]


def test_previous_result_is_last_bet_closed_before_opening():
    conn = db()
    ledger(conn, "2026-01-01T00:00:00.000000Z", 100 * ONE)
    bet(conn, "w", "2026-01-02T00:00:00Z", ONE, closed="2026-01-02T05:00:00.000000Z", pnl=ONE)
    bet(conn, "l", "2026-01-02T01:00:00Z", ONE, closed="2026-01-02T09:00:00.000000Z", pnl=-ONE, venue="polymarket")
    bet(conn, "x", "2026-01-02T06:00:00Z", 4 * ONE)   # l still running: previous is w
    bet(conn, "y", "2026-01-02T10:00:00Z", 2 * ONE)
    tags = {r["bet_id"]: r["tag"] for r in habits.bet_rows(conn)}
    assert tags == {"w": "first", "l": "first", "x": "after_win", "y": "after_loss"}
    r = {r["bet_id"]: r for r in habits.bet_rows(conn)}
    assert r["x"]["bankroll"] == 101 * ONE and abs(r["x"]["pct"] - 4 / 101) < 1e-12


def test_timing_buckets_lower_bound_inclusive_and_clv_hidden_below_min_n():
    conn = db()
    ledger(conn, "2026-01-01T00:00:00.000000Z", 100 * ONE)
    for i, lead in enumerate((60, 15 * 60, 3600, 6 * 3600, 30 * 3600)):
        bet(conn, f"b{i}", "2026-01-05T00:00:00Z", ONE, start=..., clv_net=10_000)  # start = opened + lead
    rows = habits.timing(habits.bet_rows(conn), min_n=1)
    assert [(r[0], r[1]) for r in rows] == [("< 15 min", 1), ("15 min–1 h", 1), ("1–6 h", 1), ("6–24 h", 1), ("> 24 h", 1)]
    assert rows[0][4] == 1.0
    assert habits.timing(habits.bet_rows(conn))[0][4] is None
```

(Write `start` concretely in the test from the lead; `db()`, `ledger()`, `bet()` helpers insert directly.)

- [ ] Implement `habits.py`:

```python
BUCKETS = ((15 * 60, "< 15 min"), (3600, "15 min–1 h"), (6 * 3600, "1–6 h"), (24 * 3600, "6–24 h"), (None, "> 24 h"))
TAGS = ("after_win", "after_loss", "after_push", "first")

def bankroll_series(conn):
    ev = [(iso_us(ts), a, 0) for ts, a in conn.execute("SELECT ts, amount FROM cash_ledger")]
    ev += [(c, p, p) for c, p in conn.execute("SELECT closed_ts, realized_pnl FROM bets WHERE closed_ts IS NOT NULL")]
    out, bank, pnl = [], 0, 0
    for ts, d, p in sorted(ev):
        bank, pnl = bank + d, pnl + p
        out.append((ts, bank, pnl))
    return out
```
`bet_rows`: bisect on series times (`bisect_left` → strictly before) for bankroll and on sorted `(closed_ts, pnl)` for the tag; `lead_s = archive.epoch(start) − archive.epoch(opened)` for `is_live = 0` with a start, else None; `pct = stake / bankroll if bankroll > 0 else None`.
- [ ] Full suite passes; commit "Phase 6: habit metrics".

### Task 3: fees and maker saving

**Files:** Modify `sharp_check/habits.py`; Test `tests/test_habits.py`.

**Interfaces:** `MAKER_THETA = {"kalshi": 0.0175, "polymarket": -0.0125}`; `fees(conn) -> list[(venue, split, contracts, fees, fee_cents_per_contract, fee_pct_of_stake, maker_saving|None)]`, split `singles|parlays`.

- [ ] Failing test:

```python
def test_fees_split_and_maker_saving():
    conn = db()
    fill(conn, "kalshi", "S", 10, 400_000, 170_000)       # single
    fill(conn, "polymarket", "P", 10, 500_000, 180_000)   # single
    fill(conn, "kalshi", "X", 5, 200_000, 50_000)         # parlay
    conn.execute("INSERT INTO parlay_legs VALUES ('kalshi', 'X', 'L', 'yes')")
    stake bets: ('kalshi','S',0)=4.17, ('polymarket','P',0)=5.18, ('kalshi','X',1)=1.05
    got = {(r[0], r[1]): r for r in habits.fees(conn)}
    assert got[("kalshi", "singles")][6] == round(170_000 - 0.0175 * 10 * 0.4 * 0.6 * ONE)   # 128_000
    assert got[("polymarket", "singles")][6] == round(180_000 + 0.0125 * 10 * 0.25 * ONE)     # 211_250
    assert got[("kalshi", "parlays")][6] is None
    assert got[("kalshi", "singles")][4] == 1.7   # cents per contract
```

- [ ] Implement `fees` (loop fills, parlay set from `parlay_legs`, stake per (venue, is_parlay) from `bets`).
- [ ] Commit "Phase 6: fee cost and maker saving".

### Task 4: dashboard "Habits" section

**Files:** Modify `sharp_check/app.py`. Load the `dataviz` skill before writing the chart.

- [ ] `habits_section(conn)`: chart (`dcc.Graph`, two lines: bankroll, cumulative betting P&L, step shape), check line (`OK` if `abs(bankroll_check) <= reconcile.TOLERANCE`), tables for after-results, stake % (median/p90/max + top 5), timing, fees with caveat text.
- [ ] Smoke: `python -c "from sharp_check import app; app.layout()"` on the real DB and on an empty `:memory:` DB via `habits` functions.
- [ ] Commit "Phase 6: habits dashboard section".

### Task 5: real run, docs, review

- [ ] `archive && normalize && reconcile`; print each habits view; `bankroll_check` within 5¢.
- [ ] Update `docs/data-model.md`, `docs/pnl-rules.md`, `docs/roadmap.md`; commit "Phase 6 done".
- [ ] Subagent review of phase 6 (user request).
