# Tradebot — TODO

> Work top-down. Don't start a phase until the previous phase's
> green-light test passes. Tick boxes as you go.

---

## Phase 1 — Foundation (code complete, awaiting green light on your VPS)

### Setup on the VPS
- [ ] Create a dedicated non-root Linux user for the bot (e.g. `tradebot`).
      Bot process must NOT run as root.
- [ ] Install Microsoft ODBC Driver 18:
  - [ ] Add Microsoft apt repo
  - [ ] `sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev`
- [ ] Clone/copy the `tradebot/` project to the VPS as the bot user.
- [ ] Create a Python venv: `python3 -m venv .venv && source .venv/bin/activate`
- [ ] `pip install -r requirements.txt`

### Master key
- [ ] Generate the master key ONCE:
      `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- [ ] Copy `.env.example` → `.env`, paste the key.
- [ ] `chmod 600 .env && chown tradebot:tradebot .env`
- [ ] **Back the master key up offline** (password manager / encrypted USB).

### Database
- [ ] In SSMS: `CREATE DATABASE tradebot;`
- [ ] Run `db/01_schema_phase1.sql` against the new database.
- [ ] Confirm the seeded `medium` risk profile is `is_active = 1`.
- [ ] Decide on final risk %: keep 1.0 or `UPDATE risk_profile SET max_risk_per_trade_pct = ?`.
- [ ] Add at least one watchlist row:
      `INSERT INTO watchlist (symbol, base_asset, quote_asset) VALUES ('BTC/USD','BTC','USD');`
- [ ] Decide: SQL Server same VPS or remote? Note connection in `.env`.
- [ ] Confirm SQL auth (recommended) — Windows/Kerberos needs a different connection string.

### Alpaca
- [ ] Create paper API key + secret in the Alpaca dashboard.
- [ ] Confirm the key is scoped trading-only (no withdrawal).
- [ ] Load env: `set -a; source .env; set +a`
- [ ] Run `python -m scripts.insert_credentials` and enter the paper key + secret.
- [ ] Verify in SSMS:
      `SELECT id, provider, key_label, environment, is_active FROM api_credentials;`

### Green-light test
- [ ] Run `python -m scripts.check_connectivity`
- [ ] All four lines must PASS:
  - [ ] DB reachable
  - [ ] Decrypt Alpaca creds
  - [ ] Alpaca paper account (returns equity/cash)
  - [ ] Live crypto quote (returns bid/ask)
- [ ] **PHASE 1 GREEN LIGHT achieved** → proceed to Phase 2

---

## Phase 2 — Market Data + TA Signal Engine (no orders yet) — CODE COMPLETE, green-light passed 2026-05-29

> All decisions below were made as documented config defaults (seeded in
> `app_config`, tunable in SQL). Confirm/retune the strategy knobs before
> Phase 3 commits paper trades — see "Defaults chosen" at the end of this phase.

### Design choices to make first
- [x] Base TA indicator set: EMA cross (trend) + RSI (momentum) + volume z-score
      (confirmation) + ATR (volatility/stop). Periods in `app_config` `ta.*`.
- [x] Timeframe: HOURLY on the just-closed 1h bar. `market_data.fetch_closed_bars`
      drops the forming bar by computing `now >= ts + duration`, so no fixed
      post-boundary sleep is needed — only genuinely closed bars are used.
- [x] **S/R detection method:** rolling extrema (max high / min low) over the
      `sr.lookback` (=20) bars BEFORE the latest closed bar. (NEW)
- [x] **Breakout confirmation rule:** volume-confirmed — `breakout.require_volume`
      (=true), volume z-score must clear `breakout.vol_z_min` (=1.0). (NEW)
- [x] **Confidence weights:** trend 40 / momentum 25 / volume 15 / breakout 20
      (`weights.*`); trend+momentum+volume = 80 base, breakout adds 20 → max 100. (NEW)
- [x] Cool-down rule after a stop-out: `cooldown.bars_after_stop` (=3) seeded
      now; consumed in Phase 3/4.

### Schema additions
- [x] `market_bars` (symbol, timeframe, ts_utc, OHLCV, trade_count, vwap) — cache OHLCV
- [x] `signals` (symbol, bar_ts_utc, signal, confidence, breakout_confirmed,
      entry/stop hints, S/R levels, indicator_snapshot JSON) — UNIQUE(symbol, bar_ts_utc)
- [x] **`sr_levels`** (symbol, support_price, resistance_price, method,
      lookback, bar_ts_utc, computed_at_utc) — recomputed each scan, persisted (NEW)
- [x] `db/02_schema_phase2.sql` applied to live `CryptoBotV2` DB; Phase 2
      strategy params seeded into `app_config`.

### Modules to build
- [x] `core/market_data.py` — fetch OHLCV via `alpaca-py`, closed-bars-only,
      upsert into `market_bars`; retry/backoff on transient Alpaca timeouts
- [x] `core/indicators.py` — pure functions: EMA, RSI (Wilder), ATR (Wilder), volume z-score
- [x] `core/config.py` — typed `app_config` accessor (strategy knobs from SQL)
- [x] **`core/levels.py`** — rolling-extrema S/R per symbol from closed bars;
      `persist_levels` → `sr_levels`, `latest_levels` reader for Phase 4 (NEW)
- [x] `core/ta_engine.py` — consume bars + levels → produce
      `(signal, confidence, entry_hint, stop_hint, support_level, resistance_level)`
      - **Uses only closed bars** (no look-ahead bias)
      - **Resistance breakout check:** confirmed close above resistance (+ volume
        rule) adds `weights.breakout` and sets `breakout_confirmed = true` (NEW)
      - BUY gate = trend AND momentum; breakout/volume only ADD confidence
- [x] `scripts/run_signal_scan.py` — manual run, prints per symbol:
      setup, confidence, breakout, RSI, support/resistance, entry/stop; persists all

### Green-light test (Phase 2) — PASSED 2026-05-29
- [x] For each active watchlist symbol, the engine outputs signal + 0-100 confidence
      (BTC/USD: setup=----, conf=25.0 on the 09:00 UTC bar)
- [x] Resistance breakout, when it occurs, visibly raises the confidence score:
      isolated test on fixed bars → breakout adds exactly +20.0 (== weights.breakout) (NEW)
- [x] Support & resistance levels are detected and persisted to `sr_levels` (NEW)
- [x] Signals are persisted to `signals`
- [x] No orders are placed
- [x] Spot-checked indicator math (EMA/RSI/ATR/vol-z) + closed-bar timing
      (latest bar 09:00 while UTC now 10:12 → forming 10:00 bar correctly excluded)

### Defaults chosen (confirm/retune in SQL before Phase 3)
All in `app_config` — `UPDATE app_config SET config_value=... WHERE config_key=...`:
- Indicators: `ta.ema_fast_period`=12, `ta.ema_slow_period`=26, `ta.rsi_period`=14,
  `ta.atr_period`=14, `ta.rsi_bull_min`=50, `ta.rsi_overbought`=70,
  `ta.vol_lookback`=20, `ta.vol_z_min`=0.5, `ta.atr_stop_mult`=1.5
- S/R: `sr.method`=rolling_extrema, `sr.lookback`=20
- Breakout: `breakout.require_volume`=true, `breakout.vol_z_min`=1.0
- Weights: `weights.trend`=40, `weights.momentum`=25, `weights.volume`=15, `weights.breakout`=20
- Cadence: `scan.timeframe`=1Hour, `scan.bars_to_fetch`=250
- Cooldown: `cooldown.bars_after_stop`=3

---

## Phase 3 — Execution + Initial TP/SL (paper only)

### Schema additions
- [ ] `positions` (id, symbol, side, qty, entry_price, entry_ts, status,
      tp_price, **sl_price** (price-based), **support_break_price** (NEW),
      confidence_at_entry, breakout_confirmed, risk_pct_used,
      alpaca_order_id, ...)
- [ ] `trade_history` (closed positions, realized P&L, **exit_reason**
      including 'price_sl' vs 'support_break' vs 'tp', fees)

### Modules to build
- [ ] `core/sizing.py` — implement the locked formula:
      `size = (equity × risk_pct(confidence)) / abs(entry − stop)`,
      clamped by portfolio ceiling.
      ALSO clamp to available cash: the formula can ask for more than you
      can afford (tight stop + high confidence + large equity). Cap the
      computed size at spendable cash, accounting for cash already tied up
      in open positions.
- [ ] `core/execution.py` — places buys via Alpaca, records ACTUAL fills
- [ ] `core/orchestrator.py` — the HOURLY cycle (runs every hour on the
      closed 1h bar):
      market data → levels → TA (with breakout boost) →
      for each symbol: if NOT already held AND signal AND confidence ≥ floor
      → size → execute → write position with BOTH the price-based SL and
      the support-break price.
      Also each cycle: check every OPEN position for sell/exit conditions.
      Skip symbols already held (no stacking). Honor cool-down after stop-out.
- [ ] Hard gate in code: `place_entry(ta_signal=True, ...)` — refuses to run
      without `ta_signal=True`

### Green-light test (Phase 3)
- [ ] Trigger an entry on paper end-to-end (force a signal in dev mode if needed)
- [ ] Position row appears with correct size, TP, price-SL, AND support-break price (NEW)
- [ ] Confidence-at-entry recorded; a breakout entry shows the higher confidence (NEW)
- [ ] Alpaca dashboard shows matching paper fill
- [ ] Partial fills handled correctly (size in DB = actual filled)

---

## Phase 4 — Dynamic TP/SL + Trailing + Support-break exit

### Design choices
- [ ] When can TP expand? (e.g. price moved X% in favor AND momentum still aligned)
- [ ] How does SL trail? (e.g. SL = max(old_SL, new_TP − R × ATR))
- [ ] SL must only ever move IN FAVOR of locked-in profit, never backward.

### Modules to build
- [ ] `core/position_manager.py` — periodic loop, for each open position:
  - [ ] Read current persisted `sr_levels` (do NOT recompute ad hoc) (NEW)
  - [ ] **Support-break exit:** if a confirmed closed bar is below the
        position's support level → close the position, exit_reason =
        'support_break' (NEW)
  - [ ] Price-based SL exit: if price hits sl_price → close, exit_reason = 'price_sl'
  - [ ] **Whichever of the two triggers first wins** (NEW)
  - [ ] TP expansion + SL trailing when price runs in favor
- [ ] Persist every TP/SL change to an audit table for debugging

### Green-light test (Phase 4)
- [ ] Simulated favorable move: TP expands, SL trails up, lock-in visible
- [ ] Simulated price-SL hit: closes at correct price, exit_reason = 'price_sl'
- [ ] **Simulated support break (confirmed close below support): closes,
      exit_reason = 'support_break', even if price-SL not yet hit** (NEW)
- [ ] **Confirm a single wick below support does NOT trigger — only a
      confirmed close** (NEW)
- [ ] trade_history populated correctly with the right exit reason

---

## Phase 5 — Watchlist Filters

- [ ] Implement liquidity filter (24h volume threshold)
- [ ] Implement spread filter (bid-ask spread % cap)
- [ ] Implement volatility filter (ATR band — too dead or too wild = skip)
- [ ] Admin script to review filter results before activating new symbols
- [ ] Wallet check: if a held symbol is removed from watchlist while
      losing → trigger SL instead of silent deactivation

---

## Phase 6 — AI Research Layer (default OFF)

### Hard rules (re-stated)
- [ ] AI can NEVER raise confidence past the TA gate
- [ ] AI can only veto / dampen
- [ ] `ai_enabled` config flag controls everything; default false

### Modules
- [ ] `core/ai_sentiment.py` — pull news for symbol, ask Claude for sentiment
- [ ] `core/ai_context.py` — price-context reasoning
- [ ] `core/ai_watchlist.py` — suggest add/remove (suggestions land in a
      review table, NOT auto-applied)
- [ ] Wire AI suggestion into entry path as a confidence MULTIPLIER ∈ [0, 1]

### Green-light test (Phase 6)
- [ ] Toggle `ai_enabled = true` in config
- [ ] AI runs, suggestions logged, no autonomous changes to watchlist
- [ ] Confirmed: no entry can occur without TA signal, regardless of AI score

---

## Phase 7 — Observability + Alerting

- [ ] Structured JSON logs (structlog), one file per day, rotated
- [ ] Heartbeat row written every N minutes to a `heartbeat` table
- [ ] External monitor (cron + curl, or healthchecks.io) alerts if heartbeat stalls
- [ ] Alert channel: email or Telegram bot (your choice)
- [ ] Critical alerts: bot crashed with open positions / Alpaca auth failed / DB unreachable

---

## Phase 8 — Paper soak test → Live readiness

- [ ] Run the full system on paper for at least **4 weeks** untouched
- [ ] Review every trade: did sizing match formula? Did SL trail correctly?
      Did breakout entries and support-break exits behave as designed? (NEW)
- [ ] Compute paper P&L, win rate, max drawdown, average R-multiple
- [ ] Stress test: kill the process mid-trade — does it recover cleanly?
- [ ] Stress test: revoke DB access briefly — does the bot retry then alert?
- [ ] Only if all of the above pass cleanly: discuss live-readiness criteria
      and the separate decision of whether to ever go live at all

---

## Reminders to self

- Runs HOURLY on the just-closed 1h bar. Never the forming bar.
- Each hourly cycle does BOTH: look for new buys AND check open positions
  for sell/exit conditions.
- One entry per symbol per bar; skip symbols already held; honor cool-down.
- Hourly whipsaws more than daily — cool-down and fee model matter more now.
- Paper first. Then paper longer. Then maybe live.
- The TA gate is structural, not a vibe. The code must refuse entries without it.
- Confidence scales SIZE only, not safety rails.
- Resistance breakout RAISES confidence (entry side). Support break is an
  EXTRA stop trigger (exit side). They are mirror images. (NEW)
- Two stop triggers: price-SL and support-break. Whichever fires first wins. (NEW)
- Closed bars only — no look-ahead. Applies to breakout AND support-break
  checks. A wick is not a break. (NEW)
- Persist S/R levels; the Position Manager reads them, never recomputes ad hoc. (NEW)
- Master key lives outside the DB. Back it up offline.
- All timestamps UTC. Convert only at display.
- Record actual fills, not requested quantities.
- Stops are triggers, not guarantees — crypto can gap through them.
