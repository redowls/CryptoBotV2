# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current repository state

**Phases 1, 2, 3, 4, and 5 are built and green-lit (Phases 1–2 on 2026-05-29; Phases 3–5 on 2026-05-30).** Phase 4's offline logic green-light passed (16/16) and it ran clean against live data + DB in dry-run. Phase 5's offline logic green-light passed (24/24) and the watchlist filters ran against live data + DB (admin tool + dry-run cycle). Two carry-overs need a LIVE event to close: a Phase 4 paper liquidation that writes `trade_history`, and a Phase 5 filter REJECTION / drift force-exit in a real cycle (see TODO.md). The repo holds working code plus the design docs:

- `SUMMARY.md` — the authoritative design doc. Full architecture, locked-in decisions, schema, security model, phase plan. **Read this first.**
- `TODO.md` — the phased, top-down build checklist with per-phase green-light tests.
- `core/` — `crypto.py` (Fernet), `db.py` (SQLAlchemy + pyodbc, retry, `get_active_credentials`), `config.py` (typed `app_config` accessor), `indicators.py` (pure EMA/RSI/ATR/vol-z), `levels.py` (rolling-extrema S/R + persist/read), `market_data.py` (closed-bars-only OHLCV fetch w/ Alpaca retry, `market_bars` upsert, `timeframe_duration`, plus `latest_quote` live bid/ask), `ta_engine.py` (the gatekeeper: `evaluate()` → signal + confidence + breakout boost), `sizing.py` (locked size formula + clamps), `execution.py` (the `ta_signal` gate + paper entry/exit + fill recording), `position_manager.py` (Phase 4: dynamic TP/SL, trailing, first-to-fire exits), `watchlist_filters.py` (Phase 5: liquidity/spread/volatility entry filters + watchlist-drift force-exit), `orchestrator.py` (the hourly cycle: exit pass + drift pass + filtered entry pass).
- `db/01_schema_phase1.sql` … `db/05_schema_phase5.sql` — **all five already applied** to the live `CryptoBotV2` DB. (Phase 3 is a config seed only; Phase 4 adds `position_adjustments` + `pm.*` seeds; Phase 5 is a `filters.*` config seed only.)
- `scripts/` — `insert_credentials.py` (you run it; stores encrypted keys), `check_connectivity.py` (Phase 1 green-light), `run_signal_scan.py` (Phase 2 manual scan; persists signals + S/R, places no orders), `run_trade_cycle.py` (the hourly cycle; `--dry-run`, `--force`), `run_position_manager.py` (Phase 4 exit pass alone; `--dry-run`), `flatten_position.py` (manually liquidate one open position → `trade_history` with `exit_reason='manual'`; `--dry-run`), `run_watchlist_filters.py` (Phase 5 admin review of the entry filters; read-only; `--active` / explicit symbols), `verify_phase4.py` (offline Position Manager logic gate), `verify_phase5.py` (offline watchlist-filter logic gate), `apply_sql.py` (GO-splitting `.sql` loader).
- `requirements.txt`, `.env.example`, `.gitignore`, `README.md`, and a local `.venv/` (Windows dev, deps installed).

Strategy parameters (indicator periods, confidence weights, S/R lookback, breakout/volume rules, cooldown, TP multiple, order mechanics, Position-Manager trailing, watchlist filters) live in `app_config` under the `ta.*`, `sr.*`, `breakout.*`, `weights.*`, `scan.*`, `cooldown.*`, `tp.*`, `execution.*`, `pm.*`, `filters.*` keys — **tunable in SQL without code changes** (read via `core/config.py`). The risk *envelope* (max/min risk %, confidence floor, portfolio ceiling, max open positions) lives in the `risk_profile` table, not `app_config`. See the "Defaults chosen" block in `TODO.md` Phase 2.

