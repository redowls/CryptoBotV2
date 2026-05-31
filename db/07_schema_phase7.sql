-- ============================================================
-- Phase 7 schema: Observability + Alerting
--   * obs.*   config seeds (heartbeat cadence + staleness threshold, log dir,
--             level, retention, the minute-past-the-hour the cycle fires).
--   * alert.* config seeds (master switch, channel, min severity, dedup window,
--             which api_credentials environment the Telegram bot creds live in).
--   * heartbeat — liveness row written by the daemon every obs.heartbeat_interval_secs
--                 AND once per trade cycle. The external monitor reads the latest
--                 row and ALERTS when it goes stale (a dead bot must not silently
--                 leave open positions unmanaged).
-- Telegram delivery reuses api_credentials: provider='telegram', the bot token in
-- encrypted_api_key and the chat id in encrypted_api_secret (same Fernet path as
-- the Alpaca keys). No plaintext token is ever stored (invariant #8).
-- Re-runnable: guarded CREATEs + MERGE seeds (never clobber a tuned value).
-- ============================================================

SET NOCOUNT ON;
GO

-- ----- seed Observability + Alerting params into app_config -----
-- (app_config is a 2-column key/value table; same MERGE style as Phases 1-6.)
MERGE dbo.app_config AS t
USING (VALUES
    -- Heartbeat cadence. The daemon writes a liveness row this often (independent
    -- of the hourly cycle) so a HUNG cycle is detectable between bars.
    (N'obs.heartbeat_interval_secs', N'300'),    -- write a heartbeat every 5 min
    -- Staleness threshold the external monitor alerts on. 3x the interval gives
    -- two missed beats of slack before crying wolf.
    (N'obs.heartbeat_stale_secs',    N'900'),    -- alert if no heartbeat for 15 min
    -- Minute past the hour the daemon fires the trade cycle. A small offset lets
    -- the just-closed 1h bar settle on Alpaca before we fetch it (closed-bars-only).
    (N'obs.cycle_minute',            N'1'),
    -- Structured JSON logs: directory (relative to CWD or absolute), level, and
    -- how many daily-rotated files to keep.
    (N'obs.log_dir',                 N'logs'),
    (N'obs.log_level',               N'INFO'),
    (N'obs.log_retention_days',      N'14'),

    -- Master alert switch. When false, send_alert() is a no-op (logs only).
    (N'alert.enabled',               N'true'),
    -- Delivery channel. 'telegram' (Bot API) is the wired channel; 'none' = log only.
    (N'alert.channel',               N'telegram'),
    -- Only alerts at or above this severity are delivered (info < warning < critical).
    (N'alert.min_severity',          N'warning'),
    -- Suppress an IDENTICAL alert (same severity+subject) within this window so a
    -- persistent fault doesn't spam. Dedup state is a small JSON file in log_dir.
    (N'alert.dedup_secs',            N'3600'),
    -- Which api_credentials environment holds the Telegram bot creds
    -- (provider='telegram'): encrypted_api_key=bot token, encrypted_api_secret=chat id.
    (N'alert.key_environment',       N'live')
) AS s (config_key, config_value)
ON (t.config_key = s.config_key)
WHEN NOT MATCHED BY TARGET THEN
    INSERT (config_key, config_value) VALUES (s.config_key, s.config_value);
GO

-- ----- heartbeat: liveness rows (daemon tick + per-cycle) -----
-- The monitor reads TOP 1 ... ORDER BY created_at_utc DESC and alerts if the age
-- exceeds obs.heartbeat_stale_secs. open_positions is carried so the alert can
-- say whether a stalled bot is leaving positions unmanaged (a critical case).
IF OBJECT_ID(N'dbo.heartbeat', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.heartbeat (
        id              INT IDENTITY(1,1) NOT NULL CONSTRAINT PK_heartbeat PRIMARY KEY,
        component       NVARCHAR(40)  NOT NULL,           -- 'daemon' | 'cycle' | 'monitor'
        status          NVARCHAR(20)  NOT NULL,           -- alive | ok | degraded | error
        detail          NVARCHAR(MAX) NULL,
        open_positions  INT           NULL,               -- open count at write time
        equity          DECIMAL(38,18) NULL,
        host            NVARCHAR(128) NULL,
        created_at_utc  DATETIME2(3)  NOT NULL CONSTRAINT DF_heartbeat_created DEFAULT (SYSUTCDATETIME()),
        CONSTRAINT CK_heartbeat_status CHECK (status IN (N'alive', N'ok', N'degraded', N'error'))
    );

    CREATE INDEX IX_heartbeat_created ON dbo.heartbeat (created_at_utc DESC);
    CREATE INDEX IX_heartbeat_component ON dbo.heartbeat (component, created_at_utc DESC);
END
GO

PRINT 'Phase 7 schema applied (obs.* + alert.* config seed + heartbeat table).';
GO
