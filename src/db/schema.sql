-- grazie2api unified schema
-- Merged from codex2api-workers D1 migrations 0001-0019
-- 19 tables + all indexes

-- ============================================================
-- 0001: users, api_keys, daily_usage, request_audit
-- ============================================================

CREATE TABLE IF NOT EXISTS users (
  discord_user_id TEXT PRIMARY KEY,
  username TEXT NOT NULL DEFAULT '',
  global_name TEXT NOT NULL DEFAULT '',
  avatar_url TEXT NOT NULL DEFAULT '',
  roles_json TEXT NOT NULL DEFAULT '[]',
  tier TEXT NOT NULL DEFAULT 'default',
  creator_role_granted INTEGER NOT NULL DEFAULT 0,
  blink_eligible INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_login_at INTEGER NOT NULL DEFAULT 0,
  -- 0009_jb_api_key
  jb_api_key TEXT NOT NULL DEFAULT '',
  -- 0013_pool_quota
  pool_priority INTEGER NOT NULL DEFAULT 0,
  pool_revoke_count INTEGER NOT NULL DEFAULT 0,
  pool_banned INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
CREATE INDEX IF NOT EXISTS idx_users_updated_at ON users(updated_at DESC);

CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  owner_type TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  key_hash TEXT NOT NULL UNIQUE,
  key_prefix TEXT NOT NULL DEFAULT '',
  key_last4 TEXT NOT NULL DEFAULT '',
  tier TEXT NOT NULL DEFAULT 'default',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_used_at INTEGER NOT NULL DEFAULT 0,
  revoked_at INTEGER NOT NULL DEFAULT 0,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_api_keys_owner ON api_keys(owner_type, owner_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_enabled ON api_keys(enabled, revoked_at);

CREATE TABLE IF NOT EXISTS daily_usage (
  usage_date TEXT NOT NULL,
  api_key_id TEXT NOT NULL,
  owner_type TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  identity TEXT NOT NULL DEFAULT '',
  tier TEXT NOT NULL DEFAULT 'default',
  requests INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cached_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER NOT NULL DEFAULT 0,
  last_model TEXT NOT NULL DEFAULT '',
  last_channel_id TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL,
  PRIMARY KEY (usage_date, api_key_id)
);

CREATE INDEX IF NOT EXISTS idx_daily_usage_owner ON daily_usage(owner_id, usage_date);
CREATE INDEX IF NOT EXISTS idx_daily_usage_tier ON daily_usage(tier, usage_date);

CREATE TABLE IF NOT EXISTS request_audit (
  id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  api_key_id TEXT NOT NULL,
  owner_type TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  identity TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  channel_id TEXT NOT NULL DEFAULT '',
  status_code INTEGER NOT NULL DEFAULT 0,
  latency_ms INTEGER NOT NULL DEFAULT 0,
  request_kind TEXT NOT NULL DEFAULT 'chat.completions',
  stream INTEGER NOT NULL DEFAULT 0,
  error_code TEXT NOT NULL DEFAULT '',
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_request_audit_created_at ON request_audit(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_request_audit_owner ON request_audit(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_request_audit_channel ON request_audit(channel_id, created_at DESC);

-- ============================================================
-- 0002: sessions, oauth_states
-- ============================================================

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT NOT NULL,
  kind TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  username TEXT NOT NULL DEFAULT '',
  fresh_api_key TEXT NOT NULL DEFAULT '',
  discord_access_token TEXT NOT NULL DEFAULT '',
  discord_refresh_token TEXT NOT NULL DEFAULT '',
  discord_access_expires_at INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  PRIMARY KEY (token, kind)
);

CREATE INDEX IF NOT EXISTS idx_sessions_kind_expires_at ON sessions(kind, expires_at);
CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_id, kind);

CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY,
  return_to TEXT NOT NULL DEFAULT '/',
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oauth_states_expires_at ON oauth_states(expires_at);

-- ============================================================
-- 0003 + 0005: channel_keys (with upstream registry columns)
-- ============================================================

CREATE TABLE IF NOT EXISTS channel_keys (
  id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL,
  api_key TEXT NOT NULL,
  username TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  last_used_at INTEGER NOT NULL DEFAULT 0,
  fail_count INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  -- 0005_upstream_registry
  url TEXT NOT NULL DEFAULT '',
  models_json TEXT NOT NULL DEFAULT '[]',
  last_seen_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_channel_keys_channel ON channel_keys(channel_id, enabled);
CREATE INDEX IF NOT EXISTS idx_channel_keys_last_used ON channel_keys(channel_id, enabled, last_used_at ASC);
CREATE INDEX IF NOT EXISTS idx_channel_keys_url ON channel_keys(channel_id, url);

-- ============================================================
-- 0006: service_settings
-- ============================================================

CREATE TABLE IF NOT EXISTS service_settings (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_service_settings_updated_at ON service_settings(updated_at DESC);

-- ============================================================
-- 0007: announcements
-- ============================================================

CREATE TABLE IF NOT EXISTS announcements (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  level TEXT NOT NULL DEFAULT 'info',
  target TEXT NOT NULL DEFAULT 'user',
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_announcements_target_enabled ON announcements(target, enabled, updated_at DESC);

-- ============================================================
-- 0008 + 0009_channel_schedule: channels, channel_rate_limits
-- ============================================================

CREATE TABLE IF NOT EXISTS channels (
  channel_prefix TEXT PRIMARY KEY,
  display_name   TEXT NOT NULL DEFAULT '',
  description    TEXT NOT NULL DEFAULT '',
  enabled        INTEGER NOT NULL DEFAULT 1,
  sort_order     INTEGER NOT NULL DEFAULT 100,
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  -- 0009_channel_schedule
  auto_disable_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_channels_sort ON channels(sort_order ASC, channel_prefix ASC);

CREATE TABLE IF NOT EXISTS channel_rate_limits (
  channel_prefix TEXT NOT NULL,
  user_tier      TEXT NOT NULL,
  rpm            INTEGER NOT NULL DEFAULT 0,
  updated_at     INTEGER NOT NULL,
  PRIMARY KEY (channel_prefix, user_tier),
  FOREIGN KEY (channel_prefix) REFERENCES channels(channel_prefix) ON DELETE CASCADE
);

-- ============================================================
-- 0011 + 0014: card_pool (with group columns)
-- ============================================================

CREATE TABLE IF NOT EXISTS card_pool (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL DEFAULT 'efun',
  card_number TEXT NOT NULL,
  cvv TEXT NOT NULL,
  expiry TEXT NOT NULL,
  holder TEXT NOT NULL DEFAULT '',
  last4 TEXT NOT NULL,
  bin_prefix TEXT NOT NULL DEFAULT '',
  efun_cdk TEXT NOT NULL DEFAULT '',
  efun_api_key TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'available',
  card_created_at TEXT NOT NULL DEFAULT '',
  auto_cancel_at TEXT NOT NULL DEFAULT '',
  submitted_by TEXT NOT NULL DEFAULT '',
  claimed_by TEXT NOT NULL DEFAULT '',
  claimed_at INTEGER NOT NULL DEFAULT 0,
  bind_attempts INTEGER NOT NULL DEFAULT 0,
  max_bind_attempts INTEGER NOT NULL DEFAULT 5,
  window_start INTEGER NOT NULL DEFAULT 0,
  window_max_users INTEGER NOT NULL DEFAULT 3,
  notes TEXT NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  -- 0014_card_queue group columns
  max_group_size INTEGER NOT NULL DEFAULT 6,
  active_users INTEGER NOT NULL DEFAULT 0,
  fail_count INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cp_status ON card_pool(status);
CREATE INDEX IF NOT EXISTS idx_cp_claimed_by ON card_pool(claimed_by);
CREATE INDEX IF NOT EXISTS idx_cp_submitted_by ON card_pool(submitted_by);

-- ============================================================
-- 0011: card_claims
-- ============================================================

CREATE TABLE IF NOT EXISTS card_claims (
  id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL,
  user_id TEXT NOT NULL,
  claimed_at INTEGER NOT NULL,
  ready_at INTEGER NOT NULL DEFAULT 0,
  result TEXT NOT NULL DEFAULT 'pending',
  result_at INTEGER NOT NULL DEFAULT 0,
  result_note TEXT NOT NULL DEFAULT '',
  otp_matched TEXT NOT NULL DEFAULT '',
  otp_matched_at INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cc_card ON card_claims(card_id);
CREATE INDEX IF NOT EXISTS idx_cc_user ON card_claims(user_id);
CREATE INDEX IF NOT EXISTS idx_cc_result ON card_claims(result);
CREATE INDEX IF NOT EXISTS idx_cc_claimed_at ON card_claims(claimed_at);

-- ============================================================
-- 0011 + 0012: otp_records
-- ============================================================

CREATE TABLE IF NOT EXISTS otp_records (
  id TEXT PRIMARY KEY,
  card_id TEXT NOT NULL DEFAULT '',
  efun_cdk TEXT NOT NULL,
  last4 TEXT NOT NULL,
  otp_code TEXT NOT NULL,
  merchant TEXT NOT NULL DEFAULT '',
  amount TEXT NOT NULL DEFAULT '',
  efun_time TEXT NOT NULL DEFAULT '',
  poll_time TEXT NOT NULL DEFAULT '',
  assigned_to TEXT NOT NULL DEFAULT '',
  assigned_at INTEGER NOT NULL DEFAULT 0,
  claim_id TEXT NOT NULL DEFAULT '',
  flagged INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_otp_last4 ON otp_records(last4);
CREATE INDEX IF NOT EXISTS idx_otp_created ON otp_records(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_otp_assigned ON otp_records(assigned_to);
CREATE INDEX IF NOT EXISTS idx_otp_card ON otp_records(card_id);
CREATE INDEX IF NOT EXISTS idx_otp_cdk_code ON otp_records(efun_cdk, otp_code);

-- ============================================================
-- 0011: user_contributions (+ 0013 quota columns)
-- ============================================================

CREATE TABLE IF NOT EXISTS user_contributions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  contribution_type TEXT NOT NULL,
  jb_email TEXT NOT NULL DEFAULT '',
  jb_password TEXT NOT NULL DEFAULT '',
  card_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  -- 0013_pool_quota
  quota_used INTEGER NOT NULL DEFAULT 0,
  quota_total INTEGER NOT NULL DEFAULT 50,
  jb_api_token TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_uc_user ON user_contributions(user_id);
CREATE INDEX IF NOT EXISTS idx_uc_type ON user_contributions(contribution_type);

-- ============================================================
-- 0014: card_queue (+ 0015 bind_count)
-- ============================================================

CREATE TABLE IF NOT EXISTS card_queue (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'waiting',
  card_id TEXT NOT NULL DEFAULT '',
  joined_at INTEGER NOT NULL,
  assigned_at INTEGER NOT NULL DEFAULT 0,
  completed_at INTEGER NOT NULL DEFAULT 0,
  -- 0015_otp_bind_clicks
  bind_count INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_cq_status ON card_queue(status);
CREATE INDEX IF NOT EXISTS idx_cq_user ON card_queue(user_id);
CREATE INDEX IF NOT EXISTS idx_cq_card ON card_queue(card_id);

-- ============================================================
-- 0015: otp_bind_clicks
-- ============================================================

CREATE TABLE IF NOT EXISTS otp_bind_clicks (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  card_id TEXT NOT NULL,
  click_time INTEGER NOT NULL,
  matched_otp_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_obc_card ON otp_bind_clicks(card_id);
CREATE INDEX IF NOT EXISTS idx_obc_user ON otp_bind_clicks(user_id);
CREATE INDEX IF NOT EXISTS idx_obc_time ON otp_bind_clicks(click_time);

-- ============================================================
-- 0016: account_pool
-- ============================================================

CREATE TABLE IF NOT EXISTS account_pool (
  id TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  password TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'available',
  claimed_by TEXT NOT NULL DEFAULT '',
  claimed_at INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ap_status ON account_pool(status);
CREATE INDEX IF NOT EXISTS idx_ap_claimed ON account_pool(claimed_by);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ap_email ON account_pool(email);

-- ============================================================
-- 0017 + 0019: jb_credentials (with password column)
-- ============================================================

CREATE TABLE IF NOT EXISTS jb_credentials (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  jb_email TEXT NOT NULL,
  license_id TEXT NOT NULL DEFAULT '',
  refresh_token TEXT NOT NULL DEFAULT '',
  jwt TEXT NOT NULL DEFAULT '',
  jwt_expires_at INTEGER NOT NULL DEFAULT 0,
  quota_available INTEGER NOT NULL DEFAULT 1000000,
  quota_maximum INTEGER NOT NULL DEFAULT 1000000,
  quota_exhausted INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  donated INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL DEFAULT 0,
  -- 0019_credential_password
  jb_password TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_jbc_user ON jb_credentials(user_id);
CREATE INDEX IF NOT EXISTS idx_jbc_email ON jb_credentials(jb_email);
CREATE INDEX IF NOT EXISTS idx_jbc_donated ON jb_credentials(donated);

-- ============================================================
-- 0018: cdk_pool
-- ============================================================

CREATE TABLE IF NOT EXISTS cdk_pool (
  id TEXT PRIMARY KEY,
  cdk TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'available',
  card_id TEXT NOT NULL DEFAULT '',
  used_at INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cdk_status ON cdk_pool(status);
