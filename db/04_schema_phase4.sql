-- ============================================================
-- Phase 4 schema: Position Manager
--   * position_adjustments — audit trail of every SL/TP/support change
--   * pm.* config seeds (trailing + TP-expansion knobs, tunable in SQL)
-- positions + trade_history were already FULLY declared in Phase 1; the
-- Position Manager only WRITES to them (status->closed, trade_history rows).
-- Re-runnable: guarded CREATE + MERGE seeds.
-- ============================================================

SET NOCOUNT ON;
GO

-- ----- position_adjustments (audit every TP/SL/support change) -----
IF OBJECT_ID('dbo.position_adjustments', 'U') IS NULL
BEGIN
    CREATE TABLE dbo.position_adjustments (
        id               INT IDENTITY(1,1) PRIMARY KEY,
        position_id      INT NOT NULL,
        symbol           VARCHAR(20) NOT NULL,
        adjustment_type  VARCHAR(16) NOT NULL
            CHECK (adjustment_type IN ('sl_trail','tp_expand','support_trail')),
        old_value        DECIMAL(38,18) NULL,
        new_value        DECIMAL(38,18) NOT NULL,
        bar_ts_utc       DATETIME2 NOT NULL,
        created_at_utc   DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
    );
END
GO

-- ----- seed Position Manager params into app_config -----
-- (app_config is a 2-column key/value table; same MERGE style as Phases 1-3.)
MERGE dbo.app_config AS t
USING (VALUES
    -- Trailing stop: new_sl = max(old_sl, close - pm.trail_atr_mult * ATR),
    -- only while in profit. SL never moves backward (invariant #3).
    (N'pm.trail_atr_mult',                N'1.5'),
    (N'pm.trail_only_in_profit',          N'true'),

    -- TP expansion while trend AND momentum still hold:
    -- new_tp = max(old_tp, close + pm.tp_r_multiple * (close - new_sl)).
    (N'pm.tp_r_multiple',                 N'2.0'),
    (N'pm.tp_expand_requires_alignment',  N'true'),

    -- Trail the support-break trigger up toward price (in favor only).
    (N'pm.support_trail_enabled',         N'true')
) AS s (config_key, config_value)
ON (t.config_key = s.config_key)
WHEN NOT MATCHED BY TARGET THEN
    INSERT (config_key, config_value) VALUES (s.config_key, s.config_value);
GO

PRINT 'Phase 4 schema applied (position_adjustments + pm.* config seed).';
GO
