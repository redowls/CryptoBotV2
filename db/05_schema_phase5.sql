-- ============================================================
-- Phase 5 schema: Watchlist Filters
--   * filters.* config seeds (liquidity / spread / volatility knobs + the
--     wallet-vs-watchlist drift switch), tunable in SQL with no code change.
-- No new TABLES: the filters read cached bars + a live quote and gate ENTRIES;
-- the drift safety reuses positions / trade_history (already declared Phase 1).
-- Re-runnable: MERGE seeds only INSERT missing keys (never clobber a tuned one).
-- ============================================================

SET NOCOUNT ON;
GO

-- ----- seed Watchlist Filter params into app_config -----
-- (app_config is a 2-column key/value table; same MERGE style as Phases 1-4.)
-- Defaults are the CONSERVATIVE profile: only liquid, tight-spread, sanely
-- volatile names pass. Loosen per symbol class via UPDATE app_config.
MERGE dbo.app_config AS t
USING (VALUES
    -- Liquidity: sum(close * volume) over the last filters.vol_24h_bars closed
    -- 1h bars must clear this quote-currency (USD) floor. Thin books can't be
    -- exited cleanly. 10,000,000 = $10M / 24h.
    (N'filters.liquidity_enabled',      N'true'),
    (N'filters.min_24h_quote_volume',   N'10000000'),
    (N'filters.vol_24h_bars',           N'24'),     -- bars summed for "24h" at the 1h cadence

    -- Spread: live (ask-bid)/mid as a %. A wide spread is a guaranteed cost on
    -- both entry and exit. 0.20 = 0.20%.
    (N'filters.spread_enabled',         N'true'),
    (N'filters.max_spread_pct',         N'0.20'),

    -- Volatility: ATR as a % of price must sit INSIDE this band. Too dead
    -- (< min) = no edge, fees dominate; too wild (> max) = stops get run.
    (N'filters.volatility_enabled',     N'true'),
    (N'filters.min_atr_pct',            N'0.5'),
    (N'filters.max_atr_pct',            N'8.0'),

    -- Wallet-vs-watchlist drift: if a held symbol is deactivated/removed from
    -- the watchlist AND is underwater, force-exit it (exit_reason='manual')
    -- rather than leaving it to drift unmanaged (SUMMARY 10). In-profit
    -- deactivated positions are left to the Position Manager's normal exit.
    (N'filters.drift_force_exit_enabled', N'true')
) AS s (config_key, config_value)
ON (t.config_key = s.config_key)
WHEN NOT MATCHED BY TARGET THEN
    INSERT (config_key, config_value) VALUES (s.config_key, s.config_value);
GO

PRINT 'Phase 5 schema applied (filters.* config seed).';
GO
