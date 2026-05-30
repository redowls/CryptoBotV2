/* ==========================================================================
   Tradebot — Phase 3 schema (Execution + Initial TP/SL)
   --------------------------------------------------------------------------
   Apply in SSMS (or via the GO-splitting loader — pyodbc cannot run GO):
       USE CryptoBotV2;
       :r 03_schema_phase3.sql
   Or:  .\.venv\Scripts\python.exe -m scripts.apply_sql db/03_schema_phase3.sql

   No NEW tables in Phase 3. `positions` and `trade_history` were already
   FULLY declared in 01_schema_phase1.sql (both stop triggers, confidence,
   breakout flag, risk_pct_used, alpaca_order_id, exit_reason CHECK) — Phase 3
   simply starts WRITING to those columns. We only seed the execution/sizing
   knobs into app_config so the trade path is tunable in SQL without code.

   Conventions (same as Phases 1-2):
   - All timestamps UTC via SYSUTCDATETIME(). Convert only at display.
   - Re-runnable: seeds use MERGE (idempotent); never clobbers a tuned value.
   ========================================================================== */

SET NOCOUNT ON;
GO

/* --------------------------------------------------------------------------
   Phase 3 execution / sizing parameters — seeded into app_config.

   Sizing reads the risk ENVELOPE (max/min risk %, confidence floor, portfolio
   ceiling, max open positions) from the risk_profile table, not from here.
   These keys only cover the execution mechanics + the initial take-profit.
   -------------------------------------------------------------------------- */
MERGE dbo.app_config AS t
USING (VALUES
    -- Initial take-profit, expressed as a reward:risk multiple of the
    -- per-unit risk R = (entry - price_SL). TP = entry + tp.r_multiple * R.
    -- Static in Phase 3; the Position Manager (Phase 4) expands it.
    (N'tp.r_multiple',            N'2.0'),

    -- Order mechanics. Crypto market orders use GTC time-in-force on Alpaca.
    (N'execution.time_in_force',  N'gtc'),
    (N'execution.poll_attempts',  N'5'),     -- times to poll the order for a fill
    (N'execution.poll_delay_secs', N'1.0'),  -- delay between fill polls

    -- Reject dust orders: a tiny notional gets eaten by fees / Alpaca minimums.
    (N'execution.min_notional',   N'1.0')
) AS s (config_key, config_value)
ON (t.config_key = s.config_key)
WHEN NOT MATCHED BY TARGET THEN
    INSERT (config_key, config_value) VALUES (s.config_key, s.config_value);
GO

PRINT 'Phase 3 schema applied (config seed only; positions/trade_history already declared).';
GO
