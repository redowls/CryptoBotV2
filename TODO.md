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

## Phase 3 — Execution + Initial TP/SL (paper only) — CODE COMPLETE, green-light PASSED 2026-05-30

> The 2026-05-30 TLS-interception block (Alpaca cert `Hostname mismatch`) was
> resolved; `check_connectivity` is green again. The green-light then passed on
> a forced paper entry (see below). NOTE: that entry left BTC/USD position #1
> OPEN on the paper account, unmanaged (no Position Manager until Phase 4) — it
> was a synthetic `--force` test, so consider flattening it before the soak.
> FOLLOW-UP for Phase 4: Alpaca takes crypto fees in the BASE asset, so the
> wallet qty (0.064453927) is < the recorded filled qty (0.064615466).
> Reconcile DB qty against the actual wallet / record fees for exit + P&L.

### Schema additions
- [x] `positions` — already FULLY declared in `01_schema_phase1.sql` (both
      `sl_price` and `support_break_price`, `confidence_at_entry`,
      `breakout_confirmed`, `risk_pct_used`, `alpaca_order_id`). Phase 3 writes
      to it; no ALTER needed.
- [x] `trade_history` — already declared in Phase 1 (incl. `exit_reason` CHECK
      ∈ {price_sl, support_break, tp, manual}). Populated on close (Phase 4).
- [x] `db/03_schema_phase3.sql` — seeds execution/TP knobs into `app_config`
      (`tp.r_multiple`=2.0, `execution.time_in_force`=gtc, poll attempts/delay,
      `execution.min_notional`=1.0). Applied to live `CryptoBotV2`.

### Modules to build
- [x] `core/sizing.py` — locked formula
      `qty = (equity × risk_pct(confidence)) / abs(entry − stop)`, clamped by
      the portfolio aggregate-risk ceiling AND spendable cash; reads the active
      `risk_profile` row. Records the EFFECTIVE post-clamp risk%. (Verified
      offline: interpolation, cash clamp, portfolio clamp, all veto paths.)
- [x] `core/execution.py` — `place_entry(client, *, ta_signal=True, ...)`
      submits a market BUY and polls for the ACTUAL fill (partials handled);
      `record_position` writes the open row with BOTH stop triggers + TP.
- [x] `core/orchestrator.py` — the HOURLY cycle: market data → levels → TA
      (with breakout boost) → for each symbol, if NOT held AND signal AND
      confidence ≥ floor AND not in cool-down → size → execute → write position
      with the price-based SL and the support-break price. Skips held symbols
      (no stacking); tracks caps/cash across the cycle. EXIT pass is a marked
      Phase 4 placeholder (Position Manager not built yet).
