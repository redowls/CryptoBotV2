# Tradebot — Session Summary

> Handoff document. Captures every design decision, the full architecture,
> and the rules the codebase must enforce. Read this first, then `TODO.md`.

---

## 1. Project intent

A production-grade, automated **crypto** trading bot:

- Language: **Python**, deployed on a **Linux VPS**
- Storage: **SQL Server** (managed via SSMS) — config, encrypted API keys,
  watchlist, positions, trade history
- Broker: **Alpaca** crypto endpoints
- Cadence: **hourly** — the bot wakes every hour, evaluates the just-closed
  **1-hour bar** for every watchlist symbol, and acts on buy AND sell
  signals. (Changed from daily. See section 12 for the full implications.)
- Entry decision: **technical analysis is the gatekeeper**. No TA signal → no trade.
- Phase 2 AI layer: **research only** (news sentiment, price-context
  reasoning). Toggleable via `app_config.ai_enabled`. **AI can never create
  an entry the TA didn't already approve.** AI can lower confidence (veto/
  dampen) but cannot raise it past the TA gate.

**Reality check, written down so it stays written down:** this is an
engineering system, not investment advice. Mechanisms ≠ edge. Paper-trade
the full pipeline before a cent of real money is involved. Only ever risk
capital that can be lost entirely.

---

## 2. Locked-in decisions (from this session)

| # | Decision | Value |
|---|----------|-------|
| 1 | Start environment | **Paper trading only.** Live execution code is not written until paper runs end-to-end. |
| 2 | Master key storage | **Simple: env var loaded from `chmod 600` `.env` file**, owned by a dedicated non-root VPS user. DB stores only Fernet ciphertext. |
| 3 | SQL Server | Already installed. Auth mode + same-VPS-vs-separate still **TBD** (see open questions). |
| 4 | Alpaca account | Ready; user generates paper keys themselves. Claude never sees raw keys. |
| 5 | Risk philosophy | **Confidence scales position SIZE within a fixed per-trade loss cap.** Confidence does NOT loosen stops or exits. |
| 6 | Risk profile | **Medium.** Default: **1% max risk per trade at top confidence**, scaling down to **0.4% at minimum tradeable confidence (60)**. Stored in `risk_profile` table — change without code. |
| 7 | Confidence score | 0–100. Computed from TA in Phase 1/3. AI can subtract only. |
| 8 | Confidence gates | <60 = no trade · 60–75 = min size · 75–90 = mid size · 90+ = max size (still capped by 1%). |
| 9 | Portfolio ceiling | Max 5 simultaneous open positions; max 6% aggregate open risk. Per-trade caps alone don't protect against correlated losses. |
| 10 | **Resistance breakout → confidence boost** | **A confirmed close above a recent resistance level adds weight to the TA confidence score (bullish trend confirmation). More confidence → bigger size, still within the 1% cap.** |
| 11 | **Support break → stop loss** | **A confirmed close below a defined support level is an ADDITIONAL stop-loss trigger, alongside the price-based SL. Whichever fires first closes the position.** |

---

## 3. High-level architecture

Modular components, each independently testable:

- **Config & State Layer (SQL Server)** — single source of truth. API keys
  (encrypted), watchlist, strategy params, open positions, trade history,
  AI toggle, TP/SL state, support/resistance levels.
- **Market Data Module** — pulls OHLCV bars + live quotes from Alpaca for
  watchlist pairs. Also computes spreads/volume for liquidity filtering.
- **TA Signal Engine (the gatekeeper)** — computes indicators, detects
  support/resistance levels, produces a binary entry signal AND a
  confidence score. **Nothing buys unless this says yes.**
- **Execution Module** — places orders via Alpaca, records fills, writes
  initial TP/SL.
- **Position Manager / Scheduler** — periodic loop that watches open
  positions, expands TP when justified, trails SL to lock in profit, and
  checks for support-break exits.
- **Watchlist Manager** — applies liquidity / volume / spread / volatility
  filters to decide tradeable universe.
- **AI Research Layer (Phase 2, toggleable)** — news sentiment + price
  context. Advisory only. Wired so it physically cannot place an entry
  the TA engine didn't approve.
- **Orchestrator / Daemon** — scheduling (APScheduler or systemd timers),
  structured logging, error recovery, heartbeat.
