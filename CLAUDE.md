# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current repository state

**Phases 1 and 2 are built and green-lit (both 2026-05-29).** The repo holds working code plus the design docs:

- `SUMMARY.md` — the authoritative design doc. Full architecture, locked-in decisions, schema, security model, phase plan. **Read this first.**
- `TODO.md` — the phased, top-down build checklist with per-phase green-light tests.
- `core/` — `crypto.py` (Fernet), `db.py` (SQLAlchemy + pyodbc, retry, `get_active_credentials`), `config.py` (typed `app_config` accessor), `indicators.py` (pure EMA/RSI/ATR/vol-z), `levels.py` (rolling-extrema S/R + persist/read), `market_data.py` (closed-bars-only OHLCV fetch w/ Alpaca retry, `market_bars` upsert), `ta_engine.py` (the gatekeeper: `evaluate()` → signal + confidence + breakout boost).
- `db/01_schema_phase1.sql`, `db/02_schema_phase2.sql` — **both already applied** to the live `CryptoBotV2` DB.
- `scripts/` — `insert_credentials.py` (you run it; stores encrypted keys), `check_connectivity.py` (Phase 1 green-light), `run_signal_scan.py` (Phase 2 manual scan; persists signals + S/R, places no orders).
- `requirements.txt`, `.env.example`, `.gitignore`, `README.md`, and a local `.venv/` (Windows dev, deps installed).

Strategy parameters (indicator periods, confidence weights, S/R lookback, breakout/volume rules, cooldown) live in `app_config` under the `ta.*`, `sr.*`, `breakout.*`, `weights.*`, `scan.*`, `cooldown.*` keys — **tunable in SQL without code changes** (read via `core/config.py`). See the "Defaults chosen" block in `TODO.md` Phase 2.

**Phases 3–8 are not built yet.** The modules they call for (`core/sizing.py`, `execution.py`, `orchestrator.py`, `position_manager.py`, `ai_*.py`) and the `heartbeat` table **do not exist** — search/verify before referencing them. `positions`/`trade_history` are *declared* in the Phase 1 schema but unused until Phase 3 (and may be ALTERed then).

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
- **Orchestrator** (`core/orchestrator.py`) — the hourly cycle: data → levels → TA → for each symbol enter if not already held + signal + confidence ≥ floor; then check every open position for exits. One entry per symbol per bar; skip held symbols (no stacking); honor cool-down after a stop-out.
- **Position Manager** (`core/position_manager.py`) — dynamic TP expansion + SL trailing (SL only ever moves in favor of locked-in profit) + support-break/price-SL exits.
- **AI Research** (Phase 6, `core/ai_*.py`) — advisory only; suggestions land in a review table, not auto-applied.
- **Observability** (Phase 7) — structured JSON logs (structlog, daily-rotated), a `heartbeat` table written every N minutes, and an external monitor that alerts when the heartbeat stalls. A dead bot must not silently leave positions unmanaged. Critical alerts: crash with open positions, Alpaca auth failure, DB unreachable.
- **Crypto/DB infra** (`core/crypto.py` Fernet, `core/db.py` SQLAlchemy + pyodbc with retry).

### Schema by phase

The DB grows phase by phase; don't expect later tables to exist early. Phase 1 (`db/01_schema_phase1.sql`, applied): `api_credentials`, `app_config`, `risk_profile`, `watchlist`. The Phase 1 DDL **already fully declares** `positions` (incl. both `sl_price` *and* `support_break_price`, `confidence_at_entry`, `breakout_confirmed`, `risk_pct_used`, `alpaca_order_id`) and `trade_history` (incl. `exit_reason` CHECK ∈ {`price_sl`, `support_break`, `tp`, `manual`}) — no Phase 1 code reads them. Phase 2 (`db/02_schema_phase2.sql`, applied) adds `market_bars`, `signals`, `sr_levels` + seeds the strategy params into `app_config`. **Phase 3 begins by *using* the already-declared `positions`/`trade_history` columns**; only ALTER them if execution surfaces a genuine gap. Phase 7 adds `heartbeat`.

### Data-flow rule

The trade path is: scheduler → market data → watchlist filter → TA engine (with breakout boost) → `if signal AND (AI disabled OR AI did not veto)` → sizer → execution → write TP/SL + support-break → Position Manager manages exit. The TA gate sits before sizing/execution by construction.

### Module APIs (built: Phases 1–2)

Concrete callable surface so later phases don't have to re-read the files. All money/price values are plain `float`; the DB stores `DECIMAL(38,18)`.

