# sharp-check

A personal, read-only dashboard for sports betting on prediction markets. It pulls my bet history from Kalshi and Polymarket US and answers one question: am I actually beating the market, or just getting lucky?

Python, SQLite and Plotly Dash. It only reads (account data and public market prices) and never places or changes orders.

## Status

Phases 1–4 and 6–8 are done, plus CLV for parlays. Phase 5 (sharp CLV) waits until paying for odds data is worth it.

- Every bet is rebuilt from the raw account history, and its P&L matches what each venue's app shows. Cash rebuilt from the bets matches each venue's reported balance to the cent.
- Every bet is linked to its game's start time (or flagged as a future), so it's known to be live or pregame.
- Actual vs. expected wins counts cash-outs by how their market finished (would the bet have won if held), so cashing out winners early doesn't skew it.
- Pregame single bets get closing line value (CLV) against each venue's own price at game start. There are three views: before fees, after fees (the "was this bet +EV" check), and how far the price moved before kickoff.
- Parlays whose legs are all from different games get CLV from their legs' prices, split into the markup the venue charged over the legs' fair price and how the legs moved before kickoff. Same-game parlays are listed but not scored, since their legs are correlated.
- Habit views show the bankroll curve, stake size after wins vs. losses, stake as a share of bankroll, how long before kickoff bets go in, what fees cost (with an estimate of what limit orders would have saved), and whether cashing out has made or cost money compared with holding.
- Every fill is priced against the venue's mid in the minute before it, so the spread paid getting in and getting out (cash-outs, parlays included) sits next to the fees, split pregame vs. in play.
- A live line-shop checker compares Kalshi and Polymarket all-in prices (ask plus taker fee) for upcoming NFL, college football and MLB games: moneylines, spreads, totals and NFL TD and yardage props, with the cheaper venue highlighted and a flag when little is offered at that price. Games and markets are matched across venues by Sportradar game ID, team, exact line and player name.
- Past single bets are priced the same way against the other venue in the minute they were placed, showing how much choosing one venue over the other left on the table.

## Dashboard

Seven tabs:

- **Overview**: headline P&L, ROI, CLV after fees for singles and parlays, and wins vs. expected, with open positions, recent bets and the bankroll curve. A Both / Kalshi / Polymarket switch filters the whole page.
- **CLV**: summaries by venue and every scored bet, filterable by venue.
- **Performance**: P&L by venue and bet type, own-money P&L, and segments.
- **Bets**: every bet, with venue and type filters and a search box.
- **Habits**: bankroll, cash-outs (with the exit spread), stake sizing, timing, fees, spread paid and line shopping on past bets.
- **Line shop**: pick an upcoming game and compare both venues' all-in prices per outcome. Prices are fetched live when you pick it; nothing is stored.
- **Data health**: cash reconcile, game linkage and anything that couldn't be scored or priced.

Bets show readable names ("Kyle Monangai 1+ touchdowns", "Detroit Lions −4.5", parlay legs), not market IDs. There are themes for light, dark, blue, Bears and Giants, and the layout works at phone width.

## Plans

1. **Raw archive** of all account data from both venues. *Done.*
2. **Headline P&L**: realized P&L, ROI, and actual vs. expected wins with confidence intervals. Parlays and singles are split, and P&L on my own money is kept separate from bonus money. *Done.*
3. **Game linkage**: sport, league, market type (including player props) and live vs. pregame, with segment views. *Done.*
4. **Market CLV**: entry price vs. the venue's own mid price at game start, for moneylines, spreads, totals and props, before and after fees. *Done.*
5. **Sharp CLV**: entry price vs. the de-vigged Pinnacle closing line. *Deferred (needs paid odds data).*
6. **Habit metrics**: bankroll curve, stake sizing after wins and losses, timing before kickoff, and fee cost. *Done.*
7. **Dashboard design and layout**: tabs, headline numbers up top, readable bet names, themes, phone width, a searchable list of every bet. *Done.*
8. **Execution cost**, in two parts:
   - **8a. Spread**: the spread paid when entering and when cashing out. *Done.*
   - **8b. Line shopping**: a live checker for where to place a bet, and the other venue's price at the moment of each past bet. *Done.*

Also done: **parlay CLV** for cross-game parlays and **cash-out analysis**. Possible later: more prop types and leagues in the line-shop checker, and a third venue (Novig).

## Setup

```sh
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest tests/
```

API credentials go in `.env`, which is gitignored: copy `.env.example` and fill it in.

## Usage

```sh
python -m sharp_check.archive     # pull new account data, closing prices and other-venue prices into the raw archive (read-only API calls)
python -m sharp_check.normalize   # rebuild every derived table from the archive
python -m sharp_check.reconcile   # check rebuilt cash against each venue's balance
python -m sharp_check.app         # dashboard at http://127.0.0.1:8050
```