- **Observability** — structured logs + alerting. A dead bot must not
  silently leave positions unmanaged.

### Data flow for a trade

```
Scheduler wakes EVERY HOUR (just after the 1h bar closes)
  → Market Data refreshes the latest CLOSED 1-hour bars
  → Watchlist Manager confirms pair active & passes filters
  → TA Engine evaluates:
        base indicators (trend/momentum/volume/volatility)
        + resistance-breakout check  → adds confidence if broken
        → (signal?, confidence 0-100, entry_hint, stop_hint, support_level)
  → if signal AND (AI disabled OR AI did not veto)
  → Position Sizer: size = (equity × risk%) / (entry − stop_price)
       where risk% scales between min_risk and max_risk by confidence
  → Execution buys
  → Initial TP/SL written to DB (SL = price-based SL; support-break recorded too)
  → Position Manager loop manages exit dynamically:
        price hits SL  → exit
        OR confirmed close below support → exit
        OR TP expansion / SL trailing as price runs in favor
```

### Structural rule (must be enforced in code)

The entry function takes a required `ta_signal: bool` gate. The AI's
output is only ever a *modifier* on confidence, never a creator of
entries. Phase 2 cannot subvert Phase 1 by construction.

---

## 4. Position sizing math (locked formula)

```
risk_pct(confidence) = lerp(min_risk_per_trade_pct,
                            max_risk_per_trade_pct,
                            normalize(confidence, 60, 100))

position_size_quote = (equity × risk_pct) / abs(entry_price − stop_price)
```

- `equity` from Alpaca account
- `entry_price`, `stop_price` from TA engine
- `risk_pct` interpolated linearly between profile min/max based on confidence
- Below `min_confidence_to_trade` → no trade
- Final size also clamped by `max_portfolio_risk_pct` ceiling

This guarantees: the most confident trade commits the most capital and
has the most upside, but no single trade can lose more than the cap.

---

## 5. Support / Resistance logic (NEW — decisions 10 & 11)

These two rules are intentionally symmetric: resistance affects the
**entry confidence**, support affects the **exit (stop loss)**.

### 5a. Resistance breakout → confidence boost (entry side)

- The TA engine detects a recent **resistance level** per symbol (e.g.
  the swing-high / rolling max over a lookback window, or a pivot-based
  level).
- If the latest **confirmed closed bar** closes **above** that resistance,
  it counts as a bullish breakout and adds a weighted component to the
  confidence score.
