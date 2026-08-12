"""Postgres/Supabase adapter with a bounded, health-checked connection pool."""

from __future__ import annotations

import ipaddress
import threading
from contextlib import nullcontext
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
    return sql.replace("?", "%s")


class _PooledConnectionCompat:
    """Checks out one connection per thread until commit/rollback."""

    def __init__(self, pool: Any):
        self._pool = pool
        self._local = threading.local()

    def _connection(self) -> Any:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = self._pool.getconn()
            self._local.connection = connection
        return connection

    def execute(self, sql: str, params: tuple = ()) -> Any:
        connection = self._connection()
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            cursor = connection.execute("BEGIN")
            connection.execute(
                "SELECT pg_advisory_xact_lock(%s)", (POSTGRES_WRITE_LOCK_ID,)
            )
            return cursor
        return connection.execute(_postgres_sql(sql), params)

    def _finish(self, *, commit: bool) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            return
        try:
            connection.commit() if commit else connection.rollback()
        finally:
            self._local.connection = None
            self._pool.putconn(connection)

    def commit(self) -> None:
        self._finish(commit=True)

    def rollback(self) -> None:
        self._finish(commit=False)

    def close(self) -> None:
        self.rollback()


class PostgresStore(Store):
    """Store backed by the private Agent Genia schema in Supabase Postgres."""

    def __init__(self, database_url: str):
        try:
            import psycopg
            from psycopg.rows import dict_row
            from psycopg_pool import ConnectionPool
        except ImportError as exc:  # pragma: no cover - deployment dependency guard
            raise RuntimeError(
                "DATABASE_URL requiere psycopg[binary,pool]; instala requirements.txt"
            ) from exc

        def configure(connection: Any) -> None:
            connection.execute("SET search_path TO agentgenia, public")
            connection.commit()

        self._path = "postgres"
        self._lock = nullcontext()
        self._operational_error = psycopg.OperationalError
        self._pool = ConnectionPool(
            conninfo=normalize_database_url(database_url),
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": 10,
                "application_name": "agentgenia-wrapper",
            },
            min_size=1,
            max_size=10,
            timeout=10,
            max_idle=300,
            max_lifetime=1800,
            reconnect_timeout=30,
            configure=configure,
            check=ConnectionPool.check_connection,
            open=False,
            name="agentgenia-wrapper",
        )
        self._pool.open()
        try:
            self._pool.wait(timeout=10)
            self._conn = _PooledConnectionCompat(self._pool)
            row = self._conn.execute(
                "SELECT v FROM agentgenia.kv WHERE k='schema_version'"
            ).fetchone()
            self._conn.commit()
            if row is None or int(row["v"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    "El esquema Supabase de Agent Genia no está migrado a la versión "
                    f"{SCHEMA_VERSION}"
                )
        except Exception:
            self._conn.rollback()
            self._pool.close()
            raise

    def _read(self, method: str, sql: str, params: tuple) -> Any:
        for attempt in range(2):
            try:
                cursor = self._conn.execute(sql, params)
                value = getattr(cursor, method)()
                self._conn.commit()
                return value
            except self._operational_error:
                self._conn.rollback()
                if attempt:
                    raise
            except Exception:
                self._conn.rollback()
                raise
        raise AssertionError("unreachable")

    def _q(self, sql: str, params: tuple = ()) -> list[Any]:
        return self._read("fetchall", sql, params)

    def _one(self, sql: str, params: tuple = ()) -> Any | None:
        return self._read("fetchone", sql, params)

    def _exec(self, sql: str, params: tuple = ()) -> None:
        try:
            self._conn.execute(sql, params)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def health(self) -> dict:
        try:
            with self._pool.connection(timeout=5) as connection:
                row = connection.execute(
                    "SELECT v FROM agentgenia.kv WHERE k='schema_version'"
                ).fetchone()
            ready = bool(row and int(row["v"]) == SCHEMA_VERSION)
            return {
                "ready": ready,
                "schema_version": int(row["v"]) if row else None,
                "backend": "postgres",
                "pool": self._pool.get_stats(),
            }
        except Exception:
            return {
                "ready": False,
                "schema_version": None,
                "backend": "postgres",
            }

    def close(self) -> None:
        self._conn.close()
        self._pool.close()


def create_store(*, database_url: str | None, db_path: Any) -> Store:
    if database_url:
        return PostgresStore(database_url)
    return Store(db_path)