**Phases 6–8 are not built yet.** The modules they call for (`ai_*.py`) and the `heartbeat` table **do not exist** — search/verify before referencing them. `trade_history` is written by the Phase 4 Position Manager on position close, so it stays EMPTY until a position actually closes (pos#1 is still open on paper as of the Phase 4 green-light — and roughly flat, so nothing has tripped a trigger yet).

## What this project is

A production-grade automated **crypto trading bot** in Python, deployed on a Linux VPS, using **Alpaca** crypto endpoints (paper trading first) and **SQL Server** (via pyodbc + ODBC Driver 18) as the single source of truth for config, encrypted keys, watchlist, positions, and trade history.

It runs **hourly**, evaluating the just-closed **1-hour bar** for every watchlist symbol — both for new BUY entries and for SELL/exit checks on open positions.

## Non-negotiable invariants (enforce these structurally, not by convention)

These are the rules most likely to be silently violated by a well-meaning change. They are the point of the whole design:

1. **TA is the gatekeeper.** No technical-analysis signal → no trade, ever. The entry function must take a required `ta_signal: bool` gate (e.g. `place_entry(ta_signal=True, ...)`) and refuse to run without it. This is a structural barrier, not a runtime check that can be skipped.
2. **AI can only subtract.** The Phase 6 AI layer is advisory-only and wired as a confidence *multiplier* ∈ [0, 1]. It can veto/dampen but can **never** create an entry or raise confidence past the TA gate. Default `ai_enabled = false`.
3. **Confidence scales SIZE only — never safety rails.** Higher confidence → larger position (still capped at the per-trade risk %). It must **not** loosen stops, widen exits, or change the loss cap.
4. **Closed bars only — no look-ahead bias.** Always compute on the confirmed, closed 1-hour bar; never the forming bar. This applies to indicators AND to the resistance-breakout and support-break checks. **A wick through a level is not a break** — require a confirmed close.
5. **Two independent stop triggers, first-to-fire wins.** Every position carries both a price-based `sl_price` and a `support_break_price`. The support level may sit above or below the price SL; whichever triggers first closes the position. Both must be recorded on the position so the Position Manager never guesses.
6. **Persist S/R levels; never recompute ad hoc.** The Position Manager may run at a different cadence than the signal scan. It must read persisted `sr_levels`, not recalculate them inconsistently.
7. **Record actual fills, not requested quantities.** Partial fills are normal; the DB must reflect what filled.
8. **Master key lives outside the DB.** `TRADEBOT_MASTER_KEY` is loaded from a `chmod 600` `.env` owned by a dedicated non-root user. The DB stores only Fernet ciphertext — plaintext keys are never written, logged, or echoed. Losing the master key makes every stored credential undecryptable.
9. **All timestamps UTC.** Convert only at display.
10. **Paper trading only** until the full pipeline runs end-to-end on paper. Live-execution code is not written until then.

## Position sizing (locked formula)

```
risk_pct(confidence) = lerp(min_risk_per_trade_pct, max_risk_per_trade_pct,
                            normalize(confidence, 60, 100))
position_size_quote = (equity × risk_pct) / abs(entry_price − stop_price)
```

- Below `min_confidence_to_trade` (60) → no trade. Tier mapping: 60–75 = min size, 75–90 = mid, 90+ = max (always still capped by the per-trade risk %).
- Final size is clamped by `max_portfolio_risk_pct` (6% aggregate open risk, max 5 open positions).
- **Also clamp to spendable cash** — a tight stop + high confidence + large equity can request more than the account holds. Account for cash already tied up in open positions.

Risk envelope lives in the `risk_profile` table (medium profile seeded active): defaults 1% max risk per trade at top confidence scaling down to 0.4% at confidence 60. Tunable in SQL without code changes.

## Intended architecture (build target)

Modular, independently testable components, orchestrated by an hourly daemon:

- **Config & State (SQL Server)** — single source of truth.
- **Market Data** (`core/market_data.py`) — OHLCV + quotes from Alpaca; cache bars in `market_bars` rather than re-fetching (hourly = 24× the API calls of daily).
- **Indicators** (`core/indicators.py`) — pure functions: EMA, RSI, ATR, volume z-score.
- **Levels** (`core/levels.py`) — detect support/resistance per symbol; persist to `sr_levels`.
- **TA Engine** (`core/ta_engine.py`) — the gatekeeper; emits `(signal, confidence 0–100, entry_hint, stop_hint, support_level, resistance_level)`. Resistance breakout (volume-confirmed close above resistance) adds confidence and sets `breakout_confirmed`.
- **Sizing** (`core/sizing.py`) — the locked formula above.
- **Execution** (`core/execution.py`) — places paper orders, records actual fills, writes initial TP/SL + support-break price.
- **Orchestrator** (`core/orchestrator.py`) — the hourly cycle: EXIT pass (Position Manager) first, then ENTRY pass: data → levels → TA → for each symbol enter if not already held + signal + confidence ≥ floor. One entry per symbol per bar; skip held symbols (no stacking); honor cool-down after a stop-out.
- **Position Manager** (`core/position_manager.py`, Phase 4) — dynamic TP expansion + SL trailing (SL only ever moves in favor of locked-in profit) + support-break/price-SL exits, first-to-fire wins. Pure decision helpers (`resolve_exit`/`trail_sl`/`trail_support`/`expand_tp`) are split out and offline-tested.
- **AI Research** (Phase 6, `core/ai_*.py`) — advisory only; suggestions land in a review table, not auto-applied.
- **Observability** (Phase 7) — structured JSON logs (structlog, daily-rotated), a `heartbeat` table written every N minutes, and an external monitor that alerts when the heartbeat stalls. A dead bot must not silently leave positions unmanaged. Critical alerts: crash with open positions, Alpaca auth failure, DB unreachable.
- **Crypto/DB infra** (`core/crypto.py` Fernet, `core/db.py` SQLAlchemy + pyodbc with retry).

### Schema by phase

The DB grows phase by phase; don't expect later tables to exist early. Phase 1 (`db/01_schema_phase1.sql`, applied): `api_credentials`, `app_config` (a 2-column `config_key`/`config_value` key/value table — **no `description` column**; seed it with a 2-col MERGE), `risk_profile`, `watchlist`. The Phase 1 DDL **already fully declares** `positions` (incl. both `sl_price` *and* `support_break_price`, `confidence_at_entry`, `breakout_confirmed`, `risk_pct_used`, `alpaca_order_id`; the entry timestamp is `entry_ts_utc` and there is **no** `closed_at_utc` on `positions` — closing just flips `status` to `'closed'`) and `trade_history` (cols `side`, `realized_pnl`, `fees`, `opened_at_utc`, `closed_at_utc`, and `exit_reason` CHECK ∈ {`price_sl`, `support_break`, `tp`, `manual`}). Phase 2 (`db/02_schema_phase2.sql`, applied) adds `market_bars`, `signals`, `sr_levels` + seeds strategy params. Phase 3 (`db/03_schema_phase3.sql`, applied) seeds execution/TP knobs only. Phase 4 (`db/04_schema_phase4.sql`, applied) adds `position_adjustments` (audit of every sl_trail/tp_expand/support_trail) + seeds the `pm.*` knobs. Phase 5 (`db/05_schema_phase5.sql`, applied) seeds the `filters.*` knobs only (no new tables). Phase 7 adds `heartbeat`.

### Data-flow rule

The trade path is: scheduler → market data → watchlist filter → TA engine (with breakout boost) → `if signal AND (AI disabled OR AI did not veto)` → sizer → execution → write TP/SL + support-break → Position Manager manages exit. The TA gate sits before sizing/execution by construction.

### Module APIs (built: Phases 1–5)

Concrete callable surface so later phases don't have to re-read the files. All money/price values are plain `float`; the DB stores `DECIMAL(38,18)`.

- **`core/crypto.py`** — `encrypt(str) -> bytes`, `decrypt(bytes|str) -> str` (raises `RuntimeError` on master-key mismatch). Ciphertext stored as VARBINARY Fernet tokens.
- **`core/db.py`** — `get_engine()` (process singleton, `pool_pre_ping`); `connect_with_retry(max_attempts=3, base_delay=1.0)` (linear backoff; retries only `OperationalError`/`InterfaceError` — auth/programming errors fail fast); `healthcheck()`; `get_active_credentials(provider, environment) -> (key, secret)`.
- **`core/config.py`** — `get_str/get_int/get_float/get_bool(key, default)` over `app_config`; values cached per process, `refresh()` drops the cache. This is how every strategy knob is read — **never hard-code a tunable.**
- **`core/indicators.py`** — pure: `ema(values, period)`, `rsi(values, period=14)` (Wilder), `atr(highs, lows, closes, period=14)` (Wilder), `volume_zscore(volumes, lookback=20)`. Each returns the **latest** value as `float`, or `None` if insufficient data. Inputs ordered oldest→newest.
- **`core/levels.py`** — `detect_levels(highs, lows, *, lookback, method="rolling_extrema") -> SRLevels(support, resistance, method, lookback)` (level taken from the bars *before* the latest closed bar — never includes the bar being tested); `persist_levels(conn, symbol, levels, bar_ts_utc)`; `latest_levels(conn, symbol) -> SRLevels | None` (**the Position Manager's reader — use this, don't recompute**, per invariant #6).
- **`core/market_data.py`** — `Bar(symbol, ts, open, high, low, close, volume, trade_count, vwap)` (ts = bar START, naive UTC); `make_client(key, secret)`; `fetch_closed_bars(client, symbol, *, timeframe="1Hour", limit=250, now=None) -> list[Bar]` (**enforces closed-bars-only** by dropping any bar where `ts + duration > now`; retries transient Alpaca connection/timeout errors); `upsert_bars(conn, bars, *, timeframe="1Hour") -> int` (MERGE into `market_bars`); `Quote(symbol, bid, ask, ts)` + `latest_quote(client, symbol) -> Quote | None` (**Phase 5:** LIVE top-of-book bid/ask for the spread filter — intentionally not a closed bar; same transient-error retry as bars).
- **`core/ta_engine.py`** — `TAParams.from_config()` (reads all `ta.*`/`sr.*`/`breakout.*`/`weights.*` keys), `TAParams.min_bars()`; `evaluate(symbol, bars, params) -> TASignal`. **`evaluate` returns a `TASignal` dataclass** (not a bare tuple): `.signal` (bool — the BUY gate, `= trend AND momentum`), `.confidence` (float 0–100), `.breakout_confirmed`, `.entry_hint`, `.stop_hint` (price-based SL candidate = `close - atr_stop_mult*ATR`), `.support_level`, `.resistance_level`, `.bar_ts`, `.snapshot` (dict of raw indicator values + component breakdown). Breakout/volume only *add* confidence — they can never manufacture a signal without trend+momentum.
- **`core/sizing.py`** — `load_active_risk_profile(conn) -> RiskProfile` (the single active `risk_profile` row); `risk_pct_for_confidence(confidence, profile)` (lerp floor→max); `compute_size(*, confidence, equity, cash_available, entry_price, stop_price, profile, open_position_count, current_open_risk, min_notional=0.0) -> SizeResult`. `SizeResult` has `.accepted` (bool — `False` ⇒ do not trade, with `.reason`), `.qty` (base units, rounded DOWN to 9 dp), `.risk_pct_used` (EFFECTIVE post-clamp risk %, recorded on the position), `.notional`. Clamps in order: confidence→risk$, portfolio aggregate-risk budget, spendable cash; any clamp can only *shrink* or veto, never enlarge (invariant #3).
- **`core/execution.py`** — `make_trading_client(key, secret, *, paper=True)`; **`place_entry(client, *, ta_signal, symbol, qty, time_in_force="gtc", poll_attempts=5, poll_delay=1.0) -> Fill`** — raises `PermissionError` unless `ta_signal is True` (invariant #1, structural), submits a market BUY, polls until the order reaches a terminal state, and returns a `Fill(order_id, symbol, requested_qty, filled_qty, avg_price, status)` with `.is_filled`. Broker I/O is kept OUT of the DB transaction. `record_position(conn, *, symbol, fill, sl_price, support_break_price, tp_price, confidence, breakout_confirmed, risk_pct_used) -> int` writes the open `positions` row from the ACTUAL fill (invariant #7) with both stop triggers (invariant #5); returns the new id. **Phase 4:** `close_position(client, symbol, *, poll_attempts=5, poll_delay=1.0) -> Fill` liquidates the WHOLE wallet position (absorbs the base-asset crypto-fee discrepancy) and returns the actual exit fill; `record_exit(conn, *, position_id, symbol, qty, entry_price, exit_price, opened_at_utc, exit_reason, fees=None)` writes the `trade_history` row (realized P&L from the actual fill) and flips the position to `closed`.
- **`core/position_manager.py`** (Phase 4) — `manage_open_positions(*, symbols=None, dry_run=False, now=None) -> list[ExitResult]`: the EXIT pass. For each open position on its latest closed bar: `resolve_exit` (first-to-fire of price_sl/support_break/tp; price_sl = intrabar LOW pierce, support_break = confirmed CLOSE below support — a wick is not a break, tp = intrabar HIGH reach) → if exit, `close_position` + `record_exit`; else trail/expand for the NEXT bar via `trail_sl` (close − `pm.trail_atr_mult`·ATR, in-profit only, monotonic up), `trail_support` (toward price, never ≥ close), `expand_tp` (close + `pm.tp_r_multiple`·(close − new_sl), only while trend+momentum aligned, never shrinks), persisting each change to `position_adjustments`. Reads persisted S/R via `levels.latest_levels` (invariant #6). Broker I/O stays out of the DB tx; DB writes use `conn.begin()`. The four pure helpers are importable and side-effect-free for testing. `PMParams.from_config()` reads the `pm.*` keys. Confidence never enters this module (invariant #3).
- **`core/watchlist_filters.py`** (Phase 5) — the entry-eligibility gate (entries only — exits are NEVER filtered). `FilterParams.from_config()` reads the `filters.*` keys. Pure, offline-testable helpers: `quote_volume_24h(closes, volumes, n_bars)` (Σ close·volume → quote-currency 24h volume), `spread_pct(bid, ask)` (mid-based %), `atr_pct(atr_value, close)`; and `check_liquidity` / `check_spread` / `check_volatility` → `FilterCheck`. `evaluate_filters(symbol, *, closes, volumes, atr_value, bid, ask, params) -> FilterResult` (`.passed`, `.checks`, `.summary()`) — a DISABLED filter contributes no check and so can never fail. `screen_symbol(data_client, symbol, bars, params) -> FilterResult` is the orchestrator entry point (computes ATR from bars; fetches a live quote ONLY when the spread filter is enabled; a quote-fetch failure is a conservative spread FAIL). `enforce_watchlist_drift(*, dry_run=False, now=None) -> list[DriftExit]`: the wallet-vs-watchlist drift safety — for each open position whose symbol is NOT in the active watchlist, force-exit via `close_position` + `record_exit(exit_reason='manual')` **only if underwater** (latest close < entry); an in-profit deactivated position is left to the Position Manager. Broker I/O stays out of the DB tx. Toggle `filters.drift_force_exit_enabled`.
- **`core/orchestrator.py`** — `run_cycle(*, symbols=None, dry_run=False, force=False) -> int`: one hourly cycle. **EXIT pass first** (`manage_open_positions`, wrapped so one symbol's failure doesn't abort the cycle), then the **DRIFT pass** (`enforce_watchlist_drift`, also in the exit phase so freed capital is visible to entries), THEN the ENTRY pass. Per symbol: fetch closed bars → upsert → detect+persist S/R → `evaluate` → persist signal; then enter (skip if held / no signal / below floor / in cool-down → **Phase 5 filter gate (`screen_symbol`); `--force` bypasses it** → `compute_size` → `place_entry` → `record_position`). Tracks open-position count, aggregate open risk, and cash *across* the cycle; the entry-side state is read AFTER the exit + drift passes, so it reflects positions closed this bar. `dry_run` sizes + reports but places no orders; `force` (DEV) synthesizes a BUY setup (keeps real hints) to exercise an entry. TP = `entry_hint + tp.r_multiple * (entry_hint − stop_hint)`.

The orchestrator's `cooldown.bars_after_stop` check reads `trade_history`, so it activates automatically now that the Position Manager records stop-outs there.

## Phase discipline

Work strictly top-down through `TODO.md`. **No phase begins until the previous phase passes its green-light test.** Each phase has an explicit test checklist in `TODO.md`. Phase 1's green-light test (`scripts/check_connectivity.py`) shows four PASS lines: DB reachable, Alpaca creds decrypt, paper account returns equity, live quote returns bid/ask. **Phase 1 passed all four on 2026-05-29. Phase 2 (Market Data + TA Engine) also passed on 2026-05-29. Phase 3 (Execution + Initial TP/SL) passed on 2026-05-30 via a forced paper entry (BTC/USD position #1: qty 0.06461547, risk 0.40% at the conf-60 floor, 2R TP, price-SL + support-break; order read back FILLED). Phase 4 (Position Manager) passed its OFFLINE logic green-light on 2026-05-30 (`scripts/verify_phase4.py`, 16/16: first-to-fire, wick-vs-close, monotonic SL/TP/support trails) and ran clean against LIVE data + DB in dry-run — pos#1 is roughly flat (entry 73,616 vs current close ~73,556, i.e. marginally underwater), so the manager correctly made NO change, exercising the `trail_only_in_profit` guard. Phase 5 (Watchlist Filters) passed its OFFLINE logic green-light on 2026-05-30 (`scripts/verify_phase5.py`, 24/24: value math, every filter boundary incl. too-dead-vs-too-wild, combined verdict, disabled-filter-cannot-fail) and ran against LIVE data + DB — the admin tool screened live BTC/USD (24h quote vol ~$23.3B PASS, spread ~0.013% PASS, ATR ~0.30% FAIL the 0.50% floor — currently a "too dead" market) and a dry-run cycle completed clean with the filter gate + drift pass wired in. Phase 6 (AI Research Layer, default OFF) is the active phase.** Carry-overs: (a) the remaining Phase 4 green-light item is a LIVE paper liquidation that writes `trade_history` — it needs a bar that genuinely trips a trigger or a manual flatten of pos#1 (`scripts/flatten_position 1`, which routes through the same `close_position` + `record_exit` path with `exit_reason='manual'`); (b) `close_position` liquidates the whole wallet position, which structurally absorbs the base-asset crypto-fee discrepancy (the Phase 3 carry-over) since exits record the ACTUAL fill; (c) the remaining Phase 5 item is a LIVE filter REJECTION blocking an entry in a real cycle (needs an UNHELD watchlist symbol with a BUY signal that fails a filter — BTC is held + has no signal, so the cycle never reaches its filter) and a LIVE drift FORCE-EXIT (deactivate a held, underwater symbol). Note: under the conservative `filters.*` defaults, BTC currently fails the volatility filter (hourly ATR ~0.30% < the 0.50% `filters.min_atr_pct` floor); lower that knob to trade BTC's current low-vol regime.

## Live setup facts (current)

These describe the running environment, not the design ideal — they will change as the project hardens:

- **Database:** `CryptoBotV2` (note: **not** `tradebot`) on a remote SQL Server 2022 at `185.202.236.11`. Set `DB_NAME=CryptoBotV2` in `.env`.
- **DB auth:** currently `sa` over `Encrypt=yes` + `TrustServerCertificate=yes` (remote self-signed cert). This is for bring-up only — replace `sa` with a least-privilege login scoped to `CryptoBotV2` before the paper soak.
- **Master key:** a real `TRADEBOT_MASTER_KEY` is generated into `.env`. Back it up offline. **The VPS must use the same key** or credentials encrypted here won't decrypt there.
- **Seeded:** active `medium` risk profile, base `app_config`, and `BTC/USD` in the watchlist. Alpaca paper credentials are stored encrypted and decrypt cleanly.
- **Applying the SQL schema programmatically:** the `db/*.sql` files use `GO` batch separators (an SSMS directive, not T-SQL). pyodbc/SQLAlchemy can't run `GO` — use `scripts/apply_sql.py` (splits on `^\s*GO\s*$` and runs each batch via `exec_driver_sql`, so `:r`/`:n` in comments aren't mistaken for binds), or run the file in SSMS. All five schema files are applied to `CryptoBotV2`; each is re-runnable / idempotent (guarded `CREATE`s + `MERGE` seeds).
- **TLS interception on the dev box (seen 2026-05-30, since RESOLVED):** for a window on 2026-05-30, outbound HTTPS to Alpaca (`paper-api.alpaca.markets` and `data.alpaca.markets`) failed cert verification with `Hostname mismatch` — a TLS-intercepting proxy/antivirus/VPN/firewall presenting its own certificate. It broke `check_connectivity` and every live Alpaca call. It was **environmental, not code** (DB + decrypt kept passing) and later cleared on its own / once the interception was removed. If Alpaca calls suddenly fail with a cert hostname mismatch again, this is the cause — fix the interception (or run from the VPS); never disable TLS verification in the client to work around it.

## Commands

On the **Windows dev box** the venv already exists; invoke it directly (no activation needed):

```powershell
.\.venv\Scripts\python.exe -m scripts.check_connectivity   # Phase 1 green-light test
.\.venv\Scripts\python.exe -m scripts.insert_credentials   # you run it; Claude never sees raw keys
.\.venv\Scripts\python.exe -m scripts.run_signal_scan            # Phase 2 manual scan (all active symbols)
.\.venv\Scripts\python.exe -m scripts.run_signal_scan BTC/USD    # Phase 2 manual scan (specific symbol[s])
.\.venv\Scripts\python.exe -m scripts.run_trade_cycle --dry-run  # hourly cycle (exit+entry): size + report, NO orders
.\.venv\Scripts\python.exe -m scripts.run_trade_cycle            # hourly cycle: manages exits + places paper entries
.\.venv\Scripts\python.exe -m scripts.run_trade_cycle --force BTC/USD  # DEV: force an entry (Phase 3 green-light)
.\.venv\Scripts\python.exe -m scripts.run_position_manager --dry-run   # Phase 4 exit pass alone: report trails/exits, NO orders
.\.venv\Scripts\python.exe -m scripts.run_position_manager            # Phase 4 exit pass: trails/expands + liquidates on a trigger
.\.venv\Scripts\python.exe -m scripts.flatten_position 1 --dry-run    # report a manual flatten of position id 1, NO order / NO DB write
.\.venv\Scripts\python.exe -m scripts.flatten_position 1              # manually liquidate position id 1 → trade_history (exit_reason='manual')
.\.venv\Scripts\python.exe -m scripts.run_watchlist_filters          # Phase 5 admin: review entry filters for ALL watchlist symbols (read-only)
.\.venv\Scripts\python.exe -m scripts.run_watchlist_filters BTC/USD  # Phase 5 admin: review filters for specific symbol[s]
.\.venv\Scripts\python.exe -m scripts.run_watchlist_filters --active # Phase 5 admin: only is_active=1 symbols
.\.venv\Scripts\python.exe -m scripts.verify_phase4              # Phase 4 OFFLINE logic green-light (no DB/broker)
.\.venv\Scripts\python.exe -m scripts.verify_phase5              # Phase 5 OFFLINE logic green-light (no DB/broker)
.\.venv\Scripts\python.exe -m scripts.apply_sql db/05_schema_phase5.sql  # apply a .sql file (splits on GO)
```

Fresh setup (Linux VPS, run as a dedicated non-root user):

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Generate the master key ONCE, paste into .env (chmod 600) — or reuse the existing key:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

set -a; source .env; set +a
python -m scripts.insert_credentials
python -m scripts.check_connectivity
```

`scripts.insert_credentials` prompts interactively (getpass). The DB already has the Phase 1 schema applied; to re-apply elsewhere, create the database then run `db/01_schema_phase1.sql` in SSMS (the file is re-runnable / idempotent).

No test runner, linter, or build tooling is specified yet — confirm with the user / check `requirements.txt` before assuming one.

## Environment note

Development currently happens on Windows (`d:\CryptoBot V2`) with a working `.venv` and ODBC Driver 18 already installed. The bot's intended deployment target is a **Linux VPS** — but whether it ultimately *runs* from Windows or the VPS is still undecided, and that decision dictates where the master key + `.env` must live consistently. Deployment-shaped commands (chmod, chown, ODBC apt install, systemd/APScheduler) are Linux; keep that distinction when writing setup/ops code. Use PowerShell syntax for dev-box commands here (`.\.venv\Scripts\python.exe`), bash for VPS instructions.
