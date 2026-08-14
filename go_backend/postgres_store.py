"""Postgres/Supabase adapter with a bounded, health-checked connection pool."""

from __future__ import annotations

import ipaddress
import json
import threading
import time
from contextlib import nullcontext
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .store import SCHEMA_VERSION, Store, new_id


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
        if sslmode and sslmode != "verify-full":
            raise ValueError("DATABASE_URL remoto debe usar sslmode=verify-full")
        query.setdefault("sslmode", "verify-full")
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
            return connection.execute("BEGIN")
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
            connection.execute("SET statement_timeout TO '30s'")
            connection.execute("SET lock_timeout TO '5s'")
            connection.execute("SET idle_in_transaction_session_timeout TO '30s'")
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
        """Reserve an unlimited run in one Postgres round trip.

        The regular credit path intentionally remains in ``Store``. Internal
        unlimited accounts do not need grant scans or ledger allocations, and
        doing those operations one statement at a time across regions added
        several seconds before Pi could see a prompt.
        """
        now = time.time()
        run_id = new_id("run")
        reservation_id = new_id("rsv")
        row = self._one(
            "WITH locked AS MATERIALIZED ("
            " SELECT pg_advisory_xact_lock(hashtextextended(?::text,0))"
            "), existing AS MATERIALIZED ("
            " SELECT ar.* FROM agent_runs ar,locked "
            " WHERE ar.user_id=? AND ar.idempotency_key=?"
            "), active AS MATERIALIZED ("
            " SELECT COUNT(*)::bigint AS n FROM agent_runs ar,locked "
            " WHERE ar.user_id=? AND ar.status IN ('reserved','running')"
            "), inserted AS ("
            " INSERT INTO agent_runs("
            " id,user_id,idempotency_key,status,harness,model,browser,max_credit_milli,"
            " reserved_credit_milli,created_at,heartbeat_at) "
            " SELECT ?,?,?,'running','pi',?,?,?,0,?,? FROM active "
            " WHERE active.n<? AND NOT EXISTS(SELECT 1 FROM existing) RETURNING *"
            "), reservation AS ("
            " INSERT INTO credit_reservations("
            " id,user_id,run_id,reserved_milli,status,expires_at,created_at) "
            " SELECT ?,user_id,id,0,'active',?,? FROM inserted RETURNING run_id"
            "), token_inserted AS ("
            " INSERT INTO agent_run_tokens(token_hash,user_id,run_id,expires_at,created_at) "
            " SELECT ?,user_id,id,?,? FROM inserted RETURNING run_id"
            ") SELECT 'duplicate' AS outcome,to_jsonb(existing) AS run FROM existing "
            " UNION ALL SELECT 'inserted',to_jsonb(inserted) FROM inserted",
            (
                user_id,
                user_id,
                idempotency_key,
                user_id,
                run_id,
                user_id,
                idempotency_key,
                model,
                int(browser),
                max_credit_milli,
                now,
                now,
                max_concurrent_runs,
                reservation_id,
                token_expires_at,
                now,
                token_hash,
                token_expires_at,
                now,
            ),
        )
        if row is None:
            raise RuntimeError("credit_concurrency_limit")
        return {
            "duplicate": row["outcome"] == "duplicate",
            "run": dict(row["run"]),
        }

    def settle_unmetered_agent_run(
        self,
        *,
        run_id: str,
        final_status: str,
        duration_seconds: float | None,
        error_code: str | None = None,
        warnings: list[str] | None = None,
    ) -> dict:
        """Finish an unlimited run and revoke its token in one statement."""
        now = time.time()
        row = self._one(
            "WITH revoked AS ("
            " UPDATE agent_run_tokens SET revoked_at=? "
            " WHERE run_id=? AND revoked_at IS NULL RETURNING run_id"
            "), reservation AS ("
            " UPDATE credit_reservations SET charged_milli=0,status='settled',settled_at=? "
            " WHERE run_id=? AND status='active' RETURNING run_id"
            "), updated AS ("
            " UPDATE agent_runs SET status=?,charged_credit_milli=0,"
            " llm_cost_microusd=(SELECT COALESCE(SUM(estimated_cost_microusd),0) "
            " FROM usage_events WHERE run_id=?),duration_seconds=?,error_code=?,"
            " warnings_json=?,finished_at=?,heartbeat_at=? "
            " WHERE id=? AND status IN ('reserved','running') RETURNING *"
            ") SELECT to_jsonb(updated) AS run FROM updated",
            (
                now,
                run_id,
                now,
                run_id,
                final_status,
                run_id,
                duration_seconds,
                error_code,
                json.dumps(warnings or [], separators=(",", ":")),
                now,
                now,
                run_id,
            ),
        )
        if row is not None:
            return dict(row["run"])
        existing = self.get_agent_run(run_id)
        if existing is None:
            raise KeyError(run_id)
        return existing

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
