/* ==========================================================================
   Tradebot — Phase 1 schema
   --------------------------------------------------------------------------
   Apply in SSMS:
       CREATE DATABASE tradebot;        -- run once, separately
       USE tradebot;                    -- then run this file with tradebot selected
       :r 01_schema_phase1.sql          -- or open + execute in SSMS

   Conventions:
   - All timestamps are UTC via SYSUTCDATETIME(). Convert only at display.
   - Credentials store ONLY Fernet ciphertext (VARBINARY). Never plaintext.
   - Re-runnable: object creation is guarded; seeds use MERGE (idempotent).
   ========================================================================== */

SET NOCOUNT ON;
GO

/* --------------------------------------------------------------------------
   api_credentials — encrypted broker/API keys, per provider + environment.
   At most one active row per (provider, environment), enforced by a filtered
   unique index. insert_credentials.py deactivates the old row on rotation.
   -------------------------------------------------------------------------- */
IF OBJECT_ID(N'dbo.api_credentials', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.api_credentials (
        id                   INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_api_credentials PRIMARY KEY,
        provider             NVARCHAR(50)   NOT NULL,   -- e.g. 'alpaca'
        key_label            NVARCHAR(100)  NULL,       -- human-readable label
        environment          NVARCHAR(20)   NOT NULL,   -- 'paper' | 'live'
        encrypted_api_key    VARBINARY(MAX) NOT NULL,   -- Fernet token bytes
        encrypted_api_secret VARBINARY(MAX) NOT NULL,   -- Fernet token bytes
        is_active            BIT            NOT NULL CONSTRAINT DF_api_credentials_is_active DEFAULT (1),
        created_at_utc       DATETIME2(3)   NOT NULL CONSTRAINT DF_api_credentials_created DEFAULT (SYSUTCDATETIME()),
        rotated_at_utc       DATETIME2(3)   NULL
    );

    CREATE UNIQUE INDEX UX_api_credentials_active
        ON dbo.api_credentials (provider, environment)
        WHERE is_active = 1;
END
GO

/* --------------------------------------------------------------------------
   app_config — key/value runtime config (single source of truth).
   -------------------------------------------------------------------------- */
IF OBJECT_ID(N'dbo.app_config', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.app_config (
        config_key     NVARCHAR(100) NOT NULL CONSTRAINT PK_app_config PRIMARY KEY,
        config_value   NVARCHAR(400) NULL,
        updated_at_utc DATETIME2(3)  NOT NULL CONSTRAINT DF_app_config_updated DEFAULT (SYSUTCDATETIME())
    );
END
GO

MERGE dbo.app_config AS t
USING (VALUES
    (N'environment',         N'paper'),
    (N'ai_enabled',          N'false'),
    (N'active_risk_profile', N'medium')
) AS s (config_key, config_value)
ON (t.config_key = s.config_key)
WHEN NOT MATCHED BY TARGET THEN
    INSERT (config_key, config_value) VALUES (s.config_key, s.config_value);
GO

/* --------------------------------------------------------------------------
   risk_profile — the fixed risk envelope. Confidence scales position SIZE
   between min and max risk%; it never loosens these rails. Exactly one row
   may be active (filtered unique index). Tunable in SQL without code changes.
   -------------------------------------------------------------------------- */
IF OBJECT_ID(N'dbo.risk_profile', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.risk_profile (
        name                    NVARCHAR(50) NOT NULL CONSTRAINT PK_risk_profile PRIMARY KEY,  -- 'medium'
        max_risk_per_trade_pct  DECIMAL(5,2) NOT NULL,   -- risk% at top confidence (100)
        min_risk_per_trade_pct  DECIMAL(5,2) NOT NULL,   -- risk% at the confidence floor
        min_confidence_to_trade INT          NOT NULL,   -- below this => no trade (60)
        max_portfolio_risk_pct  DECIMAL(5,2) NOT NULL,   -- aggregate open-risk ceiling (6.0)
        max_open_positions      INT          NOT NULL,   -- simultaneous position cap (5)
        is_active               BIT          NOT NULL CONSTRAINT DF_risk_profile_is_active DEFAULT (0),
        updated_at_utc          DATETIME2(3) NOT NULL CONSTRAINT DF_risk_profile_updated DEFAULT (SYSUTCDATETIME())
    );

    CREATE UNIQUE INDEX UX_risk_profile_active
        ON dbo.risk_profile (is_active)
        WHERE is_active = 1;
END
GO

MERGE dbo.risk_profile AS t
USING (VALUES
    (N'medium', 1.00, 0.40, 60, 6.00, 5, 1)
) AS s (name, max_risk_per_trade_pct, min_risk_per_trade_pct,
        min_confidence_to_trade, max_portfolio_risk_pct, max_open_positions, is_active)
ON (t.name = s.name)
WHEN NOT MATCHED BY TARGET THEN
    INSERT (name, max_risk_per_trade_pct, min_risk_per_trade_pct,
            min_confidence_to_trade, max_portfolio_risk_pct, max_open_positions, is_active)
    VALUES (s.name, s.max_risk_per_trade_pct, s.min_risk_per_trade_pct,
            s.min_confidence_to_trade, s.max_portfolio_risk_pct, s.max_open_positions, s.is_active);
GO

/* --------------------------------------------------------------------------
   watchlist — tradeable universe. added_by tracks manual vs AI suggestion.
   -------------------------------------------------------------------------- */
IF OBJECT_ID(N'dbo.watchlist', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.watchlist (
        id           INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_watchlist PRIMARY KEY,
        symbol       NVARCHAR(30) NOT NULL CONSTRAINT UQ_watchlist_symbol UNIQUE,  -- 'BTC/USD'
        base_asset   NVARCHAR(20) NOT NULL,   -- 'BTC'
        quote_asset  NVARCHAR(20) NOT NULL,   -- 'USD'
        is_active    BIT          NOT NULL CONSTRAINT DF_watchlist_is_active DEFAULT (1),
        added_by     NVARCHAR(20) NOT NULL CONSTRAINT DF_watchlist_added_by DEFAULT (N'manual'),  -- 'manual' | 'ai'
        added_at_utc DATETIME2(3) NOT NULL CONSTRAINT DF_watchlist_added DEFAULT (SYSUTCDATETIME())
    );
END
GO

/* --------------------------------------------------------------------------
   positions / trade_history — DECLARED here (SUMMARY §6). The columns follow
   the Phase 3 spec in TODO.md so the trade path has a stable target; Phase 3
   may ALTER as execution details firm up. No code reads these in Phase 1.

   Invariants baked into the shape:
   - Two independent stop triggers per position: sl_price (price-based) AND
     support_break_price. The Position Manager must never guess either.
   - exit_reason distinguishes 'price_sl' vs 'support_break' vs 'tp'.
   - Prices/qty use DECIMAL(38,18) to span large (BTC) and tiny (alt) values.
   -------------------------------------------------------------------------- */
IF OBJECT_ID(N'dbo.positions', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.positions (
        id                   INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_positions PRIMARY KEY,
        symbol               NVARCHAR(30)  NOT NULL,
        side                 NVARCHAR(10)  NOT NULL CONSTRAINT DF_positions_side DEFAULT (N'buy'),
        qty                  DECIMAL(38,18) NOT NULL,           -- ACTUAL filled qty, not requested
        entry_price          DECIMAL(38,18) NOT NULL,
        entry_ts_utc         DATETIME2(3)  NOT NULL CONSTRAINT DF_positions_entry DEFAULT (SYSUTCDATETIME()),
        status               NVARCHAR(15)  NOT NULL CONSTRAINT DF_positions_status DEFAULT (N'open'),  -- 'open' | 'closed'
        tp_price             DECIMAL(38,18) NULL,
        sl_price             DECIMAL(38,18) NULL,               -- price-based stop
        support_break_price  DECIMAL(38,18) NULL,               -- 2nd stop trigger (confirmed close below)
        confidence_at_entry  DECIMAL(5,2)  NULL,
        breakout_confirmed   BIT           NOT NULL CONSTRAINT DF_positions_breakout DEFAULT (0),
        risk_pct_used        DECIMAL(5,2)  NULL,
        alpaca_order_id      NVARCHAR(64)  NULL,
        created_at_utc       DATETIME2(3)  NOT NULL CONSTRAINT DF_positions_created DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT CK_positions_status CHECK (status IN (N'open', N'closed'))
    );

    CREATE INDEX IX_positions_open ON dbo.positions (symbol, status);
END
GO

IF OBJECT_ID(N'dbo.trade_history', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.trade_history (
        id             INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_trade_history PRIMARY KEY,
        position_id    INT           NULL CONSTRAINT FK_trade_history_position
                                          REFERENCES dbo.positions (id),
        symbol         NVARCHAR(30)  NOT NULL,
        side           NVARCHAR(10)  NOT NULL,
        qty            DECIMAL(38,18) NOT NULL,
        entry_price    DECIMAL(38,18) NOT NULL,
        exit_price     DECIMAL(38,18) NULL,
        realized_pnl   DECIMAL(38,18) NULL,
        fees           DECIMAL(38,18) NULL,
        exit_reason    NVARCHAR(20)  NULL,    -- 'price_sl' | 'support_break' | 'tp' | 'manual'
        opened_at_utc  DATETIME2(3)  NULL,
        closed_at_utc  DATETIME2(3)  NULL,
        created_at_utc DATETIME2(3)  NOT NULL CONSTRAINT DF_trade_history_created DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT CK_trade_history_exit_reason
            CHECK (exit_reason IS NULL OR exit_reason IN
                   (N'price_sl', N'support_break', N'tp', N'manual'))
    );
END
GO

PRINT 'Phase 1 schema applied.';
GO
