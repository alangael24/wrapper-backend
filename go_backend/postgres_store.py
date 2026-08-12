"""Postgres/Supabase adapter for the existing server-owned Store contract.

The local development and test path remains SQLite.  Production can opt into
this adapter with ``DATABASE_URL`` without changing the HTTP API or Pi harness.
"""

from __future__ import annotations

import ipaddress
import threading
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .store import SCHEMA_VERSION, Store


POSTGRES_WRITE_LOCK_ID = 6_913_322_107_743_045_083


def normalize_database_url(value: str) -> str:
    """Validate a Postgres URL and require TLS for non-loopback hosts."""
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("DATABASE_URL debe usar postgres:// o postgresql://")
    if not parsed.hostname:
        raise ValueError("DATABASE_URL no incluye un host")
    try:
        loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    sslmode = query.get("sslmode", "").lower()
    if not loopback:
        if sslmode in {"disable", "allow", "prefer"}:
            raise ValueError("DATABASE_URL remoto debe verificar TLS")
        query.setdefault("sslmode", "require")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def _postgres_sql(sql: str) -> str:
    # The Store queries contain no '?' string literals; every question mark is
    # a SQLite DB-API placeholder.
    return sql.replace("?", "%s")


class _PostgresConnectionCompat:
    """Small compatibility layer used by the existing Store implementation."""

    def __init__(self, connection: Any):
        self._connection = connection

    def execute(self, sql: str, params: tuple = ()) -> Any:
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            cursor = self._connection.execute("BEGIN")
            self._connection.execute(
                "SELECT pg_advisory_xact_lock(%s)", (POSTGRES_WRITE_LOCK_ID,)
            )
            return cursor
        return self._connection.execute(_postgres_sql(sql), params)

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


class PostgresStore(Store):
    """Store implementation backed by a private Supabase Postgres schema."""

    def __init__(self, database_url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - deployment dependency guard
            raise RuntimeError(
                "DATABASE_URL requiere psycopg[binary]; instala requirements.txt"
            ) from exc

        self._path = "postgres"
        self._lock = threading.RLock()
        connection = psycopg.connect(
            normalize_database_url(database_url),
            row_factory=dict_row,
            connect_timeout=10,
            application_name="agentgenia-wrapper",
        )
        self._conn = _PostgresConnectionCompat(connection)
        try:
            self._conn.execute("SET search_path TO agentgenia, public")
            row = self._conn.execute(
                "SELECT v FROM agentgenia.kv WHERE k='schema_version'"
            ).fetchone()
            if row is None or int(row["v"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    "El esquema Supabase de Agent Genia no está migrado a la versión "
                    f"{SCHEMA_VERSION}"
                )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            self._conn.close()
            raise

    def _q(self, sql: str, params: tuple = ()) -> list[Any]:
        with self._lock:
            try:
                rows = self._conn.execute(sql, params).fetchall()
                self._conn.commit()
                return rows
            except Exception:
                self._conn.rollback()
                raise

    def _one(self, sql: str, params: tuple = ()) -> Any | None:
        with self._lock:
            try:
                row = self._conn.execute(sql, params).fetchone()
                self._conn.commit()
                return row
            except Exception:
                self._conn.rollback()
                raise

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            try:
                self._conn.execute(sql, params)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise


def create_store(*, database_url: str | None, db_path: Any) -> Store:
    if database_url:
        return PostgresStore(database_url)
    return Store(db_path)
