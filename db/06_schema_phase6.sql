-- ============================================================
-- Phase 6 schema: AI Research Layer (default OFF)
--   * ai.* config seeds (model + per-module toggles + dampening floor),
--     tunable in SQL with no code change. The MASTER toggle is the existing
--     `ai_enabled` key (seeded false in Phase 1) — left untouched here.
--   * ai_suggestions          — advisory log: the confidence multiplier the AI
--                               applied to each TA signal (audit; never gates).
--   * ai_watchlist_suggestions — add/remove proposals land here for REVIEW only.
--                               The bot NEVER auto-applies them to dbo.watchlist.
-- Invariant #2: the AI can only SUBTRACT — its output is a multiplier in [0,1]
-- on TA confidence; it can veto/dampen but never raise it or create an entry.
-- Re-runnable: guarded CREATEs + MERGE seeds (never clobber a tuned value).
-- ============================================================

SET NOCOUNT ON;
GO

-- ----- seed AI Research Layer params into app_config -----
-- (app_config is a 2-column key/value table; same MERGE style as Phases 1-5.)
-- NOTE: `ai_enabled` (the master on/off) is seeded in Phase 1 and is the single
-- switch that controls whether ANY of this runs. Default false.
MERGE dbo.app_config AS t
USING (VALUES
    -- Model used for every advisory call (user's choice: Opus 4.8 — strongest
    -- reasoning). Drop to claude-sonnet-4-6 / claude-haiku-4-5 in SQL if the
    -- per-call cost of an hourly per-symbol advisory multiplier is too high.
    (N'ai.model',              N'claude-opus-4-8'),

    -- Per-module toggles (all under the ai_enabled master switch).
    (N'ai.sentiment_enabled',  N'true'),    -- news-sentiment multiplier
    (N'ai.context_enabled',    N'true'),    -- price-context multiplier
    (N'ai.watchlist_enabled',  N'false'),   -- watchlist add/remove suggestions (admin-run)

    -- min_multiplier: the FLOOR on how far the AI may dampen TA confidence.
    -- 0.0 = AI may veto entirely (multiplier -> 0). Raise toward 1.0 to cap the
    -- AI's influence (e.g. 0.5 = AI can at most halve confidence).
    (N'ai.min_multiplier',     N'0.0'),

    -- News fetch window + cap for the sentiment module (Alpaca news API).
    (N'ai.max_news',           N'10'),
    (N'ai.news_lookback_hours',N'24'),

    -- Anthropic call mechanics.
    (N'ai.max_tokens',         N'1024'),
    (N'ai.timeout_secs',       N'30'),

    -- Where the Anthropic key lives when stored encrypted in api_credentials
    -- (provider='anthropic'); the env var ANTHROPIC_API_KEY takes precedence.
    (N'ai.key_environment',    N'live')
) AS s (config_key, config_value)
ON (t.config_key = s.config_key)
WHEN NOT MATCHED BY TARGET THEN
    INSERT (config_key, config_value) VALUES (s.config_key, s.config_value);
GO

-- ----- ai_suggestions: advisory log of the AI confidence multiplier -----
-- One row per AI evaluation of a TA signal. Pure audit: records what the AI
-- saw (ta_confidence) and what it did (multipliers + adjusted). It NEVER gates
-- a trade by itself — the orchestrator applies the multiplier, this just logs.
IF OBJECT_ID(N'dbo.ai_suggestions', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ai_suggestions (
        id                    INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ai_suggestions PRIMARY KEY,
        symbol                NVARCHAR(30)  NOT NULL,
        bar_ts_utc            DATETIME2(3)  NULL,           -- TA bar this advised on
        ta_confidence         DECIMAL(5,2)  NULL,           -- TA confidence IN
        sentiment_multiplier  DECIMAL(6,4)  NULL,           -- [0,1] or NULL if module off
        context_multiplier    DECIMAL(6,4)  NULL,           -- [0,1] or NULL if module off
        combined_multiplier   DECIMAL(6,4)  NULL,           -- [0,1] applied to confidence
        adjusted_confidence   DECIMAL(5,2)  NULL,           -- ta_confidence * combined
        vetoed                BIT           NOT NULL CONSTRAINT DF_ai_suggestions_vetoed DEFAULT (0),
        model                 NVARCHAR(60)  NULL,
        rationale             NVARCHAR(MAX) NULL,
        created_at_utc        DATETIME2(3)  NOT NULL CONSTRAINT DF_ai_suggestions_created DEFAULT (SYSUTCDATETIME())
    );

    CREATE INDEX IX_ai_suggestions_symbol ON dbo.ai_suggestions (symbol, created_at_utc);
END
GO

-- ----- ai_watchlist_suggestions: add/remove proposals for REVIEW only -----
-- The AI watchlist module writes proposals here. They are NEVER auto-applied;
-- a human reviews and acts. status tracks that review lifecycle.
IF OBJECT_ID(N'dbo.ai_watchlist_suggestions', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.ai_watchlist_suggestions (
        id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_ai_watchlist_suggestions PRIMARY KEY,
        symbol          NVARCHAR(30)  NOT NULL,
        action          NVARCHAR(10)  NOT NULL,            -- 'add' | 'remove'
        rationale       NVARCHAR(MAX) NULL,
        model           NVARCHAR(60)  NULL,
        status          NVARCHAR(12)  NOT NULL CONSTRAINT DF_ai_wl_status DEFAULT (N'pending'),
        created_at_utc  DATETIME2(3)  NOT NULL CONSTRAINT DF_ai_wl_created DEFAULT (SYSUTCDATETIME()),
        reviewed_at_utc DATETIME2(3)  NULL,
        CONSTRAINT CK_ai_wl_action CHECK (action IN (N'add', N'remove')),
        CONSTRAINT CK_ai_wl_status CHECK (status IN (N'pending', N'applied', N'rejected'))
    );

    CREATE INDEX IX_ai_wl_status ON dbo.ai_watchlist_suggestions (status, created_at_utc);
END
GO

PRINT 'Phase 6 schema applied (ai.* config seed + ai_suggestions + ai_watchlist_suggestions).';
GO
