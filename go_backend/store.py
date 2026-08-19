"""SQLite persistence for accounts, billing, connectors, computers, and usage."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from .crypto_utils import hash_wrapper_key

SCHEMA_VERSION = 23

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id              TEXT PRIMARY KEY,
  name            TEXT,
  email           TEXT,
  api_key_hash    TEXT UNIQUE NOT NULL,
  subscription_id TEXT,
  tier            TEXT NOT NULL DEFAULT 'free',
  account_status  TEXT NOT NULL DEFAULT 'active',
  disabled_at     REAL,
  model_provider_override TEXT CHECK(model_provider_override IS NULL OR model_provider_override='opencode'),
  unlimited_usage INTEGER NOT NULL DEFAULT 0 CHECK(unlimited_usage IN (0,1)),
  created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS go_subscriptions (
  id               TEXT PRIMARY KEY,
  api_key_enc      BLOB NOT NULL,
  key_id           TEXT NOT NULL,          -- id usado en el Keychain (si aplica)
  key_version      INTEGER NOT NULL DEFAULT 1,
  label            TEXT,
  source           TEXT NOT NULL DEFAULT 'pool',   -- retired provider-key records
  status           TEXT NOT NULL DEFAULT 'available', -- available | assigned | revoked
  assigned_user_id TEXT,
  note             TEXT,
  created_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_events (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id              TEXT NOT NULL,
  subscription_id      TEXT,
  model                TEXT,
  endpoint             TEXT,
  input_tokens         INTEGER,
  output_tokens        INTEGER,
  cached_read_tokens   INTEGER,
  cached_write_tokens  INTEGER,
  estimated_cost_usd   REAL NOT NULL DEFAULT 0,
  run_id               TEXT,
  estimated_cost_microusd INTEGER NOT NULL DEFAULT 0,
  status               INTEGER,
  created_at           REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS account_identities (
  id             TEXT PRIMARY KEY,
  user_id        TEXT NOT NULL,
  provider       TEXT NOT NULL,
  subject        TEXT NOT NULL,
  email          TEXT NOT NULL,
  email_verified INTEGER NOT NULL DEFAULT 0,
  name           TEXT,
  picture        TEXT,
  created_at     REAL NOT NULL,
  updated_at     REAL NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  UNIQUE(provider, subject)
);
CREATE TABLE IF NOT EXISTS account_sessions (
  id                 TEXT PRIMARY KEY,
  account_id         TEXT NOT NULL,
  device_id          TEXT NOT NULL,
  access_token_hash  TEXT UNIQUE NOT NULL,
  refresh_token_hash TEXT UNIQUE NOT NULL,
  access_expires_at  REAL NOT NULL,
  refresh_expires_at REAL NOT NULL,
  revoked_at         REAL,
  created_at         REAL NOT NULL,
  FOREIGN KEY(account_id) REFERENCES account_identities(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS billing_customers (
  user_id            TEXT PRIMARY KEY,
  stripe_customer_id TEXT UNIQUE NOT NULL,
  created_at         REAL NOT NULL,
  updated_at         REAL NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS billing_subscriptions (
  stripe_subscription_id TEXT PRIMARY KEY,
  user_id                 TEXT NOT NULL,
  tier                    TEXT NOT NULL,
  stripe_price_id         TEXT,
  status                  TEXT NOT NULL,
  cancel_at_period_end    INTEGER NOT NULL DEFAULT 0,
  current_period_end      INTEGER,
  last_stripe_event_created INTEGER NOT NULL DEFAULT 0
    CHECK (last_stripe_event_created >= 0),
  updated_at              REAL NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS stripe_events (
  event_id             TEXT PRIMARY KEY,
  event_type           TEXT NOT NULL,
  stripe_event_created INTEGER NOT NULL DEFAULT 0 CHECK (stripe_event_created >= 0),
  processed_at         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS connector_credentials (
  user_id         TEXT NOT NULL,
  connector_id    TEXT NOT NULL,
  credentials_enc BLOB NOT NULL,
  key_id           TEXT NOT NULL,
  key_version      INTEGER NOT NULL DEFAULT 1,
  account_label    TEXT,
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL,
  PRIMARY KEY(user_id, connector_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS bot_computers (
  user_id        TEXT NOT NULL,
  bot_id         TEXT NOT NULL,
  provider       TEXT NOT NULL,
  provider_ref   TEXT,
  state          TEXT NOT NULL DEFAULT 'pulling',
  last_error     TEXT,
  created_at     REAL NOT NULL,
  updated_at     REAL NOT NULL,
  last_active_at REAL,
  PRIMARY KEY(user_id, bot_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS account_states (
  user_id                TEXT PRIMARY KEY,
  revision               INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
  state_json             TEXT NOT NULL,
  updated_by_device_hash TEXT NOT NULL,
  created_at             REAL NOT NULL,
  updated_at             REAL NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS account_bot_tombstones (
  user_id    TEXT NOT NULL,
  bot_id     TEXT NOT NULL,
  deleted_at REAL NOT NULL,
  PRIMARY KEY(user_id, bot_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS account_auth_attempts (
  id_hash                TEXT PRIMARY KEY,
  state_hash             TEXT UNIQUE NOT NULL,
  device_id_hash         TEXT NOT NULL,
  verifier_enc           BLOB NOT NULL,
  result_enc             BLOB,
  key_version            INTEGER NOT NULL DEFAULT 1,
  status                 TEXT NOT NULL DEFAULT 'pending',
  message                TEXT NOT NULL DEFAULT '',
  expires_at             REAL NOT NULL,
  consumed_at            REAL,
  created_at             REAL NOT NULL,
  updated_at             REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS connector_auth_attempts (
  id_hash                TEXT PRIMARY KEY,
  user_id                TEXT NOT NULL,
  connector_id           TEXT NOT NULL,
  driver                 TEXT NOT NULL,
  connected_account_id   TEXT,
  status                 TEXT NOT NULL DEFAULT 'pending',
  account_label          TEXT NOT NULL DEFAULT '',
  message                TEXT NOT NULL DEFAULT '',
  expires_at             REAL NOT NULL,
  next_poll_at           REAL NOT NULL DEFAULT 0,
  consumed_at            REAL,
  created_at             REAL NOT NULL,
  updated_at             REAL NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS rate_limit_buckets (
  scope_hash TEXT NOT NULL,
  window_start INTEGER NOT NULL,
  request_count INTEGER NOT NULL CHECK(request_count > 0),
  expires_at REAL NOT NULL,
  PRIMARY KEY(scope_hash, window_start)
);
CREATE TABLE IF NOT EXISTS account_identity_tokens (
  token_hash TEXT PRIMARY KEY,
  provider   TEXT NOT NULL,
  expires_at REAL NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS account_provider_credentials (
  account_id     TEXT NOT NULL,
  provider       TEXT NOT NULL,
  credential_enc BLOB NOT NULL,
  key_id         TEXT NOT NULL,
  key_version    INTEGER NOT NULL DEFAULT 1,
  created_at     REAL NOT NULL,
  updated_at     REAL NOT NULL,
  PRIMARY KEY(account_id, provider),
  FOREIGN KEY(account_id) REFERENCES account_identities(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS agent_runs (
  id                    TEXT PRIMARY KEY,
  user_id               TEXT NOT NULL,
  idempotency_key       TEXT NOT NULL,
  status                TEXT NOT NULL,
  harness               TEXT NOT NULL DEFAULT 'pi',
  model                 TEXT,
  browser               INTEGER NOT NULL DEFAULT 0,
  max_credit_milli      INTEGER NOT NULL,
  reserved_credit_milli INTEGER NOT NULL DEFAULT 0,
  charged_credit_milli  INTEGER NOT NULL DEFAULT 0,
  llm_cost_microusd     INTEGER NOT NULL DEFAULT 0,
  extra_cost_microusd   INTEGER NOT NULL DEFAULT 0,
  duration_seconds      REAL,
  error_code            TEXT,
  warnings_json         TEXT NOT NULL DEFAULT '[]',
  result_json           TEXT,
  created_at            REAL NOT NULL,
  started_at            REAL,
  finished_at           REAL,
  heartbeat_at          REAL,
  CHECK(max_credit_milli > 0),
  CHECK(reserved_credit_milli >= 0),
  CHECK(charged_credit_milli >= 0),
  CHECK(charged_credit_milli <= max_credit_milli),
  CHECK(llm_cost_microusd >= 0),
  CHECK(extra_cost_microusd >= 0),
  UNIQUE(user_id, idempotency_key),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS credit_grants (
  id               TEXT PRIMARY KEY,
  user_id          TEXT NOT NULL,
  source_type      TEXT NOT NULL,
  source_key       TEXT NOT NULL UNIQUE,
  original_milli   INTEGER NOT NULL CHECK(original_milli > 0),
  remaining_milli  INTEGER NOT NULL CHECK(remaining_milli >= 0),
  starts_at        REAL NOT NULL,
  expires_at       REAL,
  metadata_json    TEXT NOT NULL DEFAULT '{}',
  created_at       REAL NOT NULL,
  CHECK(remaining_milli <= original_milli),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS credit_reservations (
  id               TEXT PRIMARY KEY,
  user_id          TEXT NOT NULL,
  run_id           TEXT NOT NULL UNIQUE,
  reserved_milli   INTEGER NOT NULL CHECK(reserved_milli >= 0),
  charged_milli    INTEGER NOT NULL DEFAULT 0 CHECK(charged_milli >= 0),
  status           TEXT NOT NULL,
  expires_at       REAL NOT NULL,
  created_at       REAL NOT NULL,
  settled_at       REAL,
  CHECK(charged_milli <= reserved_milli),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS credit_reservation_allocations (
  reservation_id  TEXT NOT NULL,
  grant_id        TEXT NOT NULL,
  allocated_milli INTEGER NOT NULL CHECK(allocated_milli > 0),
  PRIMARY KEY(reservation_id, grant_id),
  FOREIGN KEY(reservation_id) REFERENCES credit_reservations(id) ON DELETE CASCADE,
  FOREIGN KEY(grant_id) REFERENCES credit_grants(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS credit_ledger (
  id                TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL,
  run_id            TEXT,
  grant_id          TEXT,
  reservation_id    TEXT,
  entry_type        TEXT NOT NULL,
  amount_milli      INTEGER NOT NULL,
  idempotency_key   TEXT NOT NULL UNIQUE,
  metadata_json     TEXT NOT NULL DEFAULT '{}',
  created_at        REAL NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS agent_run_tokens (
  token_hash  TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  run_id      TEXT NOT NULL UNIQUE,
  expires_at  REAL NOT NULL,
  revoked_at  REAL,
  created_at  REAL NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS desktop_runtime_devices (
  user_id             TEXT NOT NULL,
  device_id_hash      TEXT NOT NULL,
  platform            TEXT NOT NULL,
  app_version         TEXT NOT NULL DEFAULT '',
  capabilities_json   TEXT NOT NULL DEFAULT '{}',
  last_seen_at        REAL NOT NULL,
  lease_expires_at    REAL NOT NULL,
  created_at          REAL NOT NULL,
  updated_at          REAL NOT NULL,
  PRIMARY KEY(user_id, device_id_hash),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS desktop_runtime_jobs (
  id                    TEXT PRIMARY KEY,
  user_id               TEXT NOT NULL,
  run_id                TEXT NOT NULL UNIQUE,
  bot_id                TEXT,
  job_kind              TEXT NOT NULL CHECK(job_kind IN ('browser','computer')),
  status                TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','claimed','succeeded','failed','expired','cancelled')),
  payload_enc           BLOB NOT NULL,
  key_id                TEXT NOT NULL,
  key_version           INTEGER NOT NULL DEFAULT 1,
  claimed_device_hash   TEXT,
  claim_expires_at      REAL,
  result_json           TEXT,
  error_code            TEXT,
  error_message         TEXT,
  expires_at            REAL NOT NULL,
  created_at            REAL NOT NULL,
  updated_at            REAL NOT NULL,
  finished_at           REAL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS connector_operations (
  user_id       TEXT NOT NULL,
  run_id        TEXT NOT NULL,
  operation_id  TEXT NOT NULL,
  connector_id  TEXT NOT NULL,
  operation     TEXT NOT NULL,
  arguments_hash TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'running',
  result_json   TEXT,
  error_code    TEXT,
  created_at    REAL NOT NULL,
  updated_at    REAL NOT NULL,
  PRIMARY KEY(user_id, run_id, operation_id),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS pending_approvals (
  id             TEXT PRIMARY KEY,
  action_id      TEXT UNIQUE NOT NULL,
  user_id        TEXT NOT NULL,
  bot_id         TEXT NOT NULL,
  run_id         TEXT NOT NULL,
  target_type    TEXT NOT NULL CHECK(target_type IN ('connector','computer')),
  connector_id   TEXT NOT NULL,
  operation      TEXT NOT NULL,
  arguments_json TEXT NOT NULL,
  arguments_hash TEXT NOT NULL,
  human_summary  TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','approved','rejected','dispatched','succeeded','uncertain','expired')),
  expires_at     REAL NOT NULL,
  approved_at    REAL,
  consumed_at    REAL,
  created_at     REAL NOT NULL,
  updated_at     REAL NOT NULL,
  UNIQUE(user_id, run_id, target_type, connector_id, operation, arguments_hash),
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  FOREIGN KEY(run_id) REFERENCES agent_runs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS whatsapp_link_codes (
  code_hash   TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL,
  expires_at  REAL NOT NULL,
  consumed_at REAL,
  created_at  REAL NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS whatsapp_links (
  wa_user_id      TEXT PRIMARY KEY,
  user_id         TEXT UNIQUE NOT NULL,
  phone_number_id TEXT NOT NULL,
  display_name    TEXT NOT NULL DEFAULT '',
  active_bot_id   TEXT,
  created_at      REAL NOT NULL,
  updated_at      REAL NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS whatsapp_messages (
  message_id          TEXT PRIMARY KEY,
  user_id             TEXT,
  phone_number_id     TEXT NOT NULL,
  wa_user_id          TEXT NOT NULL,
  message_type        TEXT NOT NULL,
  text                 TEXT NOT NULL DEFAULT '',
  payload_json         TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'pending',
  attempts             INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
  next_attempt_at      REAL NOT NULL DEFAULT 0,
  result_text          TEXT NOT NULL DEFAULT '',
  outbound_message_id  TEXT,
  last_error           TEXT NOT NULL DEFAULT '',
  created_at           REAL NOT NULL,
  updated_at           REAL NOT NULL,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_subscription ON users(subscription_id);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_user_subscription
  ON users(subscription_id) WHERE subscription_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_subs_status ON go_subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_sub_time ON usage_events(subscription_id, created_at);
CREATE INDEX IF NOT EXISTS idx_account_identity_user ON account_identities(user_id);
CREATE INDEX IF NOT EXISTS idx_account_session_account ON account_sessions(account_id);
CREATE INDEX IF NOT EXISTS idx_account_session_refresh ON account_sessions(refresh_token_hash);
CREATE INDEX IF NOT EXISTS idx_billing_subscription_user ON billing_subscriptions(user_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_billing_subscription_status ON billing_subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_connector_credentials_user
  ON connector_credentials(user_id, updated_at);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_bot_computer_provider_ref
  ON bot_computers(provider, provider_ref) WHERE provider_ref IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bot_computers_user
  ON bot_computers(user_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_account_states_updated
  ON account_states(updated_at);
CREATE INDEX IF NOT EXISTS idx_account_bot_tombstones_deleted
  ON account_bot_tombstones(user_id, deleted_at);
CREATE INDEX IF NOT EXISTS idx_account_auth_attempts_expires
  ON account_auth_attempts(expires_at);
CREATE INDEX IF NOT EXISTS idx_connector_auth_attempts_user
  ON connector_auth_attempts(user_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_account_identity_tokens_expires
  ON account_identity_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_account_provider_credentials_account
  ON account_provider_credentials(account_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_user_status ON agent_runs(user_id, status);
CREATE INDEX IF NOT EXISTS idx_desktop_runtime_devices_online
  ON desktop_runtime_devices(user_id, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_desktop_runtime_jobs_pending
  ON desktop_runtime_jobs(user_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_desktop_runtime_jobs_expiry
  ON desktop_runtime_jobs(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_connector_operations_run
  ON connector_operations(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_run
  ON pending_approvals(user_id, run_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_pending_approvals_bot
  ON pending_approvals(user_id, bot_id, status, expires_at);
CREATE INDEX IF NOT EXISTS idx_credit_grants_user_expiry
  ON credit_grants(user_id, expires_at, created_at);
CREATE INDEX IF NOT EXISTS idx_credit_ledger_user_created
  ON credit_ledger(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_whatsapp_link_codes_user
  ON whatsapp_link_codes(user_id, expires_at);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_whatsapp_link_code_user
  ON whatsapp_link_codes(user_id);
CREATE INDEX IF NOT EXISTS idx_whatsapp_links_user
  ON whatsapp_links(user_id);
CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_pending
  ON whatsapp_messages(status, next_attempt_at, created_at);
CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_user
  ON whatsapp_messages(user_id, created_at);
"""


