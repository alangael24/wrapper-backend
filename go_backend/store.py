"""Capa de datos: SQLite (stdlib) con esquema de usuarios, suscripciones Go y uso."""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path

from .crypto_utils import hash_wrapper_key

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id              TEXT PRIMARY KEY,
  name            TEXT,
  email           TEXT,
  api_key_hash    TEXT UNIQUE NOT NULL,
  subscription_id TEXT,
  tier            TEXT NOT NULL DEFAULT 'basic',
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
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT
);
CREATE INDEX IF NOT EXISTS idx_users_subscription ON users(subscription_id);
CREATE INDEX IF NOT EXISTS idx_subs_status ON go_subscriptions(status);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_sub_time ON usage_events(subscription_id, created_at);
"""


def _now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


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
                self._conn.execute("ALTER TABLE users ADD COLUMN tier TEXT NOT NULL DEFAULT 'basic'")
            row = self._conn.execute("SELECT v FROM kv WHERE k='schema_version'").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO kv(k,v) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
            self._conn.commit()

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
                    subscription_id: str | None = None, tier: str = "basic") -> dict:
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

    def next_available(self) -> dict | None:
        row = self._one("SELECT * FROM go_subscriptions WHERE status='available' ORDER BY created_at LIMIT 1")
        return dict(row) if row else None

    def available_count(self) -> int:
        row = self._one("SELECT COUNT(*) AS n FROM go_subscriptions WHERE status='available'")
        return int(row["n"]) if row else 0

    def list_subscriptions(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM go_subscriptions ORDER BY created_at")]

    def revoke_subscription(self, sub_id: str) -> None:
        self._exec(
            "UPDATE go_subscriptions SET status='available', assigned_user_id=NULL WHERE id=?",
            (sub_id,),
        )

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

    def set_user_tier(self, user_id: str, tier: str) -> None:
        self._exec("UPDATE users SET tier=? WHERE id=?", (tier, user_id))

    def usage_summary(self, user_id: str, subscription_id: str | None, tier: str = "basic") -> dict:
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
