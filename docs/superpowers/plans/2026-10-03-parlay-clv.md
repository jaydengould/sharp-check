# Parlay CLV Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Market CLV for cross-game parlays with a markup / leg-movement split.

**Architecture:** `archive.parlay_clv_legs` defines eligibility once; `sync_closes` fetches leg windows at kickoff and placement; `normalize` renames `closing_lines` to `quotes` and derives parlay `close_mid`/`fair_entry`; `metrics.parlay_clv` + an app block show it.

**Tech Stack:** Python 3.12, SQLite, Dash, pytest.

**Spec:** `docs/superpowers/specs/2026-10-03-parlay-clv-design.md`

## Global Constraints

- Read-only GETs; same endpoints as phase 4.
- Micros; quote keys `iso_epoch(epoch(t))`.
- Leg side: YES mid or `ONE − YES mid`.

## Review Focus

- A leg shared by two parlays: one kickoff fetch, two placement fetches.
- A NO leg uses `1 − mid` at both times.
- Any missing leg quote → no CLV, reason `missing leg quote`.
- `opened_ts` with microseconds must still match its floored-second quote key.
- Singles CLV unchanged after the rename.

---

### Task 1: rename `closing_lines` → `quotes`
- [ ] Rename table/column (`start_time` → `at_time`), function `closing_lines` → `quotes`, SQL in `clv_bets`, tests. Keep `"closing_lines"` in `DERIVED` so the old table is dropped. Full suite green. Commit.

### Task 2: eligibility + archive leg windows
- [ ] Test `test_sync_closes_fetches_parlay_leg_windows` (fake getters; two eligible parlays sharing a leg; a same-game parlay fetches nothing).
- [ ] Implement `archive.parlay_clv_legs(conn)` and extend `sync_closes` todo with `(venue, leg, epoch(leg_start))` and `(venue, leg, epoch(opened))` for eligible parlays. Commit.

### Task 3: normalize parlay CLV
- [ ] Test `test_parlay_clv_products_and_breakdown` (YES + NO legs, quotes at both times; one leg missing a quote → NULL).
- [ ] `bets.fair_entry INTEGER` (insert width 28, pad 10); `parlay_close_mids(conn)` updates `close_mid`, `fair_entry`; shared UPDATE computes `clv`, `clv_usd`, `clv_net` for any bet with `close_mid`. Commit.

### Task 4: metrics + dashboard
- [ ] Test `test_parlay_clv_aggregates_and_reasons`.
- [ ] `metrics.parlay_clv`, `metrics.parlay_bets`, app "Parlays" block. Commit.

### Task 5: real run, docs, subagent review
- [ ] `archive && normalize && reconcile`; coverage query; docs; commit; subagent review; fix findings.