- Higher confidence → larger position size, still capped at the 1%
  per-trade ceiling. It does **not** loosen the stop. (Consistent with
  decision #5.)
- Optional strengthener (recommend wiring as a config flag): require the
  breakout to be accompanied by above-average volume to count — a
  low-volume "break" is often a fakeout.

### 5b. Support break → stop loss (exit side)

- The TA engine also detects a recent **support level** per symbol (swing
  low / rolling min / pivot).
- This produces a second exit trigger that runs ALONGSIDE the normal
  price-based stop loss:
  - **Price-based SL**: the calculated `stop_price` used for sizing.
  - **Support-break SL**: a confirmed closed bar **below** the support
    level forces an exit.
- **Whichever triggers first closes the position.** This matters because
  the support level can sit either above or below the calculated SL price,
  and either breakdown should be able to fire.
- Both thresholds are recorded on the position so the Position Manager
  knows exactly what to watch.

### Shared rules for both

- **Confirmed closed bars only.** Crypto wicks through levels constantly on
  noise; reacting to intrabar pierces causes churn. (Same principle as the
  look-ahead-bias rule.)
- Support/resistance levels are recomputed on each scan and **persisted**
  so the Position Manager (which may run more often than the signal scan)
  always has the current levels for open positions.
- Level-detection method (rolling extrema vs pivot points vs other) is an
  open design choice — see open questions.

---

## 6. Database schema

### Phase 1 (implemented)

DDL lives in `db/01_schema_phase1.sql`.

- **`api_credentials`** — encrypted keys (VARBINARY Fernet ciphertext),
  per-provider/per-environment, with rotation timestamps.
- **`app_config`** — key/value runtime config. Seeds include
  `environment=paper`, `ai_enabled=false`, `active_risk_profile=medium`.
- **`risk_profile`** — the fixed risk envelope (max/min risk%, confidence
  floor, portfolio ceiling). Medium row seeded active.
- **`watchlist`** — tradeable symbols, with `added_by` tracking manual vs
  AI suggestion.
- **`positions`, `trade_history`** — declared, defined in Phase 3.

### Later phases (to add) — now including S/R

- **`sr_levels`** (NEW) — per symbol: `support_price`, `resistance_price`,
  `method`, `lookback`, `computed_at_utc`. Recomputed each scan, read by
  both the TA engine and the Position Manager.
- **`positions`** must include both `sl_price` (price-based) and
  `support_break_price` (the support-break trigger), plus
  `confidence_at_entry` and a `breakout_confirmed` flag.

All timestamps UTC. Convert only at display.

---

## 7. Security model

The master key is the **only** secret outside the DB. Lose it → every
stored credential is undecryptable and must be re-inserted.

- `TRADEBOT_MASTER_KEY` lives in `.env` (chmod 600, owned by a dedicated
  non-root user), loaded into the environment at process start.
- DB stores only Fernet ciphertext. Plaintext keys never written, never
  logged, never echoed.
- Alpaca keys: use trading-only scope (no withdrawal capability) to limit
  blast radius if leaked.
- TLS in transit to SQL Server (`Encrypt=yes`).
- An attacker needs **both** a DB dump AND filesystem access as the bot
  user to compromise keys. Acceptable for a single VPS; upgrade to Vault
  or cloud KMS later without schema changes.

---

## 8. Phases at a glance

| Phase | What it delivers | Status |
|------|-----------------|--------|
| **1. Foundation** | DB schema, encrypted key storage, verified paper Alpaca connectivity | **Code complete** — awaiting green-light test on user's VPS |
| **2. Market Data + TA Engine** | OHLCV ingest, indicators, S/R detection, resistance-breakout confidence, signal + confidence score, no orders yet | Not started |
| **3. Execution + Initial TP/SL** | Place paper orders on TA signal, record fills, write static TP/SL + support-break trigger | Not started |
| **4. Dynamic TP/SL + Trailing** | Scheduler expands TP when warranted; SL trails; support-break exit enforced | Not started |
| **5. Watchlist Filters** | Liquidity / volume / spread / volatility filters; admin tools | Not started |
| **6. AI Research Layer (toggle off by default)** | News sentiment + price-context reasoning, advisory only | Not started |
| **7. Observability + Alerting** | Heartbeat, structured logs, failure alerts | Not started |
| **8. Paper soak test → Live readiness** | Multi-week paper run, then live readiness review | Not started |

Each phase is gated. No phase begins until the previous one has passed
its green-light test.

---

## 9. Open questions still owed back to design

1. **SQL Server location & auth:** same VPS as the bot, or separate
   machine? SQL auth or Windows/Kerberos?
2. **Final risk % at max confidence:** keep 1%, or change to 0.5% / 2%?
3. **Capital size at launch.** Tiny accounts get eaten by fees and minimum
   order sizes — worth a sanity check before Phase 3.
4. **Phase 2 — which base TA indicators drive the signal?** Starter set:
   trend (EMA cross or ADX), momentum (RSI), volume confirmation,
   volatility (ATR).
5. **S/R detection method (NEW):** rolling extrema (swing high/low over N
   bars), pivot points, or another method? And what lookback window?
6. **Breakout confirmation (NEW):** require above-average volume on the
   breakout bar to count, or price-close alone? (Recommend volume-confirmed.)
7. **Confidence weight of the resistance breakout (NEW):** how many points
   does it add, relative to the other indicator weights?
8. **Hourly run timing (DECIDED: hourly).** Bot wakes every hour just
   after the 1h bar closes. Still open: how many seconds after the hour
   boundary to wait for the bar to finalize on Alpaca's side (suggest 30–60s)?
9. **Cool-down rules (now important — hourly whipsaws far more than daily).**
   After a stop-out (price OR support break), how many hours/bars before
   re-entry on that symbol is allowed?
10. **Slippage / fee model** for sizing accuracy.

---

## 10. Risks and gotchas catalogue (running list)

- **Foundation cracks are the #1 cause of bot disasters** — not strategy.
  Phase 1 exists to eliminate these.
- **Linux → SQL Server ODBC** is fussy. `msodbcsql18` + `unixodbc-dev`.
- **Crypto markets are 24/7** but Alpaca has maintenance windows. Tolerate
  transient 5xx with backoff.
- **Partial fills**: order requested ≠ order filled. Record actual fills.
- **Correlated drawdowns**: crypto often moves together. The 6% aggregate
  cap is the guard.
- **Look-ahead bias in TA**: compute on confirmed closed bars only. This
  now explicitly includes the resistance-breakout and support-break checks.
- **Fakeout breakouts (NEW):** a resistance break on thin volume often
  reverses. Volume confirmation mitigates this — strongly recommended.
- **Whipsaw on support breaks (NEW):** a single wick below support can
  trigger a premature exit. Use confirmed closes, not intrabar lows.
- **Two SLs can conflict (NEW):** the price-based SL and the support-break
  SL must be evaluated together — whichever fires first wins — and both
  recorded on the position so the Position Manager isn't guessing.
- **Stale S/R levels (NEW):** if the Position Manager runs more often than
  the signal scan, it must read persisted `sr_levels`, not recompute
  inconsistently. Persist levels; don't recalculate ad hoc.
- **Wallet vs watchlist drift**: if a held symbol is removed from watchlist
  while losing → trigger SL instead of silent deactivation.
- **Stop-loss in crypto can slip badly** during flash crashes. Stop = a
  *trigger*, not a guarantee. Size assuming a realistic worst fill.
- **Master-key loss = total credential loss.** Back it up offline.

---

## 11. File inventory (Phase 1, ready to use)

```
tradebot/
├── README.md
├── requirements.txt
├── .env.example
├── db/
│   └── 01_schema_phase1.sql      # run in SSMS
├── core/
│   ├── __init__.py
│   ├── crypto.py                 # Fernet encrypt/decrypt
│   └── db.py                     # SQLAlchemy + pyodbc, retrying
└── scripts/
    ├── __init__.py
    ├── insert_credentials.py     # YOU run locally; stores encrypted keys
    └── check_connectivity.py     # the green-light test
```

The green-light test verifies four things end-to-end:
1. DB reachable
2. Alpaca credentials decrypt
3. Alpaca paper account returns equity
4. Live crypto quote returns bid/ask

When all four PASS → Phase 1 green light → Phase 2 begins.

---

## 12. Cadence: hourly (CHANGED from daily)

The bot now runs on an **hourly** cycle instead of daily. This is a real
strategy change, not just a polling-frequency tweak.

### What it means mechanically
- The scheduler fires **every hour**, a short delay (≈30–60s) after the
  hour boundary so the 1-hour bar has finalized on Alpaca's side.
- The TA engine, support/resistance detection, and breakout/breakdown
  checks all now operate on **1-hour bars**, not daily bars.
- Each cycle: refresh latest closed 1h bars → detect levels → run TA →
  for each symbol, check for a BUY entry (if not already held) and check
  open positions for SELL/exit conditions.
- **Confirmed closed bar rule still holds** — now it's the closed *hourly*
  bar. Never evaluate the forming (current, incomplete) hour.

### Entry frequency
- At most **one new entry per symbol per hourly bar**.
- Before entering, the orchestrator MUST check whether the symbol is
  already held and skip if so (no stacking).
- A **cool-down** after a stop-out is now important (open question #9):
  hourly strategies whipsaw far more than daily, so re-entering a symbol
  the very next hour after being stopped out is a common way to bleed.

### Exit frequency (this is the upside of hourly)
- Open positions are now checked for exits **every hour** instead of once
  a day. Support-break and stop-loss triggers fire much sooner. This is a
  genuine safety improvement on the exit side.

### Costs and risks that grow under hourly
- **More trades → more fees.** The fee/slippage model (open question #10)
  is now more important, not less. A strategy profitable on daily bars can
  be net-negative hourly purely from fees + whipsaw.
- **More noise.** Hourly bars are noisier than daily; expect more false
  signals. Volume-confirmed breakouts and confirmed-close support breaks
  matter even more here.
- **More API calls.** 24× the data pulls per day. Stay within Alpaca rate
  limits; cache bars in `market_bars` rather than re-fetching.
- **The Position Manager and signal scan now run at similar frequency**,
  but still: persist S/R levels and read them, don't recompute ad hoc.