- [x] Hard gate in code: `place_entry(ta_signal=True, ...)` raises
      `PermissionError` unless `ta_signal is True` (invariant #1, structural).
- [x] `scripts/run_trade_cycle.py` — manual cycle runner (`--dry-run`,
      `--force SYMBOL` for the dev green-light entry).
- [x] `scripts/apply_sql.py` — GO-splitting loader (applies `db/*.sql`).

### Green-light test (Phase 3) — PASSED 2026-05-30
- [x] Triggered an entry on paper end-to-end
      (`python -m scripts.run_trade_cycle --force BTC/USD` — no natural signal, conf=25)
- [x] Position row #1 with correct size (qty 0.06461547, risk 0.40% at conf 60),
      TP 74,793.82 (2R), price-SL 72,936.68, AND support-break 72,413.79 (NEW)
- [x] Confidence-at-entry recorded (60.00); breakout_confirmed=False (forced entry
      was not a breakout — Phase 2 already verified a breakout adds +20 confidence)
- [x] Alpaca order read back: status FILLED, qty + avg (73,616.50) match the DB row
- [x] Partial-fill path in place (records actual filled_qty); this fill was full

---

## Phase 4 — Dynamic TP/SL + Trailing + Support-break exit — CODE COMPLETE, offline logic green-light PASSED 2026-05-30

> Offline decision logic verified by `scripts/verify_phase4.py` (16/16:
> first-to-fire, wick-vs-close, monotonic SL/TP/support trails). The manager
> also ran against LIVE data + DB in dry-run: pos#1 (BTC entry 73,616) is
> marginally UNDERWATER at the current close (~73,556), so the manager
> correctly makes NO change — `trail_only_in_profit` freezes the SL and no
> trigger fires. That exercises the in-profit guard on live data, but the ONE
> remaining green-light item is a real exit: a paper liquidation that writes a
> `trade_history` row. It needs a bar that genuinely trips a trigger or a
> manual flatten of pos#1 (see the question at the end of this phase).

### Design choices — DECIDED (knobs seeded in `app_config` as `pm.*`)
- [x] **When can TP expand?** While trend AND momentum still hold
      (`pm.tp_expand_requires_alignment`=true, reusing the TA engine's
      trend_aligned + momentum_ok components). new_tp = max(old_tp,
      close + `pm.tp_r_multiple`(=2.0) × (close − new_sl)). Never shrinks.
- [x] **How does SL trail?** new_sl = max(old_sl, close − `pm.trail_atr_mult`
      (=1.5) × ATR), only while in profit (`pm.trail_only_in_profit`=true).
- [x] SL only ever moves IN FAVOR (monotonic up); enforced by the `max(...)`.
- [x] **Support trailing:** `pm.support_trail_enabled`=true — the support
      trigger trails up toward price using the latest PERSISTED level (never
      set at/above close, which would self-trigger).
- [x] **Exit semantics:** price_sl fires on an intrabar LOW pierce (hard stop);
      support_break fires only on a CONFIRMED CLOSE below support (wick ≠ break);
      tp fires on an intrabar HIGH reach. Forward trails/expansions apply from
      the NEXT bar — the exit check uses levels recorded on a PRIOR cycle, so
      step 1 is free of look-ahead.

### Schema additions
- [x] `db/04_schema_phase4.sql` — `position_adjustments` audit table (one row
      per sl_trail / tp_expand / support_trail) + seeds the `pm.*` knobs into
      `app_config` (2-col key/value MERGE, same style as Phases 1–3). Applied
      to live `CryptoBotV2`. `positions`/`trade_history` were already fully
      declared (Phase 1); the manager only WRITES them.

### Modules to build
- [x] `core/position_manager.py` — `manage_open_positions(symbols, dry_run, now)`
      loops every open position on its latest closed bar:
  - [x] Reads the latest persisted `sr_levels` via `levels.latest_levels`
        (do NOT recompute ad hoc — invariant #6)
  - [x] **Support-break exit:** confirmed close below support → close,
        exit_reason = 'support_break'
  - [x] Price-based SL exit: bar low pierces sl_price → close, 'price_sl'
  - [x] **Whichever of the two triggers first wins** (`resolve_exit`: higher
        trigger reached first as price falls; protective stops beat tp)
  - [x] TP expansion + SL trailing + support trailing when price runs in favor
  - [x] Pure decision helpers (`resolve_exit`/`trail_sl`/`trail_support`/
        `expand_tp`) split out for deterministic offline testing
- [x] `core/execution.py` — `close_position` (liquidate whole wallet position;
      absorbs the base-asset fee discrepancy) + `record_exit` (writes
      `trade_history` with realized P&L + exit_reason, flips position to closed)
- [x] Wired the EXIT pass into `core/orchestrator.run_cycle` (runs BEFORE the
      entry pass so freed capital is visible to entries)
- [x] Persist every TP/SL change to `position_adjustments` (audit)
- [x] `scripts/run_position_manager.py` (`--dry-run`) + `scripts/verify_phase4.py`

### Green-light test (Phase 4)
- [x] Simulated favorable move: TP expands, SL trails up, lock-in visible
      (offline `verify_phase4`)
- [x] Simulated price-SL hit: 'price_sl' verdict at the correct price (offline)
- [x] **Simulated support break (confirmed close below support): 'support_break'
      even if price-SL not yet hit** (offline)
- [x] **Confirm a single wick below support does NOT trigger — only a
      confirmed close** (offline)
- [x] Live data + DB dry-run runs clean; in-profit guard correctly holds an
      underwater position unchanged
- [ ] **LIVE:** an actual paper liquidation writes `trade_history` with the
      right exit_reason (needs a real trigger or a manual flatten of pos#1).
      Attempted 2026-05-30 via `scripts/flatten_position 1` — BLOCKED by the
      recurring dev-box TLS interception (`Hostname mismatch` on
      paper-api.alpaca.markets, see CLAUDE.md "Live setup facts"). Code path
      verified up to the broker call; the failed order wrote NOTHING (pos#1
      still open, trade_history empty — broker I/O is outside the DB tx).
      Retry from the VPS, or once the interception clears.

---

## Phase 5 — Watchlist Filters — CODE COMPLETE, offline logic green-light PASSED 2026-05-30

> The tradeable-universe gate. Three independent entry filters
> (`core/watchlist_filters.py`), each toggleable + tunable in SQL via the
> `filters.*` keys, run in the orchestrator's ENTRY pass AFTER the cooldown
> check and BEFORE sizing. They gate ENTRIES ONLY — open positions are always
> managed for exits regardless of filters (a symbol going illiquid must never
> strand a position). `--force` (DEV) bypasses the filters. Defaults are the
> CONSERVATIVE profile the user chose.

### Design choices — DECIDED (knobs seeded in `app_config` as `filters.*`)
- [x] **Liquidity:** 24h quote volume = Σ(close·volume) over the last
      `filters.vol_24h_bars`(=24) closed 1h bars ≥ `filters.min_24h_quote_volume`
      (=$10,000,000). Alpaca reports BASE volume; ×close → quote (USD).
- [x] **Spread:** live (ask−bid)/mid as a % ≤ `filters.max_spread_pct`(=0.20%).
      Uses a LIVE quote (`market_data.latest_quote`), fetched only when the
      spread filter is enabled; a quote-fetch failure is a conservative FAIL.
- [x] **Volatility:** ATR%(=ATR/close·100) must sit INSIDE
      [`filters.min_atr_pct`(=0.5), `filters.max_atr_pct`(=8.0)] — too dead
      (fees dominate) OR too wild (stops get run) both skip. Reuses `ta.atr_period`.
- [x] **Wallet-vs-watchlist drift:** if a held symbol is deactivated/removed
      from the watchlist AND is underwater, FORCE-EXIT it (exit_reason='manual')
      rather than leaving it unmanaged; an in-profit deactivated position is left
      to the Position Manager's normal exit. Toggle `filters.drift_force_exit_enabled`.

### Schema additions
- [x] `db/05_schema_phase5.sql` — seeds the `filters.*` knobs into `app_config`
      (2-col key/value MERGE, same style as Phases 1–4). NO new tables: filters
      read cached bars + a live quote; drift reuses positions/trade_history.
      Applied to live `CryptoBotV2`.

### Modules to build
- [x] `core/market_data.py` — added `Quote` + `latest_quote(client, symbol)`
      (live top-of-book bid/ask, with the same transient-error retry as bars).
- [x] `core/watchlist_filters.py` — `FilterParams.from_config()`; pure helpers
      `quote_volume_24h` / `spread_pct` / `atr_pct` and `check_liquidity` /
      `check_spread` / `check_volatility`; `evaluate_filters(...) -> FilterResult`
      (a disabled filter can never fail — pinned by the offline test);
      `screen_symbol(data_client, symbol, bars, params)` (fetches the quote only
      when spread is enabled); `enforce_watchlist_drift(*, dry_run, now)`.
- [x] Wired into `core/orchestrator.run_cycle`: filter gate in the entry loop
      (after cooldown, before sizing; `--force` bypasses) + a DRIFT pass in the
      EXIT phase (after the Position Manager, so freed capital is visible to entries).
- [x] `scripts/run_watchlist_filters.py` — admin review tool (read-only; default
      ALL watchlist rows incl. inactive, `--active`, or explicit symbols).
- [x] `scripts/verify_phase5.py` — OFFLINE logic green-light (no DB/broker).

### Green-light test (Phase 5)
- [x] Offline logic green-light: `scripts/verify_phase5` 24/24 PASS — value
      math, every filter boundary (incl. too-dead vs too-wild), combined verdict,
      and "a disabled filter cannot fail".
- [x] Live data + DB: schema applied, `filters.*` seeded; admin tool ran on live
      BTC/USD (24h quote vol ~$23.3B, spread ~0.013%, ATR ~0.30%); dry-run cycle
      completed clean with the filter gate + drift pass wired in.
- [ ] **LIVE (carry-over):** exercise an actual filter REJECTION blocking an
      entry in a real cycle (needs an UNHELD watchlist symbol with a BUY signal
      that fails a filter — BTC is held + has no signal, so the cycle never
      reaches its filter), and an actual drift FORCE-EXIT (deactivate a held,
      underwater symbol). Same shape as the Phase 4 live-liquidation carry-over.

> NOTE for the user: under the CONSERVATIVE defaults, BTC currently FAILS the
> volatility filter — its hourly ATR is ~0.30%, below the 0.50% floor (a "too
> dead" market right now). If BTC weren't held + had a signal, it would be
> skipped. Lower `filters.min_atr_pct` (e.g. 0.25) if you want to trade BTC's
> current low-volatility regime, or leave it (conservative = sit out dead tape).

---

## Phase 6 — AI Research Layer (default OFF) — CODE COMPLETE, offline logic green-light PASSED 2026-05-30

> Advisory-only AI confidence multiplier ∈ [0,1] — it can VETO/DAMPEN but never
> raise confidence or create an entry (invariant #2). The user chose to BUILD
> OFFLINE-ONLY this session: the Anthropic key is NOT wired, so the live cycle
> runs NEUTRAL (x1.00 = pure TA) until a key is provided. News source = the
> Alpaca news API (no new creds/dep). Default model = `claude-opus-4-8`
> (user's choice; tune `ai.model` in SQL to downgrade for cost).
> The `anthropic` SDK is imported LAZILY everywhere, so the bot + the offline
> green-light run without it installed (confirmed: anthropic not installed,
> 21/21 PASS).

### Design choices — DECIDED (knobs seeded in `app_config` as `ai.*`)
- [x] **Master switch** is the existing `ai_enabled` (Phase 1 seed, false).
      When false (or no key) the advisor returns a NEUTRAL 1.0 → the entry path
      is byte-for-byte the pre-Phase-6 behaviour.
- [x] **Multiplier semantics:** sentiment × context, each ∈ [0,1], combined by
      PRODUCT (each can only subtract more), bounded below by `ai.min_multiplier`
      (=0.0 → full veto allowed). `adjusted = ta_conf × multiplier` ≤ ta_conf
      ALWAYS; the TA `signal` bool is passed through UNCHANGED.
- [x] **Fail-open:** any error (no key, news fetch fail, API error, bad JSON)
      → NEUTRAL 1.0. AI failure must never alter the TA pipeline.
- [x] **Cost placement:** the advisor runs in the entry loop only AFTER the TA
      signal is True AND the Phase 5 filters pass (no API spend on non-setups);
      `--force` (DEV) bypasses it. Watchlist suggestions are ADMIN-run only
      (`scripts/run_ai_review --watchlist`), not part of the hourly cycle.

### Hard rules (re-stated)
- [x] AI can NEVER raise confidence past the TA gate (multiplier clamped ≤ 1)
- [x] AI can only veto / dampen (multiplier ∈ [0,1], applied to confidence only)
- [x] `ai_enabled` config flag controls everything; default false

### Schema additions
- [x] `db/06_schema_phase6.sql` — seeds the `ai.*` knobs + creates
      `ai_suggestions` (advisory audit log of every multiplier) and
      `ai_watchlist_suggestions` (add/remove proposals, status pending, NEVER
      auto-applied). Re-runnable (guarded CREATEs + MERGE seed).
      **NOT YET APPLIED to the live DB** — apply with
      `scripts.apply_sql db/06_schema_phase6.sql` (carry-over below).

### Modules to build
- [x] `core/ai_client.py` — `AIParams.from_config()`; the PURE multiplier core
      (`clamp01`, `combine_multipliers`, `apply_ai_multiplier`, `parse_multiplier`)
      that the offline test pins; lazy Anthropic client + `anthropic_key()`
      resolver (env `ANTHROPIC_API_KEY` → encrypted `api_credentials`).
- [x] `core/ai_sentiment.py` — pull news for the symbol (Alpaca news API), ask
      Claude for a long-entry sentiment multiplier; fail-open to neutral.
- [x] `core/ai_context.py` — price-context reasoning over the TA snapshot →
      trap-catching multiplier; no extra market-data calls.
- [x] `core/ai_watchlist.py` — suggest add/remove → review table only, NEVER
      touches `dbo.watchlist`.
- [x] `core/ai_advisor.py` — combines sentiment+context → one multiplier, logs
      to `ai_suggestions`; the single entry point the orchestrator calls.
- [x] Wired into `core/orchestrator.run_cycle` entry path as a confidence
      MULTIPLIER ∈ [0,1] (after filters, before sizing; AIVETO line when the
      dampened confidence drops below the trade floor).
- [x] `scripts/verify_phase6.py` (OFFLINE logic gate) +
      `scripts/run_ai_review.py` (admin preview; dry-run by default, `--write`,
      `--watchlist`).

### Green-light test (Phase 6)
- [x] OFFLINE logic green-light: `scripts/verify_phase6` 21/21 PASS — clamp,
      product-combine + floor, parser fail-open, and invariant #2 structurally
      (adjusted ≤ TA conf for ALL multipliers incl. >1; signal=False never
      flipped True; multiplier>1 cannot raise).
- [ ] **LIVE (carry-over):** apply `db/06_schema_phase6.sql`; wire an Anthropic
      key (env or encrypted `api_credentials` provider='anthropic'); toggle
      `ai_enabled = true`; run `scripts.run_ai_review` and a `--dry-run` cycle
      and confirm: AI runs, advisory rows land in `ai_suggestions`, an AI
      dampening/veto is visibly applied, watchlist suggestions land in the
      review table with NO autonomous changes to `dbo.watchlist`, and no entry
      can occur without a TA signal regardless of AI score.

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
