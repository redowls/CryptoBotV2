/* ==========================================================================
   Tradebot — Phase 2 schema (Market Data + TA Signal Engine)
   --------------------------------------------------------------------------
   Apply in SSMS (or via the GO-splitting loader — pyodbc cannot run GO):
       USE CryptoBotV2;
       :r 02_schema_phase2.sql

   Adds the three Phase 2 tables and seeds the tunable strategy parameters
   into app_config so the TA engine reads them from SQL (no code changes to
   retune). NO orders are placed in Phase 2 — these tables only hold cached
   bars, computed S/R levels, and emitted signals.

   Conventions (same as Phase 1):
   - All timestamps UTC via SYSUTCDATETIME(). Convert only at display.
   - Prices/qty use DECIMAL(38,18) to span large (BTC) and tiny (alt) values.
   - Re-runnable: object creation is guarded; seeds use MERGE (idempotent).
   ========================================================================== */

SET NOCOUNT ON;
GO

/* --------------------------------------------------------------------------
   market_bars — cached OHLCV. Hourly cadence = 24x the API calls of daily,
   so bars are cached here and re-read rather than re-fetched. ts_utc is the
   bar's START time; a 1Hour bar at 14:00 covers [14:00, 15:00) and is only
   stored once it has CLOSED (no forming bar). PK spans symbol+timeframe+ts.
   -------------------------------------------------------------------------- */
