-- ============================================================================
-- SkopaqTrader — System flags (kill switch)
-- ============================================================================
-- Applied via: supabase db push  (or Supabase dashboard SQL editor)
-- One row per flag, shared by every process (Railway daemon, API server,
-- CLI, MCP). `skopaq halt` writes key 'trading_halt'; while its value has
-- "halted": true, every BUY is rejected (skopaq/execution/kill_switch.py).
-- ============================================================================

CREATE TABLE IF NOT EXISTS system_flags (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Service-role access only (the app uses the service key).
ALTER TABLE system_flags ENABLE ROW LEVEL SECURITY;
