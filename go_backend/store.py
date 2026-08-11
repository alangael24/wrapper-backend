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

SCHEMA_VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id              TEXT PRIMARY KEY,
  name            TEXT,
  email           TEXT,
  api_key_hash    TEXT UNIQUE NOT NULL,
  subscription_id TEXT,
  tier            TEXT NOT NULL DEFAULT 'free',
  created_at      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS go_subscriptions (
  id               TEXT PRIMARY KEY,
  api_key_enc      BLOB NOT NULL,
  key_id           TEXT NOT NULL,          -- id usado en el Keychain (si aplica)
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
"""


def _now() -> float:
    return time.time()


class NoSubscriptionAvailable(RuntimeError):
    pass


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _hash_account_token(kind: str, token: str) -> str:
    return hashlib.sha256(f"account-{kind}|{token}".encode()).hexdigest()


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
            tier_column = next(
                row for row in self._conn.execute("PRAGMA table_info(users)") if row[1] == "tier"
            )
            if str(tier_column[4]).strip("'\"").lower() != "free":
                self._migrate_users_default_to_free()
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
                  created_at      REAL NOT NULL
                )"""
            )
            self._conn.execute(
                "INSERT INTO users(id, name, email, api_key_hash, subscription_id, tier, created_at) "
                "SELECT id, name, email, api_key_hash, subscription_id, tier, created_at "
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
        row = self._one("SELECT * FROM users WHERE api_key_hash=?", (hash_wrapper_key(api_key),))
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
                    "SELECT * FROM account_identities WHERE provider='google' AND subject=?",
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
            "SELECT a.*, u.tier, u.subscription_id FROM account_identities a "
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
            "WHERE s.access_token_hash=? AND s.revoked_at IS NULL AND s.access_expires_at>?",
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
                    "SELECT * FROM account_sessions WHERE refresh_token_hash=? "
                    "AND device_id=? AND revoked_at IS NULL AND refresh_expires_at>?",
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
    def add_subscription(self, api_key_enc: bytes, key_id: str, label: str | None, source: str = "pool", user_id: str | None = None, sub_id: str | None = None) -> dict:
        sub_id = sub_id or new_id("sub")
        created = _now()
        status = "assigned" if user_id else "available"
        with self._lock:
            self._conn.execute(
                "INSERT INTO go_subscriptions(id, api_key_enc, key_id, label, source, status, assigned_user_id, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (sub_id, api_key_enc, key_id, label, source, status, user_id, created),
            )
            if user_id:
                self._conn.execute("UPDATE users SET subscription_id=? WHERE id=?", (sub_id, user_id))
            self._conn.commit()
        return self.get_subscription(sub_id)  # type: ignore[return-value]

    def get_subscription(self, sub_id: str) -> dict | None:
        row = self._one("SELECT * FROM go_subscriptions WHERE id=?", (sub_id,))
        return dict(row) if row else None

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
                        "UPDATE go_subscriptions "
                        "SET status='assigned', assigned_user_id=? "
                        "WHERE id=? AND status='available'",
                        (user_id, subscription_id),
                    )
                    if claimed.rowcount != 1:
                        raise NoSubscriptionAvailable
                elif not needs_subscription and subscription_id:
                    self._conn.execute(
                        "UPDATE go_subscriptions "
                        "SET status='available', assigned_user_id=NULL WHERE id=?",
                        (subscription_id,),
                    )
                    subscription_id = None

                self._conn.execute(
                    "UPDATE users SET subscription_id=?, tier=? WHERE id=?",
                    (subscription_id, tier, user_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

            updated = self._conn.execute(
                "SELECT * FROM users WHERE id=?", (user_id,)
            ).fetchone()
            return dict(updated)

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
