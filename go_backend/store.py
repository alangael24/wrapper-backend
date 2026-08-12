"""Capa de datos: SQLite (stdlib) con esquema de usuarios, suscripciones Go y uso."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from .crypto_utils import hash_wrapper_key

SCHEMA_VERSION = 8

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
  created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS go_subscriptions (
  id               TEXT PRIMARY KEY,
  api_key_enc      BLOB NOT NULL,
  key_id           TEXT NOT NULL,          -- id usado en el Keychain (si aplica)
  key_version      INTEGER NOT NULL DEFAULT 1,
  label            TEXT,
  source           TEXT NOT NULL DEFAULT 'pool',   -- pool | byok
  status           TEXT NOT NULL DEFAULT 'available', -- available | assigned | revoked
  assigned_user_id TEXT,
  note             TEXT,
  created_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_events (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id              TEXT NOT NULL,
  subscription_id      TEXT NOT NULL,
  model                TEXT,
  endpoint             TEXT,
  input_tokens         INTEGER,
  output_tokens        INTEGER,
  cached_read_tokens   INTEGER,
  cached_write_tokens  INTEGER,
  estimated_cost_usd   REAL NOT NULL DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS idx_account_auth_attempts_expires
  ON account_auth_attempts(expires_at);
CREATE INDEX IF NOT EXISTS idx_connector_auth_attempts_user
  ON connector_auth_attempts(user_id, expires_at);
"""


def _now() -> float:
    return time.time()


class NoSubscriptionAvailable(RuntimeError):
    pass


class ComputerLimitReached(RuntimeError):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _hash_account_token(kind: str, token: str) -> str:
    return hashlib.sha256(f"account-{kind}|{token}".encode()).hexdigest()


