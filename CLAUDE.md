# sharp-check

Personal, read-only dashboard: ingests my sports bets from Kalshi and Polymarket US, computes P&L, segment performance, and CLV. Python + SQLite + Plotly Dash.

## Hard rules

- Read-only. Never call order create/cancel/modify endpoints on either venue.
- Polymarket US only (`api.polymarket.us`, `gateway.polymarket.us`). Never use global Polymarket Gamma/CLOB/Data API docs or code.
- Never print, log, or commit secrets. Credentials live in `.env` and `secrets/` (both gitignored).
- Test fixtures from real API responses must have account IDs and balances scrubbed. The repo may go public later.
- `raw_pages` is the append-only source of truth; every other table must be rebuildable from it. Only exception: `data/manual_cash.csv` (hand-entered cash the APIs don't expose, e.g. Kalshi rewards).
- Timestamps in UTC. Money as integer micro-dollars.
- Build one phase at a time (see `docs/roadmap.md`). Don't build ahead of the current phase.

## Commands

- Setup: `python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`. All commands assume the venv is active. Tests: `python -m pytest tests/` (no network).
- Archive account data and closing prices: `python -m sharp_check.archive` (also rebuilds normalized tables). Rebuild normalized tables: `python -m sharp_check.normalize`. Cash check: `python -m sharp_check.reconcile` (prints only differences). Dashboard: `python -m sharp_check.app` (http://127.0.0.1:8050). Read-only API check: `python scripts/smoke.py` (also rewrites `tests/fixtures/`).

## Doc conventions (apply to this file and everything in `docs/`)

- Keep this file under 200 lines. When a topic outgrows a few lines, move it to `docs/<topic>.md` and leave a one-line pointer in the index below.
- Only write down what an agent can't get elsewhere: project-specific standards, decisions, domain knowledge, data sources, and recurring mistakes.
- Don't document stack common knowledge, general best practices, or anything discoverable by searching the code.
- Never preload `docs/` files. Read one only when the current task needs it. Each pointer below says *when* that is.
- Update "Recurring mistakes" whenever an agent gets the same thing wrong twice.
- When a change makes a doc wrong, fix the doc in the same commit.
- When the user says the session is over, check whether this file or anything in `docs/` is stale or missing something new, and update only what needs it.

## Working with the user

- Act as a sparring partner, not a yes-man. Push back on weak ideas, name blind spots, structural risks, and faulty assumptions, even when they didn't ask.
- Disagree with reasons, then defer once they've decided. Don't re-argue a settled call.

## Docs index

Read a file only when the task touches its topic.

<!-- Format: - `docs/<file>.md`: <what's in it>. Read when <trigger>. -->

- `docs/roadmap.md`: phases, current phase, decisions and open questions. Read when starting a session or a new phase.
- `docs/kalshi.md`: auth signing, endpoints, historical cutoff, fill normalization, game start times (milestones), line-shop listings, depth, fees and NFL prop series. Read when touching the Kalshi client, game linkage or line shop.
- `docs/polymarket-us.md`: auth signing, activities, events, market lookup, price history, known gaps, line-shop event filters, top of book and gateway throttling. Read when touching the Polymarket client, game linkage or line shop.
- `docs/data-model.md`: tables and keys. Read when changing schema or ingestion.
- `docs/pnl-rules.md`: stake, P&L, ROI, live/pregame, void, CLV (singles and parlays), habit-metric, spread and line-shop rules. Read when changing bet derivation or metrics.
- `docs/superpowers/`: per-phase design specs and implementation plans. Read when revisiting how a finished phase was designed.

## Recurring mistakes

<!-- Format: - <mistake> → <what to do instead>. -->

- Using Kalshi's deprecated `side`/`action` fill fields → use `outcome_side`. A sell-YES appears as `no`.
- Styling Dash 4 components from assumed markup → inspect the DOM first. `className` lands on a wrapper (`dcc.Input` → `div.dash-input-container`, `dcc.Dropdown` → `button.dash-dropdown`); RadioItems mark the chosen `label` with `.selected`; menus are `.dash-dropdown-content`. Never style bare `[role=option]` (RadioItems use it too).
- Treating any archived page as current → `raw_pages` keeps every version of a record (an old `active` market page outlives its `finalized` one). Key by id and let the latest page win (`records()` yields oldest first). A re-stored identical body is deduped, not appended.
- Picking the latest *usable* quote → a decided market quotes pinned or one-sided (Kalshi ask 1.00), gets skipped, and an older pre-result quote is used (−53¢ single, 0.86 for a won leg). The latest point decides; if it's unusable, no price.