- **`core/crypto.py`** — `encrypt(str) -> bytes`, `decrypt(bytes|str) -> str` (raises `RuntimeError` on master-key mismatch). Ciphertext stored as VARBINARY Fernet tokens.
- **`core/db.py`** — `get_engine()` (process singleton, `pool_pre_ping`); `connect_with_retry(max_attempts=3, base_delay=1.0)` (linear backoff; retries only `OperationalError`/`InterfaceError` — auth/programming errors fail fast); `healthcheck()`; `get_active_credentials(provider, environment) -> (key, secret)`.
- **`core/config.py`** — `get_str/get_int/get_float/get_bool(key, default)` over `app_config`; values cached per process, `refresh()` drops the cache. This is how every strategy knob is read — **never hard-code a tunable.**
- **`core/indicators.py`** — pure: `ema(values, period)`, `rsi(values, period=14)` (Wilder), `atr(highs, lows, closes, period=14)` (Wilder), `volume_zscore(volumes, lookback=20)`. Each returns the **latest** value as `float`, or `None` if insufficient data. Inputs ordered oldest→newest.
- **`core/levels.py`** — `detect_levels(highs, lows, *, lookback, method="rolling_extrema") -> SRLevels(support, resistance, method, lookback)` (level taken from the bars *before* the latest closed bar — never includes the bar being tested); `persist_levels(conn, symbol, levels, bar_ts_utc)`; `latest_levels(conn, symbol) -> SRLevels | None` (**the Position Manager's reader — use this, don't recompute**, per invariant #6).
- **`core/market_data.py`** — `Bar(symbol, ts, open, high, low, close, volume, trade_count, vwap)` (ts = bar START, naive UTC); `make_client(key, secret)`; `fetch_closed_bars(client, symbol, *, timeframe="1Hour", limit=250, now=None) -> list[Bar]` (**enforces closed-bars-only** by dropping any bar where `ts + duration > now`; retries transient Alpaca connection/timeout errors); `upsert_bars(conn, bars, *, timeframe="1Hour") -> int` (MERGE into `market_bars`).
- **`core/ta_engine.py`** — `TAParams.from_config()` (reads all `ta.*`/`sr.*`/`breakout.*`/`weights.*` keys), `TAParams.min_bars()`; `evaluate(symbol, bars, params) -> TASignal`. **`evaluate` returns a `TASignal` dataclass** (not a bare tuple): `.signal` (bool — the BUY gate, `= trend AND momentum`), `.confidence` (float 0–100), `.breakout_confirmed`, `.entry_hint`, `.stop_hint` (price-based SL candidate = `close - atr_stop_mult*ATR`), `.support_level`, `.resistance_level`, `.bar_ts`, `.snapshot` (dict of raw indicator values + component breakdown). Breakout/volume only *add* confidence — they can never manufacture a signal without trend+momentum.

**Phase 3 wiring:** `evaluate(...).entry_hint`/`.stop_hint` feed `core/sizing.py`; the executed position records `entry_price`, `sl_price` (from `stop_hint`), `support_break_price` (from `.support_level`), `confidence_at_entry` (from `.confidence`), and `breakout_confirmed`. The hard entry gate is `place_entry(ta_signal=True, ...)` (invariant #1).

## Phase discipline

Work strictly top-down through `TODO.md`. **No phase begins until the previous phase passes its green-light test.** Each phase has an explicit test checklist in `TODO.md`. Phase 1's green-light test (`scripts/check_connectivity.py`) shows four PASS lines: DB reachable, Alpaca creds decrypt, paper account returns equity, live quote returns bid/ask. **Phase 1 passed all four on 2026-05-29. Phase 2 (Market Data + TA Engine) also passed its green-light test on 2026-05-29 (`scripts/run_signal_scan.py`: per-symbol signal + 0–100 confidence, breakout boost verified at +20, S/R + signals persisted, no orders). Phase 3 (Execution + Initial TP/SL) is the active phase — pending user confirmation of the Phase 2 strategy defaults.**

## Live setup facts (current)

These describe the running environment, not the design ideal — they will change as the project hardens:

- **Database:** `CryptoBotV2` (note: **not** `tradebot`) on a remote SQL Server 2022 at `185.202.236.11`. Set `DB_NAME=CryptoBotV2` in `.env`.
- **DB auth:** currently `sa` over `Encrypt=yes` + `TrustServerCertificate=yes` (remote self-signed cert). This is for bring-up only — replace `sa` with a least-privilege login scoped to `CryptoBotV2` before the paper soak.
- **Master key:** a real `TRADEBOT_MASTER_KEY` is generated into `.env`. Back it up offline. **The VPS must use the same key** or credentials encrypted here won't decrypt there.
- **Seeded:** active `medium` risk profile, base `app_config`, and `BTC/USD` in the watchlist. Alpaca paper credentials are stored encrypted and decrypt cleanly.
- **Applying the SQL schema programmatically:** the `db/*.sql` files use `GO` batch separators (an SSMS directive, not T-SQL). pyodbc/SQLAlchemy can't run `GO` — split the file on `^\s*GO\s*$` and execute each batch separately (or run it in SSMS). Both `01_schema_phase1.sql` and `02_schema_phase2.sql` are already applied to `CryptoBotV2`; each file is re-runnable / idempotent (guarded `CREATE`s + `MERGE` seeds).

## Commands

On the **Windows dev box** the venv already exists; invoke it directly (no activation needed):

```powershell
.\.venv\Scripts\python.exe -m scripts.check_connectivity   # Phase 1 green-light test
.\.venv\Scripts\python.exe -m scripts.insert_credentials   # you run it; Claude never sees raw keys
.\.venv\Scripts\python.exe -m scripts.run_signal_scan            # Phase 2 manual scan (all active symbols)
.\.venv\Scripts\python.exe -m scripts.run_signal_scan BTC/USD    # Phase 2 manual scan (specific symbol[s])
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