def _hash_ephemeral(kind: str, value: str) -> str:
    return hashlib.sha256(f"agentgenia-{kind}|{value}".encode()).hexdigest()


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
            self._conn.execute(
                "INSERT INTO kv(k,v) VALUES('schema_version', ?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

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
                  created_at      REAL NOT NULL
                )"""
            )
            self._conn.execute(
                "INSERT INTO users(id, name, email, api_key_hash, subscription_id, tier,account_status,disabled_at,created_at) "
                "SELECT id, name, email, api_key_hash, subscription_id, tier,account_status,disabled_at,created_at "
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
            "SELECT * FROM users WHERE api_key_hash=? AND account_status='active'",
            (hash_wrapper_key(api_key),),
        )
        return dict(row) if row else None

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
            "SELECT u.* FROM account_sessions s "
            "JOIN account_identities a ON a.id=s.account_id "
            "JOIN users u ON u.id=a.user_id "
            "WHERE s.access_token_hash=? AND s.revoked_at IS NULL AND s.access_expires_at>? "
            "AND u.account_status='active'",
            (_hash_account_token("access", access_token), _now()),
        )
        return dict(row) if row else None

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
                        "UPDATE go_subscriptions SET status='available',assigned_user_id=NULL "
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

    # ---------- uso ----------
    def record_usage(
        self,
        user_id: str,
        subscription_id: str,
        model: str | None,
        endpoint: str,
        input_tokens: int | None,
        output_tokens: int | None,
        cached_read: int | None,
        cached_write: int | None,
        estimated_cost_usd: float,
        status: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage_events(user_id, subscription_id, model, endpoint, input_tokens, output_tokens, "
                "cached_read_tokens, cached_write_tokens, estimated_cost_usd, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    user_id, subscription_id, model, endpoint, input_tokens, output_tokens,
                    cached_read, cached_write, estimated_cost_usd, status, _now(),
                ),
            )
            self._conn.commit()

    def transition_user_tier(
        self, user_id: str, tier: str, *, needs_subscription: bool
    ) -> dict:
        """Cambia tier y asignación dentro de una sola transacción de escritura.

        BEGIN IMMEDIATE serializa esta operación incluso entre conexiones o
        procesos. El UPDATE condicional y el índice único son defensas extra
        para impedir que una suscripción termine asociada a dos usuarios.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._transition_user_tier_locked(user_id, tier, needs_subscription=needs_subscription)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

            updated = self._conn.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
            return dict(updated)

    def _transition_user_tier_locked(
        self, user_id: str, tier: str, *, needs_subscription: bool
    ) -> None:
        user = self._conn.execute(
            "SELECT id, subscription_id FROM users WHERE id=?", (user_id,)
        ).fetchone()
        if user is None:
            raise KeyError(user_id)
        subscription_id = user["subscription_id"]
        if needs_subscription and not subscription_id:
            available = self._conn.execute(
                "SELECT id FROM go_subscriptions "
                "WHERE status='available' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if available is None:
                raise NoSubscriptionAvailable
            subscription_id = available["id"]
            claimed = self._conn.execute(
                "UPDATE go_subscriptions SET status='assigned', assigned_user_id=? "
                "WHERE id=? AND status='available'",
                (user_id, subscription_id),
            )
            if claimed.rowcount != 1:
                raise NoSubscriptionAvailable
        elif not needs_subscription and subscription_id:
            self._conn.execute(
                "UPDATE go_subscriptions SET status='available', assigned_user_id=NULL WHERE id=?",
                (subscription_id,),
            )
            subscription_id = None
        self._conn.execute(
            "UPDATE users SET subscription_id=?, tier=? WHERE id=?",
            (subscription_id, tier, user_id),
        )

    # ---------- facturación Stripe ----------
    def get_billing_status(self, user_id: str) -> dict:
        customer = self._one(
            "SELECT stripe_customer_id FROM billing_customers WHERE user_id=?", (user_id,)
        )
        subscription = self._one(
            "SELECT stripe_subscription_id,tier,stripe_price_id,status,cancel_at_period_end,"
            "current_period_end,last_stripe_event_created,updated_at FROM billing_subscriptions "
            "WHERE user_id=? ORDER BY updated_at DESC LIMIT 1",
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
                if stripe_subscription_id and tier in {"basic", "pro"}:
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
                    if tier not in {"basic", "pro"}:
                        raise ValueError("No se pudo resolver el tier pagado del evento Stripe")
                    self._transition_user_tier_locked(user_id, tier, needs_subscription=True)
                elif tier_action == "free":
                    self._transition_user_tier_locked(user_id, "free", needs_subscription=False)

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

        Los limites se ajustan al tier: basic=50% de la suscripcion Go,
        pro=100%. free no tiene acceso a modelos.
        """
        from .tiers import effective_limits

        now = _now()
        events = self._q(
            "SELECT estimated_cost_usd, created_at, model FROM usage_events WHERE user_id=?",
            (user_id,),
        )
        limits = effective_limits(tier)
        spans = {"5h": 5 * 3600, "week": 7 * 86400, "month": 30 * 86400}
        by_model: dict[str, dict] = {}
        result: dict = {}
        for label, span in spans.items():
            spent = sum(e["estimated_cost_usd"] for e in events if now - e["created_at"] <= span)
            requests = sum(1 for e in events if now - e["created_at"] <= span)
            result[label] = {"limit_usd": limits[label], "spent_usd": round(spent, 6), "requests": requests}
        for e in events:
            m = e["model"] or "unknown"
            agg = by_model.setdefault(m, {"requests": 0, "cost_usd": 0.0})
            agg["requests"] += 1
            agg["cost_usd"] = round(agg["cost_usd"] + e["estimated_cost_usd"], 6)
        return {"user_id": user_id, "subscription_id": subscription_id, "windows": result, "by_model": by_model}

    def usage_all(self) -> dict:
        events = self._q(
            "SELECT user_id, subscription_id, model, endpoint, input_tokens, output_tokens, "
            "cached_read_tokens, cached_write_tokens, estimated_cost_usd, status, created_at "
            "FROM usage_events ORDER BY created_at DESC LIMIT 500"
        )
        return {"events": [dict(r) for r in events]}