IF OBJECT_ID(N'dbo.market_bars', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.market_bars (
        symbol          NVARCHAR(30)   NOT NULL,
        timeframe       NVARCHAR(10)   NOT NULL,          -- '1Hour'
        ts_utc          DATETIME2(3)   NOT NULL,          -- bar START time (UTC)
        open_price      DECIMAL(38,18) NOT NULL,
        high_price      DECIMAL(38,18) NOT NULL,
        low_price       DECIMAL(38,18) NOT NULL,
        close_price     DECIMAL(38,18) NOT NULL,
        volume          DECIMAL(38,18) NOT NULL,
        trade_count     INT            NULL,
        vwap            DECIMAL(38,18) NULL,
        inserted_at_utc DATETIME2(3)   NOT NULL CONSTRAINT DF_market_bars_inserted DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT PK_market_bars PRIMARY KEY (symbol, timeframe, ts_utc)
    );
END
GO

/* --------------------------------------------------------------------------
   sr_levels — support/resistance per symbol, RECOMPUTED each scan and
   PERSISTED. History is kept (one row per scan); readers take the latest by
   computed_at_utc. The Position Manager (Phase 4) reads these levels and
   never recomputes ad hoc, so an open position is always judged against the
   same level the signal scan saw. bar_ts_utc is the latest CLOSED bar the
   level was derived from (audit / look-ahead guard).
   -------------------------------------------------------------------------- */
IF OBJECT_ID(N'dbo.sr_levels', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.sr_levels (
        id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_sr_levels PRIMARY KEY,
        symbol          NVARCHAR(30)   NOT NULL,
        support_price   DECIMAL(38,18) NULL,
        resistance_price DECIMAL(38,18) NULL,
        method          NVARCHAR(30)   NOT NULL,          -- 'rolling_extrema'
        lookback        INT            NOT NULL,
        bar_ts_utc      DATETIME2(3)   NOT NULL,          -- latest closed bar used
        computed_at_utc DATETIME2(3)   NOT NULL CONSTRAINT DF_sr_levels_computed DEFAULT (SYSUTCDATETIME())
    );

    CREATE INDEX IX_sr_levels_symbol_latest ON dbo.sr_levels (symbol, computed_at_utc DESC);
END
GO

/* --------------------------------------------------------------------------
   signals — every TA evaluation, persisted for audit and Phase 3 entry.
   signal is the binary BUY setup gate (the gatekeeper). confidence (0-100)
   scales SIZE only (Phase 3), never safety rails. breakout_confirmed records
   whether a volume-confirmed close above resistance boosted confidence.
   indicator_snapshot is the JSON of raw indicator values for spot-checking.
   bar_ts_utc is the CLOSED bar evaluated — unique per (symbol, bar) so a
   re-scan of the same bar overwrites rather than duplicates.
   -------------------------------------------------------------------------- */
IF OBJECT_ID(N'dbo.signals', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.signals (
        id                 INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_signals PRIMARY KEY,
        symbol             NVARCHAR(30)   NOT NULL,
        bar_ts_utc         DATETIME2(3)   NOT NULL,       -- the CLOSED bar evaluated
        signal             BIT            NOT NULL,        -- BUY setup present?
        confidence         DECIMAL(5,2)   NOT NULL,        -- 0-100
        breakout_confirmed BIT            NOT NULL CONSTRAINT DF_signals_breakout DEFAULT (0),
        entry_hint         DECIMAL(38,18) NULL,
        stop_hint          DECIMAL(38,18) NULL,            -- price-based SL candidate
        support_level      DECIMAL(38,18) NULL,
        resistance_level   DECIMAL(38,18) NULL,
        indicator_snapshot NVARCHAR(MAX)  NULL,            -- JSON of raw indicator values
        created_at_utc     DATETIME2(3)   NOT NULL CONSTRAINT DF_signals_created DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT UQ_signals_symbol_bar UNIQUE (symbol, bar_ts_utc)
    );

    CREATE INDEX IX_signals_symbol_bar ON dbo.signals (symbol, bar_ts_utc DESC);
END
GO

/* --------------------------------------------------------------------------
   Phase 2 strategy parameters — seeded into app_config so the TA engine
   reads them from SQL. Retune in SQL, no code change. MERGE = idempotent;
   it INSERTs missing keys but never clobbers a value you have since tuned.
   -------------------------------------------------------------------------- */
MERGE dbo.app_config AS t
USING (VALUES
    -- Bar fetch / cadence
    (N'scan.timeframe',          N'1Hour'),   -- Alpaca TimeFrame; engine runs on the closed 1h bar
    (N'scan.bars_to_fetch',      N'250'),     -- history pulled per scan (warmup + lookback headroom)

    -- Indicator periods
    (N'ta.ema_fast_period',      N'12'),
    (N'ta.ema_slow_period',      N'26'),
    (N'ta.rsi_period',           N'14'),
    (N'ta.atr_period',           N'14'),

    -- Indicator thresholds
    (N'ta.rsi_bull_min',         N'50'),      -- RSI must be >= this for a bullish setup
    (N'ta.rsi_overbought',       N'70'),      -- RSI >= this => too extended, no fresh entry
    (N'ta.vol_lookback',         N'20'),      -- window for the volume z-score
    (N'ta.vol_z_min',            N'0.5'),     -- volume z-score >= this counts as volume confirmation

    -- Price-based stop (sizing input): stop = entry - atr_stop_mult * ATR
    (N'ta.atr_stop_mult',        N'1.5'),

    -- Support/Resistance detection
    (N'sr.method',               N'rolling_extrema'),  -- rolling max/min of the prior `lookback` closed bars
    (N'sr.lookback',             N'20'),

    -- Resistance breakout (entry-side confidence boost)
    (N'breakout.require_volume', N'true'),    -- a low-volume break is often a fakeout
    (N'breakout.vol_z_min',      N'1.0'),     -- volume z-score required to confirm the breakout

    -- Confidence weights (component points; trend+momentum+volume sum to 80,
    -- breakout adds the remaining 20 -> max 100). Retune freely.
    (N'weights.trend',           N'40'),
    (N'weights.momentum',        N'25'),
    (N'weights.volume',          N'15'),
    (N'weights.breakout',        N'20'),

    -- Cool-down after a stop-out (consumed in Phase 3/4; seeded now so the
    -- knob exists in one place). Bars == hours at the 1h cadence.
    (N'cooldown.bars_after_stop', N'3')
) AS s (config_key, config_value)
ON (t.config_key = s.config_key)
WHEN NOT MATCHED BY TARGET THEN
    INSERT (config_key, config_value) VALUES (s.config_key, s.config_value);
GO

PRINT 'Phase 2 schema applied.';
GO