def _now() -> float:
    return time.time()


class ComputerLimitReached(RuntimeError):
    pass


class AccountStateConflict(RuntimeError):
    def __init__(self, current: dict):
        super().__init__("La cuenta cambió en otro dispositivo")
        self.current = current


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _hash_account_token(kind: str, token: str) -> str:
    return hashlib.sha256(f"account-{kind}|{token}".encode()).hexdigest()


def _hash_ephemeral(kind: str, value: str) -> str:
    return hashlib.sha256(f"agentgenia-{kind}|{value}".encode()).hexdigest()


def hash_agent_run_token(token: str) -> str:
    return _hash_ephemeral("run-token", token)


def hash_desktop_device_id(device_id: str) -> str:
    return _hash_ephemeral("desktop-device", device_id)


def hash_whatsapp_link_code(code: str) -> str:
    return _hash_ephemeral("whatsapp-link", code.strip().upper())


class Store:
    def __init__(self, db_path: str | Path):
        self._path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)
            version_row = self._conn.execute(
                "SELECT v FROM kv WHERE k='schema_version'"
            ).fetchone()
            previous_version = int(version_row["v"]) if version_row else 0
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(users)").fetchall()}
            if "tier" not in cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN tier TEXT NOT NULL DEFAULT 'free'")
                self._conn.commit()
            if "account_status" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN account_status TEXT NOT NULL DEFAULT 'active'"
                )
            if "disabled_at" not in cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN disabled_at REAL")
            if "model_provider_override" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN model_provider_override TEXT"
                )
            if "unlimited_usage" not in cols:
                self._conn.execute(
                    "ALTER TABLE users ADD COLUMN unlimited_usage INTEGER NOT NULL DEFAULT 0"
                )
            tier_column = next(
                row for row in self._conn.execute("PRAGMA table_info(users)") if row[1] == "tier"
            )
            if str(tier_column[4]).strip("'\"").lower() != "free":
                self._migrate_users_default_to_free()
            billing_columns = {
                row[1]
                for row in self._conn.execute("PRAGMA table_info(billing_subscriptions)")
            }
            if "last_stripe_event_created" not in billing_columns:
                self._conn.execute(
                    "ALTER TABLE billing_subscriptions ADD COLUMN "
                    "last_stripe_event_created INTEGER NOT NULL DEFAULT 0"
                )
            stripe_event_columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(stripe_events)")
            }
            if "stripe_event_created" not in stripe_event_columns:
                self._conn.execute(
                    "ALTER TABLE stripe_events ADD COLUMN "
                    "stripe_event_created INTEGER NOT NULL DEFAULT 0"
                )
            subscription_columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(go_subscriptions)")
            }
            if "key_version" not in subscription_columns:
                self._conn.execute(
                    "ALTER TABLE go_subscriptions ADD COLUMN key_version INTEGER NOT NULL DEFAULT 1"
                )
            connector_columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(connector_credentials)")
            }
            if "key_version" not in connector_columns:
                self._conn.execute(
                    "ALTER TABLE connector_credentials ADD COLUMN key_version INTEGER NOT NULL DEFAULT 1"
                )
            if previous_version < 10:
                usage_subscription = next(
                    row
                    for row in self._conn.execute("PRAGMA table_info(usage_events)")
                    if row[1] == "subscription_id"
                )
                if usage_subscription[3]:
                    self._migrate_usage_subscription_nullable()
                # Version 10 retires per-user provider credentials. Historical
                # ciphertext remains only as a revoked migration record.
                self._conn.execute("UPDATE users SET subscription_id=NULL")
                self._conn.execute(
                    "UPDATE go_subscriptions SET status='revoked',assigned_user_id=NULL"
                )
            if previous_version < 11:
                usage_columns = {
                    row[1] for row in self._conn.execute("PRAGMA table_info(usage_events)")
                }
                if "run_id" not in usage_columns:
                    self._conn.execute("ALTER TABLE usage_events ADD COLUMN run_id TEXT")
                if "estimated_cost_microusd" not in usage_columns:
                    self._conn.execute(
                        "ALTER TABLE usage_events ADD COLUMN "
                        "estimated_cost_microusd INTEGER NOT NULL DEFAULT 0"
                    )
                    self._conn.execute(
                        "UPDATE usage_events SET estimated_cost_microusd="
                        "CAST(ROUND(estimated_cost_usd * 1000000) AS INTEGER)"
                    )
            run_columns = {
                row[1] for row in self._conn.execute("PRAGMA table_info(agent_runs)")
            }
            if "result_json" not in run_columns:
                self._conn.execute("ALTER TABLE agent_runs ADD COLUMN result_json TEXT")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_whatsapp_messages_chat_order "
                "ON whatsapp_messages(phone_number_id,wa_user_id,status,created_at,message_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_usage_run "
                "ON usage_events(run_id, created_at)"
            )
            self._conn.execute(
                "INSERT INTO kv(k,v) VALUES('schema_version', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    def _migrate_usage_subscription_nullable(self) -> None:
        self._conn.execute("ALTER TABLE usage_events RENAME TO usage_events_legacy_provider")
        self._conn.execute(
            """CREATE TABLE usage_events (
              id                   INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id              TEXT NOT NULL,
              subscription_id      TEXT,
              model                TEXT,
              endpoint             TEXT,
              input_tokens         INTEGER,
              output_tokens        INTEGER,
              cached_read_tokens   INTEGER,
              cached_write_tokens  INTEGER,
              estimated_cost_usd   REAL NOT NULL DEFAULT 0,
              run_id               TEXT,
              estimated_cost_microusd INTEGER NOT NULL DEFAULT 0,
              status               INTEGER,
              created_at           REAL NOT NULL
            )"""
        )
        self._conn.execute(
            "INSERT INTO usage_events("
            "id,user_id,subscription_id,model,endpoint,input_tokens,output_tokens,"
            "cached_read_tokens,cached_write_tokens,estimated_cost_usd,status,created_at"
            ") SELECT id,user_id,subscription_id,model,endpoint,input_tokens,output_tokens,"
            "cached_read_tokens,cached_write_tokens,estimated_cost_usd,status,created_at "
            "FROM usage_events_legacy_provider"
        )
        self._conn.execute("DROP TABLE usage_events_legacy_provider")
        self._conn.execute(
            "CREATE INDEX idx_usage_user_time ON usage_events(user_id, created_at)"
        )
        self._conn.execute(
            "CREATE INDEX idx_usage_sub_time ON usage_events(subscription_id, created_at)"
        )

    def _migrate_users_default_to_free(self) -> None:
        """Reconstruye la tabla para cambiar el DEFAULT de bases ya existentes."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute("ALTER TABLE users RENAME TO users_legacy_default_basic")
            self._conn.execute(
                """CREATE TABLE users (
                  id              TEXT PRIMARY KEY,
                  name            TEXT,
                  email           TEXT,
                  api_key_hash    TEXT UNIQUE NOT NULL,
                  subscription_id TEXT,
                  tier            TEXT NOT NULL DEFAULT 'free',
                  account_status  TEXT NOT NULL DEFAULT 'active',
                  disabled_at     REAL,
                  model_provider_override TEXT,
                  unlimited_usage INTEGER NOT NULL DEFAULT 0,
                  created_at      REAL NOT NULL
                )"""
            )
            self._conn.execute(
                "INSERT INTO users(id, name, email, api_key_hash, subscription_id, tier,account_status,disabled_at,"
                "model_provider_override,unlimited_usage,created_at) "
                "SELECT id, name, email, api_key_hash, subscription_id, tier,account_status,disabled_at,"
                "model_provider_override,unlimited_usage,created_at "
                "FROM users_legacy_default_basic"
            )
            self._conn.execute("DROP TABLE users_legacy_default_basic")
            self._conn.execute(
                "CREATE INDEX idx_users_subscription ON users(subscription_id)"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX uniq_user_subscription ON users(subscription_id) "
                "WHERE subscription_id IS NOT NULL"
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---------- helpers ----------
    def _q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def health(self) -> dict:
        row = self._one("SELECT v FROM kv WHERE k='schema_version'")
        return {
            "ready": bool(row and int(row["v"]) == SCHEMA_VERSION),
            "schema_version": int(row["v"]) if row else None,
            "backend": "sqlite",
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- intentos efímeros persistentes ----------
    def prune_auth_attempts(self) -> None:
        cutoff = _now() - 60
        self._exec(
            "DELETE FROM account_auth_attempts WHERE expires_at<? OR "
            "(consumed_at IS NOT NULL AND consumed_at<?)",
            (cutoff, cutoff),
        )
        self._exec(
            "DELETE FROM connector_auth_attempts WHERE expires_at<? OR "
            "(consumed_at IS NOT NULL AND consumed_at<?)",
            (cutoff, cutoff),
        )
        self._exec("DELETE FROM rate_limit_buckets WHERE expires_at<?", (_now(),))

    def consume_rate_limit(self, scope: str, *, limit: int, window_seconds: int) -> bool:
        if limit < 1 or window_seconds < 1:
            raise ValueError("rate limit inválido")
        now = _now()
        window_start = int(now // window_seconds) * window_seconds
        scope_hash = hashlib.sha256(scope.encode("utf-8")).hexdigest()
        with self._lock:
            row = self._conn.execute(
                "INSERT INTO rate_limit_buckets(scope_hash,window_start,request_count,expires_at) "
                "VALUES(?,?,1,?) ON CONFLICT(scope_hash,window_start) DO UPDATE SET "
                "request_count=rate_limit_buckets.request_count+1,expires_at=excluded.expires_at "
                "RETURNING request_count",
                (scope_hash, window_start, window_start + window_seconds * 2),
            ).fetchone()
            self._conn.commit()
        return bool(row and int(row["request_count"]) <= limit)

    def create_account_auth_attempt(
        self,
        *,
        attempt_id: str,
        state: str,
        device_id: str,
        verifier_enc: bytes,
        key_version: int,
        expires_at: float,
    ) -> None:
        now = _now()
        self._exec(
            "INSERT INTO account_auth_attempts("
            "id_hash,state_hash,device_id_hash,verifier_enc,key_version,status,message,"
            "expires_at,created_at,updated_at) VALUES(?,?,?,?,?,'pending','',?,?,?)",
            (
                _hash_ephemeral("oauth-attempt", attempt_id),
                _hash_ephemeral("oauth-state", state),
                _hash_ephemeral("oauth-device", device_id),
                verifier_enc,
                key_version,
                expires_at,
                now,
                now,
            ),
        )

    def claim_account_auth_callback(self, state: str) -> dict | None:
        now = _now()
        state_hash = _hash_ephemeral("oauth-state", state)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM account_auth_attempts WHERE state_hash=?",
                    (state_hash,),
                ).fetchone()
                if row is None or row["status"] != "pending" or row["expires_at"] <= now:
                    self._conn.rollback()
                    return None
                changed = self._conn.execute(
                    "UPDATE account_auth_attempts SET status='exchanging',updated_at=? "
                    "WHERE id_hash=? AND status='pending'",
                    (now, row["id_hash"]),
                )
                if changed.rowcount != 1:
                    self._conn.rollback()
                    return None
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def complete_account_auth_attempt(
        self, *, id_hash: str, result_enc: bytes, key_version: int
    ) -> None:
        self._exec(
            "UPDATE account_auth_attempts SET status='complete',result_enc=?,verifier_enc=?,"
            "key_version=?,message='',updated_at=? WHERE id_hash=? AND status='exchanging'",
            (result_enc, b"", key_version, _now(), id_hash),
        )

    def fail_account_auth_attempt(self, *, id_hash: str, message: str) -> None:
        self._exec(
            "UPDATE account_auth_attempts SET status='error',message=?,verifier_enc=?,updated_at=? "
            "WHERE id_hash=? AND consumed_at IS NULL",
            (message[:500], b"", _now(), id_hash),
        )

    def consume_account_auth_attempt(self, attempt_id: str, device_id: str) -> dict | None:
        now = _now()
        attempt_hash = _hash_ephemeral("oauth-attempt", attempt_id)
        device_hash = _hash_ephemeral("oauth-device", device_id)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM account_auth_attempts WHERE id_hash=? AND device_id_hash=?",
                    (attempt_hash, device_hash),
                ).fetchone()
                if row is None or row["consumed_at"] is not None:
                    self._conn.rollback()
                    return None
                if row["expires_at"] <= now and row["status"] not in {"complete", "error"}:
                    self._conn.execute(
                        "UPDATE account_auth_attempts SET status='error',message=?,updated_at=? WHERE id_hash=?",
                        ("El inicio de sesión expiró. Inténtalo de nuevo.", now, attempt_hash),
                    )
                    row = self._conn.execute(
                        "SELECT * FROM account_auth_attempts WHERE id_hash=?", (attempt_hash,)
                    ).fetchone()
                if row["status"] in {"complete", "error"}:
                    changed = self._conn.execute(
                        "UPDATE account_auth_attempts SET consumed_at=?,updated_at=? "
                        "WHERE id_hash=? AND consumed_at IS NULL",
                        (now, now, attempt_hash),
                    )
                    if changed.rowcount != 1:
                        self._conn.rollback()
                        return None
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def get_account_auth_attempt(self, attempt_id: str, device_id: str) -> dict | None:
        """Read an attempt without consuming its one-time result."""
        row = self._one(
            "SELECT * FROM account_auth_attempts WHERE id_hash=? AND device_id_hash=? "
            "AND consumed_at IS NULL",
            (
                _hash_ephemeral("oauth-attempt", attempt_id),
                _hash_ephemeral("oauth-device", device_id),
            ),
        )
        return dict(row) if row else None

    def create_connector_auth_attempt(
        self,
        *,
        attempt_id: str,
        user_id: str,
        connector_id: str,
        driver: str,
        connected_account_id: str | None,
        expires_at: float,
    ) -> None:
        now = _now()
        self._exec(
            "INSERT INTO connector_auth_attempts("
            "id_hash,user_id,connector_id,driver,connected_account_id,status,expires_at,created_at,updated_at"
            ") VALUES(?,?,?,?,?,'pending',?,?,?)",
            (
                _hash_ephemeral("connector-attempt", attempt_id),
                user_id,
                connector_id,
                driver,
                connected_account_id,
                expires_at,
                now,
                now,
            ),
        )

    def get_connector_auth_attempt(self, attempt_id: str, user_id: str | None = None) -> dict | None:
        params: tuple = (_hash_ephemeral("connector-attempt", attempt_id),)
        sql = "SELECT * FROM connector_auth_attempts WHERE id_hash=?"
        if user_id is not None:
            sql += " AND user_id=?"
            params += (user_id,)
        row = self._one(sql, params)
        if row is None or row["expires_at"] <= _now() or row["consumed_at"] is not None:
            return None
        return dict(row)

    def has_pending_connector_auth_attempt(self, user_id: str) -> bool:
        row = self._one(
            "SELECT 1 AS pending FROM connector_auth_attempts WHERE user_id=? "
            "AND status='pending' AND consumed_at IS NULL AND expires_at>? LIMIT 1",
            (user_id, _now()),
        )
        return row is not None

    def consume_connected_auth_attempts(
        self, user_id: str, connector_ids: list[str]
    ) -> None:
        now = _now()
        for connector_id in dict.fromkeys(connector_ids):
            self._exec(
                "UPDATE connector_auth_attempts SET status='complete',consumed_at=?,updated_at=? "
                "WHERE user_id=? AND connector_id=? AND status='pending' "
                "AND consumed_at IS NULL AND expires_at>?",
                (now, now, user_id, connector_id, now),
            )

    def claim_connector_poll(
        self, attempt_id: str, user_id: str, interval: float, *, now: float | None = None
    ) -> tuple[dict | None, bool]:
        now = _now() if now is None else now
        attempt_hash = _hash_ephemeral("connector-attempt", attempt_id)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM connector_auth_attempts WHERE id_hash=? AND user_id=? "
                    "AND consumed_at IS NULL AND expires_at>?",
                    (attempt_hash, user_id, now),
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    return None, False
                should_poll = row["status"] == "pending" and float(row["next_poll_at"] or 0) <= now
                if should_poll:
                    self._conn.execute(
                        "UPDATE connector_auth_attempts SET next_poll_at=?,updated_at=? WHERE id_hash=?",
                        (now + interval, now, attempt_hash),
                    )
                self._conn.commit()
                return dict(row), should_poll
            except Exception:
                self._conn.rollback()
                raise

    def finish_connector_auth_attempt(
        self, *, attempt_id: str, status: str, account_label: str = "", message: str = ""
    ) -> None:
        if status not in {"complete", "error"}:
            raise ValueError("Estado terminal inválido")
        self._exec(
            "UPDATE connector_auth_attempts SET status=?,account_label=?,message=?,updated_at=? "
            "WHERE id_hash=? AND consumed_at IS NULL",
            (
                status,
                account_label[:160],
                message[:500],
                _now(),
                _hash_ephemeral("connector-attempt", attempt_id),
            ),
        )

    def consume_connector_auth_attempt(self, attempt_id: str, user_id: str) -> dict | None:
        now = _now()
        attempt_hash = _hash_ephemeral("connector-attempt", attempt_id)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM connector_auth_attempts WHERE id_hash=? AND user_id=? "
                    "AND consumed_at IS NULL AND expires_at>?",
                    (attempt_hash, user_id, now),
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    return None
                if row["status"] in {"complete", "error"}:
                    changed = self._conn.execute(
                        "UPDATE connector_auth_attempts SET consumed_at=?,updated_at=? "
                        "WHERE id_hash=? AND consumed_at IS NULL",
                        (now, now, attempt_hash),
                    )
                    if changed.rowcount != 1:
                        self._conn.rollback()
                        return None
                self._conn.commit()
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    # ---------- credenciales privadas de conectores ----------
    def upsert_connector_credentials(
        self,
        *,
        user_id: str,
        connector_id: str,
        credentials_enc: bytes,
        key_id: str,
        key_version: int = 1,
        account_label: str,
    ) -> None:
        now = _now()
        self._exec(
            "INSERT INTO connector_credentials("
            "user_id,connector_id,credentials_enc,key_id,key_version,account_label,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id,connector_id) DO UPDATE SET "
            "credentials_enc=excluded.credentials_enc,key_id=excluded.key_id,"
            "key_version=excluded.key_version,"
            "account_label=excluded.account_label,updated_at=excluded.updated_at",
            (
                user_id,
                connector_id,
                credentials_enc,
                key_id,
                key_version,
                account_label[:160],
                now,
                now,
            ),
        )

    def get_connector_credentials(self, user_id: str, connector_id: str) -> dict | None:
        row = self._one(
            "SELECT user_id,connector_id,credentials_enc,key_id,key_version,account_label,created_at,updated_at "
            "FROM connector_credentials WHERE user_id=? AND connector_id=?",
            (user_id, connector_id),
        )
        return dict(row) if row else None

    def delete_connector_credentials(self, user_id: str, connector_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM connector_credentials WHERE user_id=? AND connector_id=?",
                (user_id, connector_id),
            )
            self._conn.commit()
            return bool(cursor.rowcount)

    def update_connector_encryption(
        self, user_id: str, connector_id: str, credentials_enc: bytes, key_version: int
    ) -> None:
        self._exec(
            "UPDATE connector_credentials SET credentials_enc=?,key_version=?,updated_at=? "
            "WHERE user_id=? AND connector_id=?",
            (credentials_enc, key_version, _now(), user_id, connector_id),
        )

    # ---------- computadoras persistentes por bot ----------
    def claim_bot_computer(
        self,
        user_id: str,
        bot_id: str,
        provider: str,
        max_computers: int,
    ) -> dict:
        """Reserva de forma atómica la identidad remota de un bot."""
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM bot_computers WHERE user_id=? AND bot_id=?",
                    (user_id, bot_id),
                ).fetchone()
                if row is None:
                    count_row = self._conn.execute(
                        "SELECT COUNT(*) AS computer_count FROM bot_computers WHERE user_id=?",
                        (user_id,),
                    ).fetchone()
                    count = int(count_row["computer_count"])
                    if count >= max_computers:
                        raise ComputerLimitReached(
                            f"La cuenta alcanzó su límite de {max_computers} computadoras"
                        )
                    self._conn.execute(
                        "INSERT INTO bot_computers("
                        "user_id,bot_id,provider,provider_ref,state,last_error,created_at,updated_at,last_active_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?)",
                        (user_id, bot_id, provider, None, "pulling", "", now, now, None),
                    )
                    row = self._conn.execute(
                        "SELECT * FROM bot_computers WHERE user_id=? AND bot_id=?",
                        (user_id, bot_id),
                    ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        if row is None:
            raise RuntimeError("No se pudo reservar la computadora")
        result = dict(row)
        if result["provider"] != provider:
            raise RuntimeError("El bot ya pertenece a otro proveedor de computadoras")
        return result

    def get_bot_computer(self, user_id: str, bot_id: str) -> dict | None:
        row = self._one(
            "SELECT * FROM bot_computers WHERE user_id=? AND bot_id=?",
            (user_id, bot_id),
        )
        return dict(row) if row else None

    def list_bot_computers(self, user_id: str) -> list[dict]:
        return [
            dict(row)
            for row in self._q(
                "SELECT * FROM bot_computers WHERE user_id=? ORDER BY created_at", (user_id,)
            )
        ]

    def update_bot_computer(
        self,
        *,
        user_id: str,
        bot_id: str,
        provider_ref: str | None = None,
        state: str | None = None,
        last_error: str | None = None,
        touch: bool = False,
    ) -> None:
        assignments = ["updated_at=?"]
        params: list = [_now()]
        if provider_ref is not None:
            assignments.append("provider_ref=?")
            params.append(provider_ref)
        if state is not None:
            assignments.append("state=?")
            params.append(state)
        if last_error is not None:
            assignments.append("last_error=?")
            params.append(last_error[:500])
        if touch:
            assignments.append("last_active_at=?")
            params.append(_now())
        params.extend((user_id, bot_id))
        self._exec(
            f"UPDATE bot_computers SET {','.join(assignments)} WHERE user_id=? AND bot_id=?",
            tuple(params),
        )

    def delete_bot_computer(self, user_id: str, bot_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM bot_computers WHERE user_id=? AND bot_id=?",
                (user_id, bot_id),
            )
            self._conn.commit()
            return bool(cursor.rowcount)

    # ---------- estado de producto sincronizado por cuenta ----------
    def get_account_state(self, user_id: str) -> dict | None:
        row = self._one(
            "SELECT user_id,revision,state_json,created_at,updated_at "
            "FROM account_states WHERE user_id=?",
            (user_id,),
        )
        return dict(row) if row else None

    def save_account_state(
        self,
        *,
        user_id: str,
        base_revision: int,
        state_json: str,
        device_hash: str,
    ) -> dict:
        now = _now()
        payload = json.loads(state_json)
        if not isinstance(payload, dict):
            raise ValueError("state_json debe contener un objeto")
        deleted_ids = [
            item for item in payload.get("deletedBotIds", [])
            if isinstance(item, str) and item
        ]
        incoming_bots = payload.get("bots")
        incoming_bot_ids = [
            item.get("id") for item in incoming_bots
            if isinstance(incoming_bots, list)
            and isinstance(item, dict)
            and isinstance(item.get("id"), str)
        ] if isinstance(incoming_bots, list) else []
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT user_id,revision,state_json,created_at,updated_at "
                    "FROM account_states WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                current_revision = int(row["revision"]) if row else 0
                if current_revision != base_revision:
                    self._conn.rollback()
                    current = dict(row) if row else None
                    raise AccountStateConflict(current or {
                        "user_id": user_id,
                        "revision": 0,
                        "state_json": "",
                        "created_at": now,
                        "updated_at": now,
                    })
                for bot_id in deleted_ids:
                    self._conn.execute(
                        "INSERT INTO account_bot_tombstones(user_id,bot_id,deleted_at) "
                        "VALUES(?,?,?) ON CONFLICT(user_id,bot_id) DO NOTHING",
                        (user_id, bot_id, now),
                    )
                tombstoned: set[str] = set()
                if incoming_bot_ids:
                    placeholders = ",".join("?" for _item in incoming_bot_ids)
                    rows = self._conn.execute(
                        "SELECT bot_id FROM account_bot_tombstones WHERE user_id=? "
                        f"AND bot_id IN ({placeholders})",
                        (user_id, *incoming_bot_ids),
                    ).fetchall()
                    tombstoned = {str(item["bot_id"]) for item in rows}
                if tombstoned:
                    payload["bots"] = [
                        item for item in incoming_bots
                        if not isinstance(item, dict) or item.get("id") not in tombstoned
                    ]
                    active = payload.get("activeBotId")
                    if active in tombstoned:
                        payload["activeBotId"] = (
                            payload["bots"][0].get("id") if payload["bots"] else None
                        )
                    state_json = json.dumps(
                        payload, separators=(",", ":"), ensure_ascii=False
                    )
                if row is None:
                    self._conn.execute(
                        "INSERT INTO account_states(user_id,revision,state_json,updated_by_device_hash,created_at,updated_at) "
                        "VALUES(?,1,?,?,?,?)",
                        (user_id, state_json, device_hash, now, now),
                    )
                else:
                    self._conn.execute(
                        "UPDATE account_states SET revision=revision+1,state_json=?,"
                        "updated_by_device_hash=?,updated_at=? WHERE user_id=? AND revision=?",
                        (state_json, device_hash, now, user_id, base_revision),
                    )
                saved = self._conn.execute(
                    "SELECT user_id,revision,state_json,created_at,updated_at "
                    "FROM account_states WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                self._conn.commit()
            except AccountStateConflict:
                raise
            except Exception:
                self._conn.rollback()
                raise
        if saved is None:
            raise RuntimeError("No se pudo guardar el estado de la cuenta")
        return dict(saved)

    # ---------- canal oficial de WhatsApp ----------
    def create_whatsapp_link_code(
        self, *, user_id: str, code: str, expires_at: float
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "DELETE FROM whatsapp_link_codes WHERE expires_at<=?",
                    (now,),
                )
                self._conn.execute(
                    "INSERT INTO whatsapp_link_codes(code_hash,user_id,expires_at,created_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                    "code_hash=excluded.code_hash,expires_at=excluded.expires_at,"
                    "consumed_at=NULL,created_at=excluded.created_at",
                    (hash_whatsapp_link_code(code), user_id, expires_at, now),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def consume_whatsapp_link_code(
        self,
        *,
        code: str,
        wa_user_id: str,
        phone_number_id: str,
        display_name: str,
    ) -> dict | None:
        now = _now()
        code_hash = hash_whatsapp_link_code(code)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT c.*,u.account_status FROM whatsapp_link_codes c "
                    "JOIN users u ON u.id=c.user_id WHERE c.code_hash=?",
                    (code_hash,),
                ).fetchone()
                if (
                    row is None
                    or row["consumed_at"] is not None
                    or float(row["expires_at"]) <= now
                    or row["account_status"] != "active"
                ):
                    self._conn.rollback()
                    return None
                changed = self._conn.execute(
                    "UPDATE whatsapp_link_codes SET consumed_at=? "
                    "WHERE code_hash=? AND consumed_at IS NULL AND expires_at>?",
                    (now, code_hash, now),
                )
                if changed.rowcount != 1:
                    self._conn.rollback()
                    return None
                user_id = row["user_id"]
                # One personal WhatsApp identity per account in the MVP. A
                # fresh, authenticated code deliberately replaces an older link.
                self._conn.execute(
                    "DELETE FROM whatsapp_links WHERE user_id=? OR wa_user_id=?",
                    (user_id, wa_user_id),
                )
                self._conn.execute(
                    "INSERT INTO whatsapp_links(wa_user_id,user_id,phone_number_id,display_name,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (wa_user_id, user_id, phone_number_id, display_name[:120], now, now),
                )
                self._conn.commit()
                return {
                    "user_id": user_id,
                    "wa_user_id": wa_user_id,
                    "phone_number_id": phone_number_id,
                    "display_name": display_name[:120],
                    "created_at": now,
                    "updated_at": now,
                }
            except Exception:
                self._conn.rollback()
                raise

    def get_whatsapp_link_for_user(self, user_id: str) -> dict | None:
        row = self._one(
            "SELECT * FROM whatsapp_links WHERE user_id=?", (user_id,)
        )
        return dict(row) if row else None

    def get_whatsapp_link_for_sender(
        self, *, wa_user_id: str, phone_number_id: str
    ) -> dict | None:
        row = self._one(
            "SELECT l.*,u.account_status,u.tier,u.model_provider_override,u.subscription_id,"
            "u.unlimited_usage FROM whatsapp_links l JOIN users u ON u.id=l.user_id "
            "WHERE l.wa_user_id=? AND l.phone_number_id=? AND u.account_status='active'",
            (wa_user_id, phone_number_id),
        )
        return dict(row) if row else None

    def get_whatsapp_processing_context(
        self, *, wa_user_id: str, phone_number_id: str
    ) -> dict:
        """Load the linked account context needed by one inbound turn.

        SQLite keeps the straightforward reads. PostgreSQL overrides this
        method with one joined query so production does not pay three
        cross-region round trips before every WhatsApp response.
        """
        link = self.get_whatsapp_link_for_sender(
            wa_user_id=wa_user_id, phone_number_id=phone_number_id
        )
        if not link:
            return {"link": None, "user": None, "account_state": None}
        user_id = str(link["user_id"])
        return {
            "link": link,
            "user": self.get_user_by_id(user_id),
            "account_state": self.get_account_state(user_id),
        }

    def update_whatsapp_active_bot(self, *, user_id: str, bot_id: str | None) -> None:
        self._exec(
            "UPDATE whatsapp_links SET active_bot_id=?,updated_at=? WHERE user_id=?",
            (bot_id, _now(), user_id),
        )

    def delete_whatsapp_link(self, user_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM whatsapp_links WHERE user_id=?", (user_id,)
            )
            self._conn.execute(
                "DELETE FROM whatsapp_link_codes WHERE user_id=?", (user_id,)
            )
            self._conn.commit()
            return bool(cursor.rowcount)

    def enqueue_whatsapp_message(
        self,
        *,
        message_id: str,
        phone_number_id: str,
        wa_user_id: str,
        message_type: str,
        text: str,
        payload: dict,
    ) -> bool:
        return bool(self.enqueue_whatsapp_messages([{
            "message_id": message_id,
            "phone_number_id": phone_number_id,
            "wa_user_id": wa_user_id,
            "message_type": message_type,
            "text": text,
            "payload": payload,
        }]))

    def enqueue_whatsapp_messages(self, messages: list[dict]) -> int:
        """Persist one Meta webhook batch in a single transaction."""
        if not messages:
            return 0
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            inserted = 0
            link_cache: dict[tuple[str, str], str | None] = {}
            try:
                for message in messages:
                    phone_number_id = str(message["phone_number_id"])
                    wa_user_id = str(message["wa_user_id"])
                    key = (wa_user_id, phone_number_id)
                    if key not in link_cache:
                        link = self._conn.execute(
                            "SELECT l.user_id FROM whatsapp_links l "
                            "JOIN users u ON u.id=l.user_id "
                            "WHERE l.wa_user_id=? AND l.phone_number_id=? "
                            "AND u.account_status='active'",
                            key,
                        ).fetchone()
                        link_cache[key] = str(link["user_id"]) if link else None
                    cursor = self._conn.execute(
                        "INSERT INTO whatsapp_messages("
                        "message_id,user_id,phone_number_id,wa_user_id,message_type,text,payload_json,"
                        "status,next_attempt_at,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,'pending',?,?,?) "
                        "ON CONFLICT(message_id) DO NOTHING",
                        (
                            str(message["message_id"])[:300],
                            link_cache[key],
                            phone_number_id[:100],
                            wa_user_id[:100],
                            str(message["message_type"])[:40],
                            str(message.get("text") or "")[:20_000],
                            json.dumps(
                                message.get("payload") or {},
                                separators=(",", ":"),
                                ensure_ascii=False,
                            ),
                            now,
                            now,
                            now,
                        ),
                    )
                    inserted += max(0, int(cursor.rowcount or 0))
                self._conn.commit()
                return inserted
            except Exception:
                self._conn.rollback()
                raise

    def enqueue_and_claim_whatsapp_messages(
        self, messages: list[dict]
    ) -> tuple[int, dict | None]:
        """Persist a webhook batch and optionally return a pre-claimed item.

        SQLite is used for local development and tests, where an extra local
        query is effectively free. PostgreSQL overrides this method with a
        pipelined insert+claim that avoids a second cross-region round trip.
        """
        return self.enqueue_whatsapp_messages(messages), None

    def claim_whatsapp_message(self) -> dict | None:
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT m.* FROM whatsapp_messages m WHERE m.status='pending' "
                    "AND m.next_attempt_at<=? AND NOT EXISTS ("
                    "SELECT 1 FROM whatsapp_messages earlier "
                    "WHERE earlier.phone_number_id=m.phone_number_id "
                    "AND earlier.wa_user_id=m.wa_user_id "
                    "AND earlier.status IN ('pending','processing','sending') AND ("
                    "earlier.created_at<m.created_at OR "
                    "(earlier.created_at=m.created_at AND earlier.message_id<m.message_id))) "
                    "ORDER BY m.created_at,m.message_id LIMIT 1",
                    (now,),
                ).fetchone()
                if row is None:
                    self._conn.commit()
                    return None
                changed = self._conn.execute(
                    "UPDATE whatsapp_messages SET status='processing',attempts=attempts+1,updated_at=? "
                    "WHERE message_id=? AND status='pending'",
                    (now, row["message_id"]),
                )
                if changed.rowcount != 1:
                    self._conn.rollback()
                    return None
                claimed = self._conn.execute(
                    "SELECT * FROM whatsapp_messages WHERE message_id=?",
                    (row["message_id"],),
                ).fetchone()
                self._conn.commit()
                return dict(claimed) if claimed else None
            except Exception:
                self._conn.rollback()
                raise

    def complete_whatsapp_message(
        self,
        *,
        message_id: str,
        status: str,
        result_text: str = "",
        outbound_message_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        if status not in {"succeeded", "ignored"}:
            raise ValueError("estado final de WhatsApp inválido")
        self._exec(
            "UPDATE whatsapp_messages SET status=?,result_text=?,outbound_message_id=?,"
            "user_id=COALESCE(?,user_id),last_error='',updated_at=? WHERE message_id=?",
            (
                status,
                result_text[:20_000],
                outbound_message_id,
                user_id,
                _now(),
                message_id,
            ),
        )

    def prepare_whatsapp_outbound(
        self, *, message_id: str, result_text: str, user_id: str | None = None
    ) -> None:
        """Persist the final text and claim the one allowed delivery attempt.

        Meta does not expose a caller supplied idempotency key for message
        sends. Moving to ``sending`` before the network call makes an
        interrupted delivery explicit and prevents an automatic duplicate.
        """
        with self._lock:
            changed = self._conn.execute(
                "UPDATE whatsapp_messages SET status='sending',result_text=?,"
                "user_id=COALESCE(?,user_id),updated_at=? "
                "WHERE message_id=? AND status='processing'",
                (result_text[:20_000], user_id, _now(), message_id),
            )
            self._conn.commit()
        if changed.rowcount != 1:
            raise RuntimeError("whatsapp_delivery_already_claimed")

    def retry_whatsapp_message(
        self,
        *,
        message_id: str,
        error: str,
        maximum_attempts: int = 4,
        retryable: bool | None = None,
        delivery_uncertain: bool | None = None,
        retry_after_seconds: float | None = None,
    ) -> float | None:
        row = self._one(
            "SELECT attempts,status FROM whatsapp_messages WHERE message_id=?", (message_id,)
        )
        attempts = int(row["attempts"]) if row else maximum_attempts
        # Once delivery entered ``sending`` the provider outcome is uncertain.
        # Never resend automatically: that would create duplicate WhatsApp
        # replies after a timeout or process crash.
        inferred_uncertain = bool(row and row["status"] == "sending")
        uncertain_delivery = (
            inferred_uncertain if delivery_uncertain is None else bool(delivery_uncertain)
        )
        safe_to_retry = (
            not uncertain_delivery if retryable is None else bool(retryable)
        )
        terminal = uncertain_delivery or not safe_to_retry or attempts >= maximum_attempts
        delay = min(300.0, 1.0 * (2 ** max(0, attempts - 1)))
        if retry_after_seconds is not None:
            delay = max(delay, min(300.0, max(0.0, float(retry_after_seconds))))
        self._exec(
            "UPDATE whatsapp_messages SET status=?,next_attempt_at=?,last_error=?,updated_at=? "
            "WHERE message_id=?",
            (
                "failed" if terminal else "pending",
                _now() if terminal else _now() + delay,
                ("outbound_delivery_uncertain: " + error if uncertain_delivery else error)[:500],
                _now(),
                message_id,
            ),
        )
        return None if terminal else delay

    # ---------- usuarios ----------
    def create_user(self, api_key: str, name: str | None, email: str | None,
                    subscription_id: str | None = None, tier: str = "free") -> dict:
        user_id = new_id("usr")
        created = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO users(id, name, email, api_key_hash, subscription_id, tier, created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (user_id, name, email, hash_wrapper_key(api_key), subscription_id, tier, created),
            )
            if subscription_id:
                self._conn.execute(
                    "UPDATE go_subscriptions SET status='assigned', assigned_user_id=? WHERE id=?",
                    (user_id, subscription_id),
                )
            self._conn.commit()
        return self.get_user_by_id(user_id)  # type: ignore[return-value]

    def get_user_by_api_key(self, api_key: str) -> dict | None:
        row = self._one(
            "SELECT u.*,gs.id AS provider_subscription_id,"
            "gs.api_key_enc AS provider_api_key_enc,gs.key_id AS provider_key_id,"
            "gs.key_version AS provider_key_version,gs.status AS provider_subscription_status,"
            "gs.assigned_user_id AS provider_assigned_user_id "
            "FROM users u LEFT JOIN go_subscriptions gs ON gs.id=u.subscription_id "
            "WHERE u.api_key_hash=? AND u.account_status='active'",
            (hash_wrapper_key(api_key),),
        )
        return dict(row) if row else None

    def get_agent_user_by_api_key(self, api_key: str, bot_id: str | None) -> dict | None:
        """Authenticate and include only this bot's connector assignment.

        SQLite keeps the simple two-read implementation. Postgres overrides
        this with one query so the production agent path avoids a second
        cross-region round trip without transferring the full account state.
        """
        return self._agent_user_with_connectors(self.get_user_by_api_key(api_key), bot_id)

    def get_user_by_id(self, user_id: str) -> dict | None:
        row = self._one("SELECT * FROM users WHERE id=?", (user_id,))
        return dict(row) if row else None

    def get_or_create_google_account(
        self,
        *,
        subject: str,
        email: str,
        name: str | None,
        picture: str | None,
    ) -> dict:
        """Crea una identidad Google free o actualiza sus metadatos verificados.

        No enlaza por email con usuarios de `/v1/signup`: esas direcciones nunca
        fueron verificadas y hacerlo permitiría apropiarse de una cuenta ajena.
        La identidad estable es exclusivamente el `sub` emitido por Google.
        """
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT a.*,u.account_status FROM account_identities a "
                    "JOIN users u ON u.id=a.user_id "
                    "WHERE a.provider='google' AND a.subject=?",
                    (subject,),
                ).fetchone()
                if existing is None:
                    user_id = new_id("usr")
                    account_id = new_id("acct")
                    internal_api_key = secrets.token_urlsafe(48)
                    self._conn.execute(
                        "INSERT INTO users(id, name, email, api_key_hash, subscription_id, tier, created_at) "
                        "VALUES(?,?,?,?,NULL,'free',?)",
                        (user_id, name, email, hash_wrapper_key(internal_api_key), now),
                    )
                    self._conn.execute(
                        "INSERT INTO account_identities("
                        "id,user_id,provider,subject,email,email_verified,name,picture,created_at,updated_at"
                        ") VALUES(?,?,?,?,?,1,?,?,?,?)",
                        (account_id, user_id, "google", subject, email, name, picture, now, now),
                    )
                else:
                    if existing["account_status"] != "active":
                        raise PermissionError("La cuenta está deshabilitada")
                    account_id = existing["id"]
                    user_id = existing["user_id"]
                    self._conn.execute(
                        "UPDATE account_identities SET email=?, email_verified=1, name=?, picture=?, updated_at=? "
                        "WHERE id=?",
                        (email, name, picture, now, account_id),
                    )
                    self._conn.execute(
                        "UPDATE users SET name=?, email=? WHERE id=?",
                        (name, email, user_id),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        account = self.get_account_identity(account_id)
        if account is None:  # pragma: no cover - defensa ante corrupción externa
            raise RuntimeError("No se pudo leer la identidad Google recién guardada")
        return account

    def get_or_create_federated_account(
        self,
        *,
        provider: str,
        subject: str,
        email: str | None,
        name: str | None,
        picture: str | None,
        identity_token_hash: str,
        token_expires_at: float,
    ) -> dict:
        """Consume un token firmado una sola vez y crea/actualiza su identidad.

        No enlaza identidades por email. El identificador estable es siempre el
        ``(provider, subject)`` firmado por el proveedor.
        """
        if provider not in {"apple"}:
            raise ValueError("Proveedor de identidad no permitido")
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "DELETE FROM account_identity_tokens WHERE expires_at<=?", (now,)
                )
                consumed = self._conn.execute(
                    "INSERT INTO account_identity_tokens(token_hash,provider,expires_at,created_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(token_hash) DO NOTHING",
                    (identity_token_hash, provider, token_expires_at, now),
                )
                if consumed.rowcount != 1:
                    raise PermissionError("El token de identidad ya fue utilizado")
                existing = self._conn.execute(
                    "SELECT a.*,u.account_status FROM account_identities a "
                    "JOIN users u ON u.id=a.user_id WHERE a.provider=? AND a.subject=?",
                    (provider, subject),
                ).fetchone()
                if existing is None:
                    if not email or "@" not in email:
                        raise ValueError(
                            "Apple no devolvió el email inicial. Revoca Agent Genia en "
                            "Ajustes de Apple ID e intenta nuevamente."
                        )
                    user_id = new_id("usr")
                    account_id = new_id("acct")
                    internal_api_key = secrets.token_urlsafe(48)
                    self._conn.execute(
                        "INSERT INTO users(id,name,email,api_key_hash,subscription_id,tier,created_at) "
                        "VALUES(?,?,?,?,NULL,'free',?)",
                        (user_id, name, email, hash_wrapper_key(internal_api_key), now),
                    )
                    self._conn.execute(
                        "INSERT INTO account_identities("
                        "id,user_id,provider,subject,email,email_verified,name,picture,created_at,updated_at"
                        ") VALUES(?,?,?,?,?,1,?,?,?,?)",
                        (account_id, user_id, provider, subject, email, name, picture, now, now),
                    )
                else:
                    if existing["account_status"] != "active":
                        raise PermissionError("La cuenta está deshabilitada")
                    account_id = existing["id"]
                    user_id = existing["user_id"]
                    next_email = email or existing["email"]
                    next_name = name or existing["name"]
                    next_picture = picture or existing["picture"]
                    self._conn.execute(
                        "UPDATE account_identities SET email=?,email_verified=1,name=?,picture=?,updated_at=? "
                        "WHERE id=?",
                        (next_email, next_name, next_picture, now, account_id),
                    )
                    self._conn.execute(
                        "UPDATE users SET name=?,email=? WHERE id=?",
                        (next_name, next_email, user_id),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        account = self.get_account_identity(account_id)
        if account is None:  # pragma: no cover - defensa ante corrupción externa
            raise RuntimeError("No se pudo leer la identidad recién guardada")
        return account

    def put_account_provider_credential(
        self,
        *,
        account_id: str,
        provider: str,
        credential_enc: bytes,
        key_id: str,
        key_version: int,
    ) -> None:
        now = _now()
        self._exec(
            "INSERT INTO account_provider_credentials("
            "account_id,provider,credential_enc,key_id,key_version,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?) ON CONFLICT(account_id,provider) DO UPDATE SET "
            "credential_enc=excluded.credential_enc,key_id=excluded.key_id,"
            "key_version=excluded.key_version,updated_at=excluded.updated_at",
            (account_id, provider, credential_enc, key_id, key_version, now, now),
        )

    def get_account_provider_credential(self, user_id: str, provider: str) -> dict | None:
        row = self._one(
            "SELECT c.*,a.user_id FROM account_provider_credentials c "
            "JOIN account_identities a ON a.id=c.account_id "
            "WHERE a.user_id=? AND c.provider=?",
            (user_id, provider),
        )
        return dict(row) if row else None

    def get_account_identity(self, account_id: str) -> dict | None:
        row = self._one(
            "SELECT a.*, u.tier, u.subscription_id,u.account_status,u.disabled_at FROM account_identities a "
            "JOIN users u ON u.id=a.user_id WHERE a.id=?",
            (account_id,),
        )
        return dict(row) if row else None

    def create_account_session(
        self,
        *,
        account_id: str,
        device_id: str,
        access_token: str,
        refresh_token: str,
        access_expires_at: float,
        refresh_expires_at: float,
    ) -> None:
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO account_sessions("
                "id,account_id,device_id,access_token_hash,refresh_token_hash,"
                "access_expires_at,refresh_expires_at,created_at"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (
                    new_id("ses"),
                    account_id,
                    device_id,
                    _hash_account_token("access", access_token),
                    _hash_account_token("refresh", refresh_token),
                    access_expires_at,
                    refresh_expires_at,
                    now,
                ),
            )
            self._conn.commit()

    def get_user_by_access_token(self, access_token: str) -> dict | None:
        row = self._one(
            "SELECT u.*,s.access_expires_at AS authenticated_until,"
            "gs.id AS provider_subscription_id,"
            "gs.api_key_enc AS provider_api_key_enc,gs.key_id AS provider_key_id,"
            "gs.key_version AS provider_key_version,gs.status AS provider_subscription_status,"
            "gs.assigned_user_id AS provider_assigned_user_id "
            "FROM account_sessions s "
            "JOIN account_identities a ON a.id=s.account_id "
            "JOIN users u ON u.id=a.user_id "
            "LEFT JOIN go_subscriptions gs ON gs.id=u.subscription_id "
            "WHERE s.access_token_hash=? AND s.revoked_at IS NULL AND s.access_expires_at>? "
            "AND u.account_status='active'",
            (_hash_account_token("access", access_token), _now()),
        )
        return dict(row) if row else None

    def get_agent_user_by_access_token(
        self, access_token: str, bot_id: str | None
    ) -> dict | None:
        return self._agent_user_with_connectors(
            self.get_user_by_access_token(access_token), bot_id
        )

    def _agent_user_with_connectors(
        self, user: dict | None, bot_id: str | None
    ) -> dict | None:
        if user is None:
            return None
        connector_ids: list[str] = []
        if bot_id:
            row = self.get_account_state(str(user["id"]))
            try:
                state = json.loads(row.get("state_json") or "{}") if row else {}
            except (TypeError, json.JSONDecodeError):
                state = {}
            bots = state.get("bots") if isinstance(state, dict) else None
            if isinstance(bots, list):
                bot = next(
                    (
                        item for item in bots
                        if isinstance(item, dict) and item.get("id") == bot_id
                    ),
                    None,
                )
                if isinstance(bot, dict) and isinstance(bot.get("connectorIds"), list):
                    connector_ids = [
                        item for item in bot["connectorIds"] if isinstance(item, str)
                    ]
        return {
            **user,
            "assigned_connector_ids_json": json.dumps(
                connector_ids, separators=(",", ":")
            ),
        }

    def rotate_account_session(
        self,
        *,
        refresh_token: str,
        device_id: str,
        new_access_token: str,
        new_refresh_token: str,
        access_expires_at: float,
        refresh_expires_at: float,
    ) -> dict | None:
        """Rota refresh+access atómicamente y rechaza reuso o cambio de dispositivo."""
        now = _now()
        refresh_hash = _hash_account_token("refresh", refresh_token)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                session = self._conn.execute(
                    "SELECT s.* FROM account_sessions s "
                    "JOIN account_identities a ON a.id=s.account_id "
                    "JOIN users u ON u.id=a.user_id "
                    "WHERE s.refresh_token_hash=? AND s.device_id=? AND s.revoked_at IS NULL "
                    "AND s.refresh_expires_at>? AND u.account_status='active'",
                    (refresh_hash, device_id, now),
                ).fetchone()
                if session is None:
                    self._conn.rollback()
                    return None
                self._conn.execute(
                    "UPDATE account_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (now, session["id"]),
                )
                self._conn.execute(
                    "INSERT INTO account_sessions("
                    "id,account_id,device_id,access_token_hash,refresh_token_hash,"
                    "access_expires_at,refresh_expires_at,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (
                        new_id("ses"),
                        session["account_id"],
                        device_id,
                        _hash_account_token("access", new_access_token),
                        _hash_account_token("refresh", new_refresh_token),
                        access_expires_at,
                        refresh_expires_at,
                        now,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self.get_account_identity(session["account_id"])

    def revoke_account_session(self, access_token: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE account_sessions SET revoked_at=? "
                "WHERE access_token_hash=? AND revoked_at IS NULL",
                (_now(), _hash_account_token("access", access_token)),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def revoke_user_account(self, user_id: str) -> dict:
        """Deshabilita la cuenta e invalida credenciales locales atómicamente."""
        now = _now()
        revoked_hash = hash_wrapper_key("revoked|" + secrets.token_urlsafe(48))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                user = self._conn.execute(
                    "SELECT * FROM users WHERE id=?", (user_id,)
                ).fetchone()
                if user is None:
                    raise KeyError(user_id)
                if user["subscription_id"]:
                    self._conn.execute(
                        "UPDATE go_subscriptions SET status='revoked',assigned_user_id=NULL "
                        "WHERE id=?",
                        (user["subscription_id"],),
                    )
                sessions = self._conn.execute(
                    "UPDATE account_sessions SET revoked_at=? WHERE revoked_at IS NULL AND account_id IN ("
                    "SELECT id FROM account_identities WHERE user_id=?)",
                    (now, user_id),
                ).rowcount
                connectors = self._conn.execute(
                    "DELETE FROM connector_credentials WHERE user_id=?", (user_id,)
                ).rowcount
                self._conn.execute(
                    "DELETE FROM connector_auth_attempts WHERE user_id=?", (user_id,)
                )
                active_reservations = self._conn.execute(
                    "SELECT * FROM credit_reservations WHERE user_id=? AND status='active'",
                    (user_id,),
                ).fetchall()
                for reservation in active_reservations:
                    allocations = self._conn.execute(
                        "SELECT * FROM credit_reservation_allocations WHERE reservation_id=?",
                        (reservation["id"],),
                    ).fetchall()
                    for allocation in allocations:
                        self._conn.execute(
                            "UPDATE credit_grants SET remaining_milli=remaining_milli+? WHERE id=?",
                            (allocation["allocated_milli"], allocation["grant_id"]),
                        )
                    self._ledger_locked(
                        user_id=user_id, run_id=reservation["run_id"],
                        reservation_id=reservation["id"], entry_type="release",
                        amount_milli=int(reservation["reserved_milli"]),
                        idempotency_key=f"revoke:{reservation['id']}",
                        metadata={"reason": "account_revoked"}, now=now,
                    )
                    self._conn.execute(
                        "UPDATE credit_reservations SET status='released',settled_at=? WHERE id=?",
                        (now, reservation["id"]),
                    )
                self._conn.execute(
                    "UPDATE agent_runs SET status='cancelled',finished_at=?,error_code='account_revoked' "
                    "WHERE user_id=? AND status IN ('reserved','running')",
                    (now, user_id),
                )
                self._conn.execute(
                    "UPDATE agent_run_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                    (now, user_id),
                )
                self._conn.execute(
                    "UPDATE users SET tier='free',subscription_id=NULL,account_status='disabled',"
                    "disabled_at=?,api_key_hash=? WHERE id=?",
                    (now, revoked_hash, user_id),
                )
                self._conn.commit()
                return {
                    "user_id": user_id,
                    "sessions_revoked": int(sessions),
                    "connector_credentials_deleted": int(connectors),
                    "disabled_at": now,
                }
            except Exception:
                self._conn.rollback()
                raise

    def delete_user_account(self, user_id: str) -> dict:
        """Elimina definitivamente una cuenta y todos sus datos locales."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                user = self._conn.execute(
                    "SELECT * FROM users WHERE id=?", (user_id,)
                ).fetchone()
                if user is None:
                    raise KeyError(user_id)
                subscription_id = user["subscription_id"]
                usage = self._conn.execute(
                    "DELETE FROM usage_events WHERE user_id=?", (user_id,)
                ).rowcount
                self._conn.execute("DELETE FROM credit_ledger WHERE user_id=?", (user_id,))
                self._conn.execute(
                    "DELETE FROM credit_reservation_allocations WHERE reservation_id IN "
                    "(SELECT id FROM credit_reservations WHERE user_id=?)",
                    (user_id,),
                )
                self._conn.execute("DELETE FROM credit_reservations WHERE user_id=?", (user_id,))
                self._conn.execute("DELETE FROM agent_run_tokens WHERE user_id=?", (user_id,))
                self._conn.execute("DELETE FROM agent_runs WHERE user_id=?", (user_id,))
                self._conn.execute("DELETE FROM credit_grants WHERE user_id=?", (user_id,))
                self._conn.execute(
                    "DELETE FROM account_provider_credentials WHERE account_id IN "
                    "(SELECT id FROM account_identities WHERE user_id=?)",
                    (user_id,),
                )
                self._conn.execute(
                    "DELETE FROM account_sessions WHERE account_id IN "
                    "(SELECT id FROM account_identities WHERE user_id=?)",
                    (user_id,),
                )
                self._conn.execute("DELETE FROM connector_credentials WHERE user_id=?", (user_id,))
                self._conn.execute("DELETE FROM connector_auth_attempts WHERE user_id=?", (user_id,))
                self._conn.execute("DELETE FROM bot_computers WHERE user_id=?", (user_id,))
                self._conn.execute("DELETE FROM billing_subscriptions WHERE user_id=?", (user_id,))
                self._conn.execute("DELETE FROM billing_customers WHERE user_id=?", (user_id,))
                self._conn.execute("DELETE FROM account_identities WHERE user_id=?", (user_id,))
                self._conn.execute("DELETE FROM users WHERE id=?", (user_id,))
                if subscription_id:
                    self._conn.execute(
                        "UPDATE go_subscriptions SET status='revoked',assigned_user_id=NULL "
                        "WHERE id=?",
                        (subscription_id,),
                    )
                self._conn.commit()
                return {"user_id": user_id, "usage_events_deleted": int(usage)}
            except Exception:
                self._conn.rollback()
                raise

    def list_users(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM users ORDER BY created_at")]

    def update_user_subscription(self, user_id: str, subscription_id: str | None) -> None:
        with self._lock:
            old = self._one("SELECT subscription_id FROM users WHERE id=?", (user_id,))
            if old and old["subscription_id"]:
                self._conn.execute(
                    "UPDATE go_subscriptions SET status='available', assigned_user_id=NULL WHERE id=?",
                    (old["subscription_id"],),
                )
            self._conn.execute("UPDATE users SET subscription_id=? WHERE id=?", (subscription_id, user_id))
            if subscription_id:
                self._conn.execute(
                    "UPDATE go_subscriptions SET status='assigned', assigned_user_id=? WHERE id=?",
                    (user_id, subscription_id),
                )
            self._conn.commit()

    def configure_user_model_provider(
        self,
        user_id: str,
        *,
        provider: str | None,
        subscription_id: str | None = None,
        unlimited_usage: bool = False,
    ) -> dict:
        """Atomically configure a private provider override for one account.

        DeepSeek remains the implicit default (``provider=None``). OpenCode is
        available only when an encrypted, server-owned credential is assigned
        to the same user. This intentionally does not expose a public BYOK or
        provider-selection surface.
        """
        if provider not in {None, "opencode"}:
            raise ValueError("Proveedor de modelo inválido")
        if provider == "opencode" and not subscription_id:
            raise ValueError("OpenCode requiere una credencial asignada")
        if provider is None and subscription_id is not None:
            raise ValueError("DeepSeek no utiliza credenciales por usuario")

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                user = self._conn.execute(
                    "SELECT * FROM users WHERE id=?", (user_id,)
                ).fetchone()
                if user is None:
                    raise KeyError(user_id)
                old_subscription_id = user["subscription_id"]
                if old_subscription_id and old_subscription_id != subscription_id:
                    self._conn.execute(
                        "UPDATE go_subscriptions SET status='revoked',assigned_user_id=NULL "
                        "WHERE id=?",
                        (old_subscription_id,),
                    )
                if subscription_id:
                    credential = self._conn.execute(
                        "SELECT * FROM go_subscriptions WHERE id=?", (subscription_id,)
                    ).fetchone()
                    if credential is None:
                        raise KeyError(subscription_id)
                    assigned_to = credential["assigned_user_id"]
                    if assigned_to and assigned_to != user_id:
                        raise ValueError("La credencial ya pertenece a otra cuenta")
                    self._conn.execute(
                        "UPDATE go_subscriptions SET status='assigned',assigned_user_id=? "
                        "WHERE id=?",
                        (user_id, subscription_id),
                    )
                self._conn.execute(
                    "UPDATE users SET model_provider_override=?,subscription_id=?,"
                    "unlimited_usage=? WHERE id=?",
                    (provider, subscription_id, int(unlimited_usage), user_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        configured = self.get_user_by_id(user_id)
        if configured is None:  # pragma: no cover - defensive consistency check
            raise RuntimeError("No se pudo leer la cuenta configurada")
        return configured

    # ---------- suscripciones Go ----------
    def add_subscription(self, api_key_enc: bytes, key_id: str, label: str | None, source: str = "pool", user_id: str | None = None, sub_id: str | None = None, key_version: int = 1) -> dict:
        sub_id = sub_id or new_id("sub")
        created = _now()
        status = "assigned" if user_id else "available"
        with self._lock:
            self._conn.execute(
                "INSERT INTO go_subscriptions(id, api_key_enc, key_id,key_version,label,source,status,assigned_user_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (sub_id, api_key_enc, key_id, key_version, label, source, status, user_id, created),
            )
            if user_id:
                self._conn.execute("UPDATE users SET subscription_id=? WHERE id=?", (sub_id, user_id))
            self._conn.commit()
        return self.get_subscription(sub_id)  # type: ignore[return-value]

    def get_subscription(self, sub_id: str) -> dict | None:
        row = self._one("SELECT * FROM go_subscriptions WHERE id=?", (sub_id,))
        return dict(row) if row else None

    def update_subscription_encryption(
        self, sub_id: str, api_key_enc: bytes, key_version: int
    ) -> None:
        self._exec(
            "UPDATE go_subscriptions SET api_key_enc=?,key_version=? WHERE id=?",
            (api_key_enc, key_version, sub_id),
        )

    def available_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM go_subscriptions WHERE status='available'")
        return int(row["n"]) if row else 0

    def list_subscriptions(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM go_subscriptions ORDER BY created_at")]

    # ---------- créditos y ejecuciones ----------
    def _ledger_locked(
        self,
        *,
        user_id: str,
        entry_type: str,
        amount_milli: int,
        idempotency_key: str,
        run_id: str | None = None,
        grant_id: str | None = None,
        reservation_id: str | None = None,
        metadata: dict | None = None,
        now: float | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO credit_ledger("
            "id,user_id,run_id,grant_id,reservation_id,entry_type,amount_milli,"
            "idempotency_key,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(idempotency_key) DO NOTHING",
            (
                new_id("led"), user_id, run_id, grant_id, reservation_id,
                entry_type, amount_milli, idempotency_key,
                json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True),
                now if now is not None else _now(),
            ),
        )

    def _grant_credits_locked(
        self,
        *,
        user_id: str,
        amount_milli: int,
        source_type: str,
        source_key: str,
        starts_at: float,
        expires_at: float | None,
        metadata: dict | None,
        allow_increase: bool,
    ) -> dict:
        if amount_milli <= 0:
            raise ValueError("amount_milli debe ser positivo")
        existing = self._conn.execute(
            "SELECT * FROM credit_grants WHERE source_key=?", (source_key,)
        ).fetchone()
        now = _now()
        if existing:
            if existing["user_id"] != user_id:
                raise ValueError("source_key ya pertenece a otro usuario")
            current = int(existing["original_milli"])
            if allow_increase and amount_milli > current:
                delta = amount_milli - current
                self._conn.execute(
                    "UPDATE credit_grants SET original_milli=?,remaining_milli=remaining_milli+?,"
                    "expires_at=?,metadata_json=? WHERE id=?",
                    (
                        amount_milli, delta, expires_at,
                        json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True),
                        existing["id"],
                    ),
                )
                self._ledger_locked(
                    user_id=user_id,
                    grant_id=existing["id"],
                    entry_type="grant",
                    amount_milli=delta,
                    idempotency_key=f"grant-increase:{source_key}:{amount_milli}",
                    metadata={"source_type": source_type, "target_milli": amount_milli},
                    now=now,
                )
            row = self._conn.execute(
                "SELECT * FROM credit_grants WHERE id=?", (existing["id"],)
            ).fetchone()
            return dict(row)
        grant_id = new_id("grt")
        self._conn.execute(
            "INSERT INTO credit_grants("
            "id,user_id,source_type,source_key,original_milli,remaining_milli,"
            "starts_at,expires_at,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                grant_id, user_id, source_type, source_key, amount_milli, amount_milli,
                starts_at, expires_at,
                json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True), now,
            ),
        )
        self._ledger_locked(
            user_id=user_id,
            grant_id=grant_id,
            entry_type="grant",
            amount_milli=amount_milli,
            idempotency_key=f"grant:{source_key}",
            metadata={"source_type": source_type},
            now=now,
        )
        row = self._conn.execute("SELECT * FROM credit_grants WHERE id=?", (grant_id,)).fetchone()
        return dict(row)

    def grant_credits(
        self,
        *,
        user_id: str,
        amount_milli: int,
        source_type: str,
        source_key: str,
        expires_at: float | None = None,
        metadata: dict | None = None,
        starts_at: float | None = None,
        allow_increase: bool = False,
    ) -> dict:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if not self._conn.execute(
                    "SELECT 1 FROM users WHERE id=?", (user_id,)
                ).fetchone():
                    raise KeyError(user_id)
                result = self._grant_credits_locked(
                    user_id=user_id,
                    amount_milli=amount_milli,
                    source_type=source_type,
                    source_key=source_key,
                    starts_at=starts_at if starts_at is not None else _now(),
                    expires_at=expires_at,
                    metadata=metadata,
                    allow_increase=allow_increase,
                )
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def get_credit_grant_by_source(self, source_key: str) -> dict | None:
        row = self._one(
            "SELECT * FROM credit_grants WHERE source_key=?", (source_key,)
        )
        return dict(row) if row else None

    def _expire_stale_locked(self, now: float) -> int:
        stale = self._conn.execute(
            "SELECT * FROM credit_reservations WHERE status='active' AND expires_at<=?",
            (now,),
        ).fetchall()
        for reservation in stale:
            allocations = self._conn.execute(
                "SELECT * FROM credit_reservation_allocations WHERE reservation_id=?",
                (reservation["id"],),
            ).fetchall()
            for allocation in allocations:
                self._conn.execute(
                    "UPDATE credit_grants SET remaining_milli=remaining_milli+? WHERE id=?",
                    (allocation["allocated_milli"], allocation["grant_id"]),
                )
            self._ledger_locked(
                user_id=reservation["user_id"],
                run_id=reservation["run_id"],
                reservation_id=reservation["id"],
                entry_type="release",
                amount_milli=int(reservation["reserved_milli"]),
                idempotency_key=f"expire:{reservation['id']}",
                metadata={"reason": "reservation_ttl"},
                now=now,
            )
            self._conn.execute(
                "UPDATE credit_reservations SET status='expired',settled_at=? WHERE id=?",
                (now, reservation["id"]),
            )
            self._conn.execute(
                "UPDATE agent_runs SET status='expired',finished_at=?,error_code='reservation_expired' "
                "WHERE id=? AND status IN ('reserved','running')",
                (now, reservation["run_id"]),
            )
            self._conn.execute(
                "UPDATE agent_run_tokens SET revoked_at=? WHERE run_id=? AND revoked_at IS NULL",
                (now, reservation["run_id"]),
            )
        expired_grants = self._conn.execute(
            "SELECT * FROM credit_grants WHERE remaining_milli>0 AND expires_at IS NOT NULL "
            "AND expires_at<=?",
            (now,),
        ).fetchall()
        for grant in expired_grants:
            amount = int(grant["remaining_milli"])
            self._conn.execute(
                "UPDATE credit_grants SET remaining_milli=0 WHERE id=?", (grant["id"],)
            )
            self._ledger_locked(
                user_id=grant["user_id"], grant_id=grant["id"], entry_type="expire",
                amount_milli=-amount, idempotency_key=f"expire-grant:{grant['id']}", now=now,
            )
        return len(stale)

    def expire_stale_reservations(self, now: float | None = None) -> int:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                count = self._expire_stale_locked(now if now is not None else _now())
                self._conn.commit()
                return count
            except Exception:
                self._conn.rollback()
                raise

    def credit_summary(self, user_id: str, *, recent_limit: int = 20) -> dict:
        now = _now()
        self.expire_stale_reservations(now)
        available = self._one(
            "SELECT COALESCE(SUM(remaining_milli),0) AS n FROM credit_grants "
            "WHERE user_id=? AND starts_at<=? AND (expires_at IS NULL OR expires_at>?)",
            (user_id, now, now),
        )
        reserved = self._one(
            "SELECT COALESCE(SUM(reserved_milli),0) AS n FROM credit_reservations "
            "WHERE user_id=? AND status='active'",
            (user_id,),
        )
        recent = self._q(
            "SELECT entry_type,amount_milli,run_id,created_at FROM credit_ledger "
            "WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(0, min(recent_limit, 100))),
        )
        available_milli = int(available["n"] if available else 0)
        reserved_milli = int(reserved["n"] if reserved else 0)
        return {
            "available_milli": available_milli,
            "reserved_milli": reserved_milli,
            "total_milli": available_milli + reserved_milli,
            "recent_activity": [dict(row) for row in recent],
        }

    def create_agent_run(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        model: str,
        browser: bool,
        max_credit_milli: int,
        max_concurrent_runs: int,
        token_hash: str,
        token_expires_at: float,
        enforce: bool,
        five_hour_credit_milli: int | None = None,
        seven_day_credit_milli: int | None = None,
    ) -> dict:
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._expire_stale_locked(now)
                existing = self._conn.execute(
                    "SELECT * FROM agent_runs WHERE user_id=? AND idempotency_key=?",
                    (user_id, idempotency_key),
                ).fetchone()
                if existing:
                    if existing["status"] in {"failed", "cancelled", "expired", "budget_exhausted"}:
                        # A terminal failure must not poison the user's stable
                        # idempotency key for the whole retention window. Keep
                        # the old run auditable under a retired key and allow
                        # one controlled fresh execution.
                        self._conn.execute(
                            "UPDATE agent_runs SET idempotency_key=? WHERE id=?",
                            (f"{idempotency_key}:retired:{existing['id']}", existing["id"]),
                        )
                    else:
                        self._conn.rollback()
                        return {"duplicate": True, "run": dict(existing)}
                active = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM agent_runs WHERE user_id=? "
                    "AND status IN ('reserved','running')",
                    (user_id,),
                ).fetchone()
                if int(active["n"] or 0) >= max_concurrent_runs:
                    raise RuntimeError("credit_concurrency_limit")
                if enforce:
                    active_reserved = self._conn.execute(
                        "SELECT COALESCE(SUM(reserved_milli),0) AS n FROM credit_reservations "
                        "WHERE user_id=? AND status='active'",
                        (user_id,),
                    ).fetchone()
                    reserved_milli = int(active_reserved["n"] or 0)
                    for code, span, limit in (
                        ("credit_5h_limit", 5 * 3600, five_hour_credit_milli),
                        ("credit_7d_limit", 7 * 86400, seven_day_credit_milli),
                    ):
                        if limit is None:
                            continue
                        charged = self._conn.execute(
                            "SELECT COALESCE(SUM(charged_credit_milli),0) AS n FROM agent_runs "
                            "WHERE user_id=? AND created_at>=?",
                            (user_id, now - span),
                        ).fetchone()
                        if int(charged["n"] or 0) + reserved_milli + max_credit_milli > limit:
                            raise RuntimeError(code)
                grants = self._conn.execute(
                    "SELECT * FROM credit_grants WHERE user_id=? AND remaining_milli>0 "
                    "AND starts_at<=? AND (expires_at IS NULL OR expires_at>?) "
                    "ORDER BY CASE WHEN expires_at IS NULL THEN 1 ELSE 0 END,expires_at,"
                    "CASE source_type WHEN 'subscription' THEN 0 WHEN 'trial' THEN 1 "
                    "WHEN 'promotion' THEN 2 WHEN 'topup' THEN 3 ELSE 4 END,created_at",
                    (user_id, now, now),
                ).fetchall()
                available = sum(int(grant["remaining_milli"]) for grant in grants)
                if enforce and available < max_credit_milli:
                    raise RuntimeError("insufficient_credits")
                reserve_amount = max_credit_milli if enforce else 0
                run_id = new_id("run")
                reservation_id = new_id("rsv")
                self._conn.execute(
                    "INSERT INTO agent_runs("
                    "id,user_id,idempotency_key,status,harness,model,browser,max_credit_milli,"
                    "reserved_credit_milli,created_at,heartbeat_at) VALUES(?,?,?,'reserved','pi',?,?,?,?,?,?)",
                    (
                        run_id, user_id, idempotency_key, model, int(browser), max_credit_milli,
                        reserve_amount, now, now,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO credit_reservations("
                    "id,user_id,run_id,reserved_milli,status,expires_at,created_at) "
                    "VALUES(?,?,?,?, 'active',?,?)",
                    (reservation_id, user_id, run_id, reserve_amount, token_expires_at, now),
                )
                remaining = reserve_amount
                for grant in grants:
                    if remaining <= 0:
                        break
                    allocated = min(remaining, int(grant["remaining_milli"]))
                    self._conn.execute(
                        "UPDATE credit_grants SET remaining_milli=remaining_milli-? WHERE id=?",
                        (allocated, grant["id"]),
                    )
                    self._conn.execute(
                        "INSERT INTO credit_reservation_allocations("
                        "reservation_id,grant_id,allocated_milli) VALUES(?,?,?)",
                        (reservation_id, grant["id"], allocated),
                    )
                    remaining -= allocated
                if reserve_amount:
                    self._ledger_locked(
                        user_id=user_id, run_id=run_id, reservation_id=reservation_id,
                        entry_type="reserve", amount_milli=-reserve_amount,
                        idempotency_key=f"reserve:{reservation_id}", now=now,
                    )
                self._conn.execute(
                    "INSERT INTO agent_run_tokens(token_hash,user_id,run_id,expires_at,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (token_hash, user_id, run_id, token_expires_at, now),
                )
                self._conn.commit()
                row = self._one("SELECT * FROM agent_runs WHERE id=?", (run_id,))
                return {"duplicate": False, "run": dict(row)}
            except Exception:
                self._conn.rollback()
                raise

    def create_unmetered_agent_run(
        self,
        *,
        user_id: str,
        idempotency_key: str,
        model: str,
        browser: bool,
        max_credit_milli: int,
        max_concurrent_runs: int,
        token_hash: str,
        token_expires_at: float,
    ) -> dict:
        """Create an unlimited/internal run without credit-ledger work.

        SQLite keeps the regular implementation as the compatibility path.
        Postgres overrides this with one server-side statement so production
        does not pay a network round trip for every ledger operation.
        """
        return self.create_agent_run(
            user_id=user_id,
            idempotency_key=idempotency_key,
            model=model,
            browser=browser,
            max_credit_milli=max_credit_milli,
            max_concurrent_runs=max_concurrent_runs,
            token_hash=token_hash,
            token_expires_at=token_expires_at,
            enforce=False,
        )

    def mark_agent_run_running(self, run_id: str) -> None:
        now = _now()
        self._exec(
            "UPDATE agent_runs SET status='running',started_at=?,heartbeat_at=? "
            "WHERE id=? AND status='reserved'",
            (now, now, run_id),
        )

    def get_agent_run(self, run_id: str) -> dict | None:
        row = self._one("SELECT * FROM agent_runs WHERE id=?", (run_id,))
        return dict(row) if row else None

    def get_agent_run_for_user(self, run_id: str, user_id: str) -> dict | None:
        row = self._one(
            "SELECT * FROM agent_runs WHERE id=? AND user_id=?", (run_id, user_id)
        )
        return dict(row) if row else None

    def get_agent_run_by_idempotency(self, user_id: str, idempotency_key: str) -> dict | None:
        row = self._one(
            "SELECT * FROM agent_runs WHERE user_id=? AND idempotency_key=?",
            (user_id, idempotency_key),
        )
        return dict(row) if row else None

    def save_agent_run_result(self, run_id: str, payload: dict) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 1_000_000:
            raise ValueError("El resultado durable excede 1 MB")
        self._exec(
            "UPDATE agent_runs SET result_json=? WHERE id=? "
            "AND status IN ('reserved','running','succeeded')",
            (encoded, run_id),
        )

    # ---------- desktop runtime relay ----------
    def upsert_desktop_runtime_device(
        self,
        *,
        user_id: str,
        device_id_hash: str,
        platform: str,
        app_version: str,
        capabilities: dict,
        lease_seconds: int = 45,
    ) -> dict:
        now = _now()
        lease_expires_at = now + max(15, min(int(lease_seconds), 120))
        encoded = json.dumps(capabilities, separators=(",", ":"), sort_keys=True)
        self._exec(
            "INSERT INTO desktop_runtime_devices("
            "user_id,device_id_hash,platform,app_version,capabilities_json,"
            "last_seen_at,lease_expires_at,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(user_id,device_id_hash) DO UPDATE SET "
            "platform=excluded.platform,app_version=excluded.app_version,"
            "capabilities_json=excluded.capabilities_json,"
            "last_seen_at=excluded.last_seen_at,lease_expires_at=excluded.lease_expires_at,"
            "updated_at=excluded.updated_at",
            (
                user_id,
                device_id_hash,
                platform,
                app_version,
                encoded,
                now,
                lease_expires_at,
                now,
                now,
            ),
        )
        return {
            "online": True,
            "lease_expires_at": lease_expires_at,
            "capabilities": capabilities,
        }

    def desktop_runtime_available(self, user_id: str, capability: str) -> bool:
        rows = self._q(
            "SELECT capabilities_json FROM desktop_runtime_devices "
            "WHERE user_id=? AND lease_expires_at>? ORDER BY last_seen_at DESC",
            (user_id, _now()),
        )
        for row in rows:
            try:
                capabilities = json.loads(row["capabilities_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(capabilities, dict) and capabilities.get(capability) is True:
                return True
        return False

    def create_desktop_runtime_job(
        self,
        *,
        user_id: str,
        run_id: str,
        bot_id: str | None,
        job_kind: str,
        payload_enc: bytes,
        key_id: str,
        key_version: int,
        expires_at: float,
    ) -> dict:
        if job_kind not in {"browser", "computer"}:
            raise ValueError("job_kind inválido")
        now = _now()
        job_id = new_id("djob")
        self._exec(
            "INSERT INTO desktop_runtime_jobs("
            "id,user_id,run_id,bot_id,job_kind,status,payload_enc,key_id,key_version,"
            "expires_at,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?,?,?,?,?)",
            (
                job_id,
                user_id,
                run_id,
                bot_id,
                job_kind,
                payload_enc,
                key_id,
                key_version,
                expires_at,
                now,
                now,
            ),
        )
        row = self._one("SELECT * FROM desktop_runtime_jobs WHERE id=?", (job_id,))
        if row is None:
            raise RuntimeError("No se pudo crear el trabajo local")
        return dict(row)

    def claim_desktop_runtime_job(
        self,
        *,
        user_id: str,
        device_id_hash: str,
        capabilities: dict,
        lease_seconds: int = 1_900,
    ) -> dict | None:
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                device = self._conn.execute(
                    "SELECT capabilities_json FROM desktop_runtime_devices "
                    "WHERE user_id=? AND device_id_hash=? AND lease_expires_at>?",
                    (user_id, device_id_hash, now),
                ).fetchone()
                if device is None:
                    self._conn.rollback()
                    return None
                try:
                    registered = json.loads(device["capabilities_json"])
                except (TypeError, json.JSONDecodeError):
                    registered = {}
                supported = tuple(
                    kind for kind in ("computer", "browser")
                    if capabilities.get(kind) is True
                    and isinstance(registered, dict)
                    and registered.get(kind) is True
                )
                if not supported:
                    self._conn.rollback()
                    return None
                placeholders = ",".join("?" for _ in supported)
                # A desktop can disappear while it owns a job (sleep, crash or
                # network change). Make the claim recoverable after its lease
                # instead of leaving the mobile/WhatsApp request stuck forever.
                self._conn.execute(
                    "UPDATE desktop_runtime_jobs SET status='pending',"
                    "claimed_device_hash=NULL,claim_expires_at=NULL,updated_at=? "
                    "WHERE user_id=? AND status='claimed' AND claim_expires_at<=? "
                    "AND expires_at>?",
                    (now, user_id, now, now),
                )
                rows = self._conn.execute(
                    "SELECT * FROM desktop_runtime_jobs WHERE user_id=? "
                    "AND status='pending' AND expires_at>? "
                    f"AND job_kind IN ({placeholders}) ORDER BY created_at ASC LIMIT 8",
                    (user_id, now, *supported),
                ).fetchall()
                for candidate in rows:
                    job_id = candidate["id"]
                    cursor = self._conn.execute(
                        "UPDATE desktop_runtime_jobs SET status='claimed',"
                        "claimed_device_hash=?,claim_expires_at=?,updated_at=? "
                        "WHERE id=? AND status='pending'",
                        (
                            device_id_hash,
                            min(
                                float(candidate["expires_at"]),
                                now + max(30, min(int(lease_seconds), 3_600)),
                            ),
                            now,
                            job_id,
                        ),
                    )
                    if cursor.rowcount == 1:
                        claimed = self._conn.execute(
                            "SELECT * FROM desktop_runtime_jobs WHERE id=?", (job_id,)
                        ).fetchone()
                        self._conn.commit()
                        return dict(claimed) if claimed else None
                self._conn.commit()
                return None
            except Exception:
                self._conn.rollback()
                raise

    def get_desktop_runtime_job(self, job_id: str, user_id: str) -> dict | None:
        row = self._one(
            "SELECT * FROM desktop_runtime_jobs WHERE id=? AND user_id=?",
            (job_id, user_id),
        )
        return dict(row) if row else None

    def finish_desktop_runtime_job(
        self,
        *,
        job_id: str,
        user_id: str,
        device_id_hash: str,
        status: str,
        result: dict | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        if status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("status de trabajo local inválido")
        encoded = None
        if result is not None:
            encoded = json.dumps(
                result, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
            if len(encoded.encode("utf-8")) > 1_000_000:
                raise ValueError("El resultado local excede 1 MB")
        now = _now()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE desktop_runtime_jobs SET status=?,result_json=?,error_code=?,"
                "error_message=?,finished_at=?,updated_at=? WHERE id=? AND user_id=? "
                "AND claimed_device_hash=? AND status='claimed'",
                (
                    status,
                    encoded,
                    error_code,
                    error_message,
                    now,
                    now,
                    job_id,
                    user_id,
                    device_id_hash,
                ),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def expire_desktop_runtime_job(self, job_id: str, user_id: str) -> None:
        now = _now()
        self._exec(
            "UPDATE desktop_runtime_jobs SET status='expired',finished_at=?,updated_at=? "
            "WHERE id=? AND user_id=? AND status IN ('pending','claimed')",
            (now, now, job_id, user_id),
        )

    def begin_connector_operation(
        self,
        *,
        user_id: str,
        run_id: str,
        operation_id: str,
        connector_id: str,
        operation: str,
        arguments_hash: str,
    ) -> dict:
        """Atomically reserve a provider call or replay its durable result.

        A row left in ``running`` after a crash is deliberately treated as
        uncertain. Replaying it could duplicate an email, calendar event, or
        payment even though the first provider request actually succeeded.
        """
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._conn.execute(
                    "SELECT * FROM connector_operations "
                    "WHERE user_id=? AND run_id=? AND operation_id=?",
                    (user_id, run_id, operation_id),
                ).fetchone()
                if existing is not None:
                    row = dict(existing)
                    same_call = (
                        row["connector_id"] == connector_id
                        and row["operation"] == operation
                        and row["arguments_hash"] == arguments_hash
                    )
                    self._conn.rollback()
                    if not same_call:
                        return {"status": "conflict"}
                    if row["status"] == "succeeded" and row.get("result_json"):
                        try:
                            result = json.loads(row["result_json"])
                        except (TypeError, json.JSONDecodeError):
                            return {"status": "uncertain"}
                        return {"status": "replay", "result": result}
                    return {"status": "uncertain"}
                self._conn.execute(
                    "INSERT INTO connector_operations("
                    "user_id,run_id,operation_id,connector_id,operation,arguments_hash,"
                    "status,created_at,updated_at) VALUES(?,?,?,?,?,?,'running',?,?)",
                    (
                        user_id, run_id, operation_id, connector_id, operation,
                        arguments_hash, now, now,
                    ),
                )
                self._conn.commit()
                return {"status": "owner"}
            except Exception:
                self._conn.rollback()
                raise

    def complete_connector_operation(
        self, *, user_id: str, run_id: str, operation_id: str, result: dict
    ) -> None:
        encoded = json.dumps(
            result, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        self._exec(
            "UPDATE connector_operations SET status='succeeded',result_json=?,"
            "error_code=NULL,updated_at=? WHERE user_id=? AND run_id=? "
            "AND operation_id=? AND status='running'",
            (encoded, _now(), user_id, run_id, operation_id),
        )

    def fail_connector_operation(
        self, *, user_id: str, run_id: str, operation_id: str, error_code: str
    ) -> None:
        self._exec(
            "UPDATE connector_operations SET status='failed',error_code=?,updated_at=? "
            "WHERE user_id=? AND run_id=? AND operation_id=? AND status='running'",
            (error_code[:100], _now(), user_id, run_id, operation_id),
        )

    @staticmethod
    def _decode_pending_approval(row: Any) -> dict:
        value = dict(row)
        try:
            arguments = json.loads(value.pop("arguments_json"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("pending_approval_corrupt") from exc
        if not isinstance(arguments, dict):
            raise RuntimeError("pending_approval_corrupt")
        value["arguments"] = arguments
        return value

    def create_pending_approval(
        self,
        *,
        user_id: str,
        bot_id: str,
        run_id: str,
        target_type: str,
        connector_id: str,
        operation: str,
        arguments: dict,
        arguments_hash: str,
        human_summary: str,
        ttl_seconds: int = 600,
    ) -> dict:
        """Persist one exact proposed side effect before asking the user.

        The uniqueness key collapses repeated tool attempts made by the model
        during the same run. A later run receives a different approval and
        action id, so consent can never leak between logical user actions.
        """
        if target_type not in {"connector", "computer"}:
            raise ValueError("target_type inválido")
        encoded = json.dumps(
            arguments, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        now = _now()
        approval_id = new_id("apr")
        action_id = new_id("act")
        expires_at = now + max(60, min(int(ttl_seconds), 1800))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO pending_approvals("
                    "id,action_id,user_id,bot_id,run_id,target_type,connector_id,operation,"
                    "arguments_json,arguments_hash,human_summary,status,expires_at,created_at,updated_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?) "
                    "ON CONFLICT(user_id,run_id,target_type,connector_id,operation,arguments_hash) "
                    "DO NOTHING",
                    (
                        approval_id, action_id, user_id, bot_id, run_id, target_type,
                        connector_id, operation, encoded, arguments_hash,
                        human_summary[:1000], expires_at, now, now,
                    ),
                )
                row = self._conn.execute(
                    "SELECT * FROM pending_approvals WHERE user_id=? AND run_id=? "
                    "AND target_type=? AND connector_id=? AND operation=? AND arguments_hash=?",
                    (user_id, run_id, target_type, connector_id, operation, arguments_hash),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        if row is None:
            raise RuntimeError("pending_approval_not_created")
        return self._decode_pending_approval(row)

    def pending_approvals_for_run(self, user_id: str, run_id: str) -> list[dict]:
        now = _now()
        self._exec(
            "UPDATE pending_approvals SET status='expired',updated_at=? "
            "WHERE user_id=? AND run_id=? AND status IN ('pending','approved') AND expires_at<=?",
            (now, user_id, run_id, now),
        )
        return [
            self._decode_pending_approval(row)
            for row in self._q(
                "SELECT * FROM pending_approvals WHERE user_id=? AND run_id=? "
                "AND status='pending' AND expires_at>? ORDER BY created_at,id",
                (user_id, run_id, now),
            )
        ]

    def pending_approvals_for_bot(self, user_id: str, bot_id: str) -> list[dict]:
        """Return live approvals newest-first for a text-only channel.

        Desktop and mobile submit a typed approval id from their widget. A
        WhatsApp reply cannot carry that hidden payload, so the backend must
        resolve the latest proposal inside the already authenticated account
        and bot boundary.
        """
        now = _now()
        self._exec(
            "UPDATE pending_approvals SET status='expired',updated_at=? "
            "WHERE user_id=? AND bot_id=? AND status IN ('pending','approved') "
            "AND expires_at<=?",
            (now, user_id, bot_id, now),
        )
        return [
            self._decode_pending_approval(row)
            for row in self._q(
                "SELECT * FROM pending_approvals WHERE user_id=? AND bot_id=? "
                "AND status='pending' AND expires_at>? ORDER BY created_at DESC,id DESC",
                (user_id, bot_id, now),
            )
        ]

    def approve_pending_approval(
        self, *, user_id: str, bot_id: str, approval_id: str
    ) -> dict | None:
        """Approve exactly one proposal and return its immutable capability."""
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM pending_approvals WHERE id=? AND user_id=? AND bot_id=?",
                    (approval_id, user_id, bot_id),
                ).fetchone()
                if row is None or float(row["expires_at"]) <= now:
                    if row is not None and row["status"] in {"pending", "approved"}:
                        self._conn.execute(
                            "UPDATE pending_approvals SET status='expired',updated_at=? WHERE id=?",
                            (now, approval_id),
                        )
                        self._conn.commit()
                    else:
                        self._conn.rollback()
                    return None
                if row["status"] == "pending":
                    changed = self._conn.execute(
                        "UPDATE pending_approvals SET status='approved',approved_at=?,updated_at=? "
                        "WHERE id=? AND status='pending' AND expires_at>?",
                        (now, now, approval_id, now),
                    )
                    if changed.rowcount != 1:
                        self._conn.rollback()
                        return None
                    row = self._conn.execute(
                        "SELECT * FROM pending_approvals WHERE id=?", (approval_id,)
                    ).fetchone()
                elif row["status"] != "approved":
                    self._conn.rollback()
                    return None
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return self._decode_pending_approval(row)

    def reject_pending_approval(
        self, *, user_id: str, bot_id: str, approval_id: str
    ) -> bool:
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                changed = self._conn.execute(
                    "UPDATE pending_approvals SET status='rejected',consumed_at=?,updated_at=? "
                    "WHERE id=? AND user_id=? AND bot_id=? AND status='pending' AND expires_at>?",
                    (now, now, approval_id, user_id, bot_id, now),
                )
                self._conn.commit()
                return changed.rowcount == 1
            except Exception:
                self._conn.rollback()
                raise

    def dispatch_pending_approval(
        self,
        *,
        approval_id: str,
        action_id: str,
        user_id: str,
        connector_id: str,
        operation: str,
        arguments_hash: str,
    ) -> bool:
        """Consume the capability immediately before the provider call."""
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                changed = self._conn.execute(
                    "UPDATE pending_approvals SET status='dispatched',consumed_at=?,updated_at=? "
                    "WHERE id=? AND action_id=? AND user_id=? AND connector_id=? AND operation=? "
                    "AND arguments_hash=? AND status='approved' AND expires_at>?",
                    (
                        now, now, approval_id, action_id, user_id, connector_id,
                        operation, arguments_hash, now,
                    ),
                )
                self._conn.commit()
                return changed.rowcount == 1
            except Exception:
                self._conn.rollback()
                raise

    def settle_pending_approval(
        self, *, approval_id: str, action_id: str, succeeded: bool
    ) -> None:
        now = _now()
        self._exec(
            "UPDATE pending_approvals SET status=?,updated_at=? "
            "WHERE id=? AND action_id=? AND status='dispatched'",
            ("succeeded" if succeeded else "uncertain", now, approval_id, action_id),
        )

    def get_agent_run_by_token(self, token: str) -> dict | None:
        now = _now()
        row = self._one(
            "SELECT r.*,t.expires_at AS token_expires_at,u.account_status,"
            "u.id AS principal_user_id,u.tier AS principal_tier,"
            "u.unlimited_usage AS principal_unlimited_usage,"
            "u.model_provider_override AS principal_model_provider_override,"
            "u.subscription_id AS principal_subscription_id,"
            "gs.id AS provider_subscription_id,gs.api_key_enc AS provider_api_key_enc,"
            "gs.key_id AS provider_key_id,gs.key_version AS provider_key_version,"
            "gs.status AS provider_subscription_status,"
            "gs.assigned_user_id AS provider_assigned_user_id "
            "FROM agent_run_tokens t JOIN agent_runs r ON r.id=t.run_id "
            "JOIN users u ON u.id=t.user_id "
            "LEFT JOIN go_subscriptions gs ON gs.id=u.subscription_id "
            "WHERE t.token_hash=? AND t.revoked_at IS NULL "
            "AND t.expires_at>? AND r.status IN ('reserved','running') AND u.account_status='active'",
            (hash_agent_run_token(token), now),
        )
        return dict(row) if row else None

    def agent_run_cost_microusd(self, run_id: str) -> int:
        row = self._one(
            "SELECT COALESCE(SUM(estimated_cost_microusd),0) AS n FROM usage_events WHERE run_id=?",
            (run_id,),
        )
        return int(row["n"] if row else 0)

    def record_run_extra_cost(self, run_id: str, cost_microusd: int) -> None:
        """Add a measured non-LLM cost to an active run using integer units."""
        if cost_microusd < 0:
            raise ValueError("cost_microusd no puede ser negativo")
        self._exec(
            "UPDATE agent_runs SET extra_cost_microusd=extra_cost_microusd+?,heartbeat_at=? "
            "WHERE id=? AND status IN ('reserved','running')",
            (int(cost_microusd), _now(), run_id),
        )

    def settle_agent_run(
        self,
        *,
        run_id: str,
        charged_milli: int,
        final_status: str,
        duration_seconds: float | None,
        error_code: str | None = None,
        warnings: list[str] | None = None,
        reservation_status: str = "settled",
        result: dict | None = None,
    ) -> dict:
        if reservation_status not in {"settled", "released"}:
            raise ValueError("reservation_status inválido")
        encoded_result = None
        if result is not None:
            encoded_result = json.dumps(
                result, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
            if len(encoded_result.encode("utf-8")) > 1_000_000:
                raise ValueError("El resultado durable excede 1 MB")
        now = _now()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                run = self._conn.execute(
                    "SELECT * FROM agent_runs WHERE id=?", (run_id,)
                ).fetchone()
                if run is None:
                    raise KeyError(run_id)
                if run["status"] not in {"reserved", "running"}:
                    self._conn.rollback()
                    return dict(run)
                reservation = self._conn.execute(
                    "SELECT * FROM credit_reservations WHERE run_id=?", (run_id,)
                ).fetchone()
                reserved = int(reservation["reserved_milli"] if reservation else 0)
                charged = min(max(0, charged_milli), int(run["max_credit_milli"]))
                if reserved:
                    charged = min(charged, reserved)
                    allocations = self._conn.execute(
                        "SELECT a.*,g.expires_at,g.created_at FROM credit_reservation_allocations a "
                        "JOIN credit_grants g ON g.id=a.grant_id WHERE a.reservation_id=? "
                        "ORDER BY CASE WHEN g.expires_at IS NULL THEN 1 ELSE 0 END,g.expires_at,g.created_at",
                        (reservation["id"],),
                    ).fetchall()
                    consume_remaining = charged
                    for allocation in allocations:
                        allocated = int(allocation["allocated_milli"])
                        consumed = min(allocated, consume_remaining)
                        refund = allocated - consumed
                        if refund:
                            self._conn.execute(
                                "UPDATE credit_grants SET remaining_milli=remaining_milli+? WHERE id=?",
                                (refund, allocation["grant_id"]),
                            )
                        consume_remaining -= consumed
                    self._ledger_locked(
                        user_id=run["user_id"], run_id=run_id,
                        reservation_id=reservation["id"], entry_type="release",
                        amount_milli=reserved,
                        idempotency_key=f"release:{reservation['id']}", now=now,
                    )
                    if charged:
                        self._ledger_locked(
                            user_id=run["user_id"], run_id=run_id,
                            reservation_id=reservation["id"], entry_type="charge",
                            amount_milli=-charged,
                            idempotency_key=f"charge:{reservation['id']}", now=now,
                        )
                    self._conn.execute(
                        "UPDATE credit_reservations SET charged_milli=?,status=?,settled_at=? "
                        "WHERE id=?",
                        (charged, reservation_status, now, reservation["id"]),
                    )
                run_cost = self._conn.execute(
                    "SELECT COALESCE(SUM(estimated_cost_microusd),0) AS n "
                    "FROM usage_events WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                self._conn.execute(
                    "UPDATE agent_runs SET status=?,charged_credit_milli=?,llm_cost_microusd=?,"
                    "duration_seconds=?,error_code=?,warnings_json=?,"
                    "result_json=COALESCE(?,result_json),finished_at=?,heartbeat_at=? WHERE id=?",
                    (
                        final_status, charged, int(run_cost["n"] if run_cost else 0),
                        duration_seconds, error_code,
                        json.dumps(warnings or [], separators=(",", ":")),
                        encoded_result, now, now, run_id,
                    ),
                )
                self._conn.execute(
                    "UPDATE agent_run_tokens SET revoked_at=? WHERE run_id=? AND revoked_at IS NULL",
                    (now, run_id),
                )
                self._conn.commit()
                row = self._one("SELECT * FROM agent_runs WHERE id=?", (run_id,))
                return dict(row)
            except Exception:
                self._conn.rollback()
                raise

    def settle_unmetered_agent_run(
        self,
        *,
        run_id: str,
        final_status: str,
        duration_seconds: float | None,
        error_code: str | None = None,
        warnings: list[str] | None = None,
        result: dict | None = None,
    ) -> dict:
        """Settle an unlimited/internal run without credit calculations."""
        return self.settle_agent_run(
            run_id=run_id,
            charged_milli=0,
            final_status=final_status,
            duration_seconds=duration_seconds,
            error_code=error_code,
            warnings=warnings,
            result=result,
        )

    def release_agent_run(
        self,
        *,
        run_id: str,
        final_status: str = "cancelled",
        error_code: str | None = None,
        duration_seconds: float | None = None,
    ) -> dict:
        """Release the complete reservation without charging the user."""
        return self.settle_agent_run(
            run_id=run_id,
            charged_milli=0,
            final_status=final_status,
            duration_seconds=duration_seconds,
            error_code=error_code,
            reservation_status="released",
        )

    def recent_agent_runs(self, user_id: str, limit: int = 20) -> list[dict]:
        return [
            dict(row) for row in self._q(
                "SELECT * FROM agent_runs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, max(1, min(limit, 100))),
            )
        ]

    # ---------- uso ----------
    def record_usage(
        self,
        user_id: str,
        subscription_id: str | None,
        model: str | None,
        endpoint: str,
        input_tokens: int | None,
        output_tokens: int | None,
        cached_read: int | None,
        cached_write: int | None,
        estimated_cost_usd: float,
        status: int,
        run_id: str | None = None,
        estimated_cost_microusd: int | None = None,
    ) -> None:
        microusd = (
            max(0, int(estimated_cost_microusd))
            if estimated_cost_microusd is not None
            else max(0, int(round(estimated_cost_usd * 1_000_000)))
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage_events(user_id, subscription_id, model, endpoint, input_tokens, output_tokens, "
                "cached_read_tokens, cached_write_tokens, estimated_cost_usd,run_id,"
                "estimated_cost_microusd,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    user_id, subscription_id, model, endpoint, input_tokens, output_tokens,
                    cached_read, cached_write, estimated_cost_usd, run_id, microusd, status, _now(),
                ),
            )
            self._conn.commit()

    def transition_user_tier(self, user_id: str, tier: str) -> dict:
        """Change the product entitlement without assigning provider keys."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._transition_user_tier_locked(user_id, tier)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

            updated = self._conn.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
            return dict(updated)

    def _transition_user_tier_locked(self, user_id: str, tier: str) -> None:
        user = self._conn.execute(
            "SELECT id, subscription_id FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if user is None:
            raise KeyError(user_id)
        subscription_id = user["subscription_id"]
        if subscription_id:
            self._conn.execute(
                "UPDATE go_subscriptions SET status='revoked', assigned_user_id=NULL WHERE id=?",
                (subscription_id,),
            )
        self._conn.execute(
            "UPDATE users SET subscription_id=NULL, tier=? WHERE id=?",
            (tier, user_id),
        )

    # ---------- facturación Stripe ----------
    def get_billing_status(self, user_id: str) -> dict:
        customer = self._one(
            "SELECT stripe_customer_id FROM billing_customers WHERE user_id=?", (user_id,)
        )
        subscription = self._one(
            "SELECT stripe_subscription_id,tier,stripe_price_id,status,cancel_at_period_end,"
            "current_period_end,last_stripe_event_created,updated_at FROM billing_subscriptions "
            "WHERE user_id=? ORDER BY "
            "CASE WHEN status IN ('active','trialing','past_due') THEN 0 ELSE 1 END,"
            "last_stripe_event_created DESC,updated_at DESC LIMIT 1",
            (user_id,),
        )
        result = dict(subscription) if subscription else None
        if result:
            result["cancel_at_period_end"] = bool(result["cancel_at_period_end"])
        return {
            "customer_id": customer["stripe_customer_id"] if customer else None,
            "subscription": result,
        }

    def apply_billing_event(self, action: dict) -> dict:
        """Aplica un evento firmado y el entitlement en una transacción idempotente."""
        now = _now()
        event_id = action["event_id"]
        event_type = action["event_type"]
        stripe_event_created = action["stripe_event_created"]
        if (
            not isinstance(stripe_event_created, int)
            or isinstance(stripe_event_created, bool)
            or stripe_event_created < 0
        ):
            raise ValueError("stripe_event_created debe ser un timestamp Unix válido")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if self._conn.execute(
                    "SELECT 1 FROM stripe_events WHERE event_id=?", (event_id,)
                ).fetchone():
                    self._conn.rollback()
                    return {"duplicate": True}
                if not action.get("recognized"):
                    self._conn.execute(
                        "INSERT INTO stripe_events("
                        "event_id,event_type,stripe_event_created,processed_at"
                        ") VALUES(?,?,?,?)",
                        (event_id, event_type, stripe_event_created, now),
                    )
                    self._conn.commit()
                    return {"ignored": True}

                user_id = action.get("user_id")
                customer_id = action.get("customer_id")
                stripe_subscription_id = action.get("stripe_subscription_id")
                if customer_id:
                    bound = self._conn.execute(
                        "SELECT user_id FROM billing_customers WHERE stripe_customer_id=?",
                        (customer_id,),
                    ).fetchone()
                    if bound:
                        if user_id and user_id != bound["user_id"]:
                            raise ValueError("El customer de Stripe ya pertenece a otro usuario")
                        user_id = bound["user_id"]
                existing = None
                if stripe_subscription_id:
                    existing = self._conn.execute(
                        "SELECT * FROM billing_subscriptions WHERE stripe_subscription_id=?",
                        (stripe_subscription_id,),
                    ).fetchone()
                    if existing:
                        if user_id and user_id != existing["user_id"]:
                            raise ValueError("La suscripción de Stripe ya pertenece a otro usuario")
                        user_id = existing["user_id"]
                        last_created = int(existing["last_stripe_event_created"] or 0)
                        if stripe_event_created < last_created:
                            self._conn.execute(
                                "INSERT INTO stripe_events("
                                "event_id,event_type,stripe_event_created,processed_at"
                                ") VALUES(?,?,?,?)",
                                (event_id, event_type, stripe_event_created, now),
                            )
                            self._conn.commit()
                            return {
                                "ignored": True,
                                "stale": True,
                                "last_stripe_event_created": last_created,
                            }
                if not user_id or not self._conn.execute(
                    "SELECT 1 FROM users WHERE id=?", (user_id,)
                ).fetchone():
                    self._conn.execute(
                        "INSERT INTO stripe_events("
                        "event_id,event_type,stripe_event_created,processed_at"
                        ") VALUES(?,?,?,?)",
                        (event_id, event_type, stripe_event_created, now),
                    )
                    self._conn.commit()
                    return {"ignored": True}

                if customer_id:
                    own_customer = self._conn.execute(
                        "SELECT stripe_customer_id FROM billing_customers WHERE user_id=?", (user_id,)
                    ).fetchone()
                    if own_customer and own_customer["stripe_customer_id"] != customer_id:
                        raise ValueError("El usuario ya está ligado a otro customer de Stripe")
                    self._conn.execute(
                        "INSERT INTO billing_customers(user_id,stripe_customer_id,created_at,updated_at) "
                        "VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET updated_at=excluded.updated_at",
                        (user_id, customer_id, now, now),
                    )

                tier = action.get("tier") or (existing["tier"] if existing else None)
                price_id = action.get("stripe_price_id") or (
                    existing["stripe_price_id"] if existing else None
                )
                status = action.get("status") or (existing["status"] if existing else "unknown")
                if stripe_subscription_id and tier in {"basic", "pro", "business"}:
                    self._conn.execute(
                        "INSERT INTO billing_subscriptions("
                        "stripe_subscription_id,user_id,tier,stripe_price_id,status,"
                        "cancel_at_period_end,current_period_end,last_stripe_event_created,updated_at"
                        ") VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(stripe_subscription_id) DO UPDATE SET "
                        "tier=excluded.tier,stripe_price_id=excluded.stripe_price_id,status=excluded.status,"
                        "cancel_at_period_end=excluded.cancel_at_period_end,"
                        "current_period_end=excluded.current_period_end,"
                        "last_stripe_event_created=excluded.last_stripe_event_created,"
                        "updated_at=excluded.updated_at",
                        (
                            stripe_subscription_id,
                            user_id,
                            tier,
                            price_id,
                            status,
                            int(bool(action.get("cancel_at_period_end"))),
                            action.get("current_period_end"),
                            stripe_event_created,
                            now,
                        ),
                    )

                tier_action = action.get("tier_action")
                if tier_action == "activate":
                    if tier not in {"basic", "pro", "business"}:
                        raise ValueError("No se pudo resolver el tier pagado del evento Stripe")
                    self._transition_user_tier_locked(user_id, tier)
                    grant_milli = int(action.get("grant_credit_milli") or 0)
                    period_end = action.get("current_period_end")
                    if grant_milli > 0 and stripe_subscription_id and period_end:
                        self._grant_credits_locked(
                            user_id=user_id,
                            amount_milli=grant_milli,
                            source_type="subscription",
                            source_key=(
                                f"stripe-period:{stripe_subscription_id}:{int(period_end)}"
                            ),
                            starts_at=now,
                            expires_at=float(period_end),
                            metadata={
                                "tier": tier,
                                "stripe_subscription_id": stripe_subscription_id,
                                "current_period_end": int(period_end),
                            },
                            allow_increase=True,
                        )
                elif tier_action == "free":
                    replacement = self._conn.execute(
                        "SELECT tier FROM billing_subscriptions WHERE user_id=? "
                        "AND stripe_subscription_id<>? "
                        "AND status IN ('active','trialing','past_due') "
                        "ORDER BY last_stripe_event_created DESC,updated_at DESC LIMIT 1",
                        (user_id, stripe_subscription_id or ""),
                    ).fetchone()
                    if replacement:
                        self._transition_user_tier_locked(user_id, replacement["tier"])
                        tier_action = "keep"
                    else:
                        self._transition_user_tier_locked(user_id, "free")

                self._conn.execute(
                    "INSERT INTO stripe_events("
                    "event_id,event_type,stripe_event_created,processed_at"
                    ") VALUES(?,?,?,?)",
                    (event_id, event_type, stripe_event_created, now),
                )
                self._conn.commit()
                return {"user_id": user_id, "tier": tier if tier_action == "activate" else None}
            except Exception:
                self._conn.rollback()
                raise

    def usage_summary(self, user_id: str, subscription_id: str | None, tier: str = "free") -> dict:
        """Resume de uso por ventanas (5h/semana/mes) para el usuario.

        Limits are Agent Genia product budgets; no provider credential is tied
        to an individual user.
        """
        from .tiers import effective_limits

        now = _now()
        limits = effective_limits(tier)
        spans = {"5h": 5 * 3600, "week": 7 * 86400, "month": 30 * 86400}
        cutoffs = {label: now - span for label, span in spans.items()}
        event_totals = self._one(
            "SELECT "
            "COALESCE(SUM(CASE WHEN created_at>=? THEN estimated_cost_usd ELSE 0 END),0) AS cost_5h,"
            "COALESCE(SUM(CASE WHEN created_at>=? THEN estimated_cost_usd ELSE 0 END),0) AS cost_week,"
            "COALESCE(SUM(CASE WHEN created_at>=? THEN estimated_cost_usd ELSE 0 END),0) AS cost_month,"
            "COALESCE(SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END),0) AS requests_5h,"
            "COALESCE(SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END),0) AS requests_week,"
            "COALESCE(SUM(CASE WHEN created_at>=? THEN 1 ELSE 0 END),0) AS requests_month "
            "FROM usage_events WHERE user_id=? AND created_at>=?",
            (
                cutoffs["5h"], cutoffs["week"], cutoffs["month"],
                cutoffs["5h"], cutoffs["week"], cutoffs["month"],
                user_id, cutoffs["month"],
            ),
        )
        run_totals = self._one(
            "SELECT "
            "COALESCE(SUM(CASE WHEN created_at>=? THEN charged_credit_milli ELSE 0 END),0) AS credits_5h,"
            "COALESCE(SUM(CASE WHEN created_at>=? THEN charged_credit_milli ELSE 0 END),0) AS credits_week,"
            "COALESCE(SUM(CASE WHEN created_at>=? THEN charged_credit_milli ELSE 0 END),0) AS credits_month "
            "FROM agent_runs WHERE user_id=? AND charged_credit_milli>0 AND created_at>=?",
            (cutoffs["5h"], cutoffs["week"], cutoffs["month"], user_id, cutoffs["month"]),
        )
        model_rows = self._q(
            "SELECT COALESCE(model,'unknown') AS model,COUNT(*) AS requests,"
            "COALESCE(SUM(estimated_cost_usd),0) AS cost_usd "
            "FROM usage_events WHERE user_id=? AND created_at>=? GROUP BY COALESCE(model,'unknown')",
            (user_id, cutoffs["month"]),
        )
        by_model = {
            row["model"]: {
                "requests": int(row["requests"]),
                "cost_usd": round(float(row["cost_usd"]), 6),
            }
            for row in model_rows
        }
        result: dict = {}
        for label in spans:
            spent = float(event_totals[f"cost_{label}"] if event_totals else 0)
            spent_credits = int(run_totals[f"credits_{label}"] if run_totals else 0) / 1_000
            requests = int(event_totals[f"requests_{label}"] if event_totals else 0)
            result[label] = {
                "limit_credits": limits[label],
                "spent_credits": round(spent_credits, 3),
                "spent_usd": round(spent, 6),
                "requests": requests,
            }
        return {"user_id": user_id, "windows": result, "by_model": by_model}

    def usage_all(self) -> dict:
        events = self._q(
            "SELECT user_id, subscription_id, model, endpoint, input_tokens, output_tokens, "
            "cached_read_tokens, cached_write_tokens, estimated_cost_usd,run_id,"
            "estimated_cost_microusd,status,created_at "
            "FROM usage_events ORDER BY created_at DESC LIMIT 500"
        )
        return {"events": [dict(r) for r in events]}

    def purge_expired_ephemeral_data(self, now: float | None = None) -> dict[str, int]:
        """Apply the documented retention policy to credentials that no longer work."""
        current = _now() if now is None else now
        revoked_session_cutoff = current - 30 * 86400
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                counts: dict[str, int] = {}
                statements = {
                    "account_auth_attempts": (
                        "DELETE FROM account_auth_attempts WHERE expires_at<=?", (current,)
                    ),
                    "connector_auth_attempts": (
                        "DELETE FROM connector_auth_attempts WHERE expires_at<=?", (current,)
                    ),
                    "rate_limit_buckets": (
                        "DELETE FROM rate_limit_buckets WHERE expires_at<=?", (current,)
                    ),
                    "account_identity_tokens": (
                        "DELETE FROM account_identity_tokens WHERE expires_at<=?", (current,)
                    ),
                    "whatsapp_link_codes": (
                        "DELETE FROM whatsapp_link_codes WHERE expires_at<=?", (current,)
                    ),
                    "whatsapp_messages": (
                        "DELETE FROM whatsapp_messages WHERE created_at<=?",
                        (current - 90 * 86400,),
                    ),
                    "account_sessions": (
                        "DELETE FROM account_sessions WHERE refresh_expires_at<=? "
                        "OR (revoked_at IS NOT NULL AND revoked_at<=?)",
                        (current, revoked_session_cutoff),
                    ),
                    "agent_run_tokens": (
                        "DELETE FROM agent_run_tokens WHERE expires_at<=? "
                        "OR (revoked_at IS NOT NULL AND revoked_at<=?)",
                        (current, revoked_session_cutoff),
                    ),
                    "pending_approvals": (
                        "DELETE FROM pending_approvals WHERE expires_at<=? "
                        "OR (consumed_at IS NOT NULL AND consumed_at<=?)",
                        (current - 86400, current - 30 * 86400),
                    ),
                    "stripe_events": (
                        "DELETE FROM stripe_events WHERE processed_at<=?",
                        (current - 400 * 86400,),
                    ),
                    "usage_events": (
                        "DELETE FROM usage_events WHERE created_at<=?",
                        (current - 400 * 86400,),
                    ),
                }
                for name, (sql, params) in statements.items():
                    cursor = self._conn.execute(sql, params)
                    counts[name] = max(0, int(cursor.rowcount or 0))
                # A process can stop after claiming a webhook. Return only
                # genuinely abandoned work to the durable queue.
                self._conn.execute(
                    "UPDATE whatsapp_messages SET status='pending',next_attempt_at=?,updated_at=? "
                    "WHERE status='processing' AND updated_at<=?",
                    (current, current, current - 3600),
                )
                self._conn.execute(
                    "UPDATE whatsapp_messages SET status='failed',"
                    "last_error='outbound_delivery_uncertain',updated_at=? "
                    "WHERE status='sending' AND updated_at<=?",
                    (current, current - 3600),
                )
                self._conn.commit()
                return counts
            except Exception:
                self._conn.rollback()
                raise

    def expire_past_due_entitlements(
        self, *, now: float | None = None, grace_seconds: int = 7 * 86400
    ) -> int:
        """Downgrade accounts whose most recent paid state exceeded grace."""
        current = _now() if now is None else now
        cutoff = current - max(0, grace_seconds)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT DISTINCT b.user_id FROM billing_subscriptions b "
                    "WHERE b.status='past_due' AND b.updated_at<=? "
                    "AND NOT EXISTS (SELECT 1 FROM billing_subscriptions active "
                    "WHERE active.user_id=b.user_id AND active.status IN ('active','trialing'))",
                    (cutoff,),
                ).fetchall()
                for row in rows:
                    self._transition_user_tier_locked(row["user_id"], "free")
                self._conn.commit()
                return len(rows)
            except Exception:
                self._conn.rollback()
                raise
