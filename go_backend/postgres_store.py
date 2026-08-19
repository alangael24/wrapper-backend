"""Postgres/Supabase adapter with a bounded, health-checked connection pool."""

from __future__ import annotations

import ipaddress
import json
import threading
import time
from contextlib import nullcontext
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .store import (
    AccountStateConflict,
    SCHEMA_VERSION,
    Store,
    _hash_account_token,
    hash_whatsapp_link_code,
    hash_wrapper_key,
    new_id,
)


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
        self._undefined_function_error = psycopg.errors.UndefinedFunction
        self._pool = ConnectionPool(
            conninfo=normalize_database_url(database_url),
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": 10,
                "application_name": "agentgenia-wrapper",
            },
            # Mobile startup performs profile/state/connector refreshes in
            # parallel. Keep enough TLS connections warm so the first agent
            # turn does not wait several seconds for lazy pool growth.
            min_size=4,
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
        # Create the compatibility facade before opening/waiting on the pool.
        # That makes cleanup safe even when the very first TLS connection fails.
        self._conn = _PooledConnectionCompat(self._pool)
        self._pool.open()
        try:
            self._pool.wait(timeout=10)
            row = self._conn.execute(
                "SELECT v FROM agentgenia.kv WHERE k='schema_version'"
            ).fetchone()
            self._conn.commit()
            if row is None or int(row["v"]) not in {SCHEMA_VERSION - 1, SCHEMA_VERSION}:
                raise RuntimeError(
                    "El esquema Supabase de Agent Genia no está migrado a la versión "
                    f"{SCHEMA_VERSION - 1} o {SCHEMA_VERSION}"
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

    @staticmethod
    def _agent_connector_projection() -> str:
        return (
            "COALESCE((SELECT bot.value->'connectorIds' FROM account_states ast "
            "CROSS JOIN LATERAL jsonb_array_elements("
            "COALESCE((ast.state_json::jsonb)->'bots','[]'::jsonb)) AS bot(value) "
            "WHERE ast.user_id=u.id AND bot.value->>'id'=? LIMIT 1),"
            "'[]'::jsonb)::text AS assigned_connector_ids_json "
        )

    def get_agent_user_by_api_key(self, api_key: str, bot_id: str | None) -> dict | None:
        row = self._one(
            "SELECT u.*,gs.id AS provider_subscription_id,"
            "gs.api_key_enc AS provider_api_key_enc,gs.key_id AS provider_key_id,"
            "gs.key_version AS provider_key_version,gs.status AS provider_subscription_status,"
            "gs.assigned_user_id AS provider_assigned_user_id,"
            + self._agent_connector_projection()
            + "FROM users u LEFT JOIN go_subscriptions gs ON gs.id=u.subscription_id "
            "WHERE u.api_key_hash=? AND u.account_status='active'",
            (bot_id or "", hash_wrapper_key(api_key)),
        )
        return dict(row) if row else None

    def get_agent_user_by_access_token(
        self, access_token: str, bot_id: str | None
    ) -> dict | None:
        row = self._one(
            "SELECT u.*,s.access_expires_at AS authenticated_until,"
            "gs.id AS provider_subscription_id,"
            "gs.api_key_enc AS provider_api_key_enc,gs.key_id AS provider_key_id,"
            "gs.key_version AS provider_key_version,gs.status AS provider_subscription_status,"
            "gs.assigned_user_id AS provider_assigned_user_id,"
            + self._agent_connector_projection()
            + "FROM account_sessions s "
            "JOIN account_identities a ON a.id=s.account_id "
            "JOIN users u ON u.id=a.user_id "
            "LEFT JOIN go_subscriptions gs ON gs.id=u.subscription_id "
            "WHERE s.access_token_hash=? AND s.revoked_at IS NULL AND s.access_expires_at>? "
            "AND u.account_status='active'",
            (
                bot_id or "",
                _hash_account_token("access", access_token),
                time.time(),
            ),
        )
        return dict(row) if row else None

    def consume_whatsapp_link_code(
        self,
        *,
        code: str,
        wa_user_id: str,
        phone_number_id: str,
        display_name: str,
    ) -> dict | None:
        """Consume a link code under a row lock across multiple replicas."""
        now = time.time()
        code_hash = hash_whatsapp_link_code(code)
        try:
            row = self._conn.execute(
                "SELECT c.*,u.account_status FROM whatsapp_link_codes c "
                "JOIN users u ON u.id=c.user_id WHERE c.code_hash=? FOR UPDATE",
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

    def claim_whatsapp_message(self) -> dict | None:
        """Atomically lease one webhook while preserving per-chat order."""
        now = time.time()
        try:
            row = self._conn.execute(
                "WITH candidate AS ("
                " SELECT m.message_id FROM whatsapp_messages m "
                " WHERE m.status='pending' AND m.next_attempt_at<=? AND NOT EXISTS ("
                " SELECT 1 FROM whatsapp_messages earlier "
                " WHERE earlier.phone_number_id=m.phone_number_id "
                " AND earlier.wa_user_id=m.wa_user_id "
                " AND earlier.status IN ('pending','processing','sending') AND ("
                " earlier.created_at<m.created_at OR "
                " (earlier.created_at=m.created_at AND earlier.message_id<m.message_id))) "
                " ORDER BY m.created_at,m.message_id FOR UPDATE SKIP LOCKED LIMIT 1"
                "), claimed AS ("
                " UPDATE whatsapp_messages m SET status='processing',"
                " attempts=m.attempts+1,updated_at=? FROM candidate "
                " WHERE m.message_id=candidate.message_id RETURNING m.*"
                ") SELECT claimed.*,to_jsonb(l) AS context_link_json,"
                "to_jsonb(u) AS context_user_json,"
                "gs.id AS context_provider_subscription_id,"
                "gs.api_key_enc AS context_provider_api_key_enc,"
                "gs.key_id AS context_provider_key_id,"
                "gs.key_version AS context_provider_key_version,"
                "gs.status AS context_provider_subscription_status,"
                "gs.assigned_user_id AS context_provider_assigned_user_id,"
                "ast.user_id AS context_state_user_id,"
                "ast.revision AS context_state_revision,"
                "ast.state_json AS context_state_json,"
                "ast.created_at AS context_state_created_at,"
                "ast.updated_at AS context_state_updated_at "
                "FROM claimed "
                "LEFT JOIN whatsapp_links l ON l.wa_user_id=claimed.wa_user_id "
                " AND l.phone_number_id=claimed.phone_number_id "
                "LEFT JOIN users u ON u.id=l.user_id AND u.account_status='active' "
                "LEFT JOIN go_subscriptions gs ON gs.id=u.subscription_id "
                "LEFT JOIN account_states ast ON ast.user_id=u.id",
                (now, now),
            ).fetchone()
            self._conn.commit()
            if row is None:
                return None
            message = dict(row)
            message["_processing_context"] = self._whatsapp_context_from_row(
                row, prefix="context_"
            )
            return message
        except Exception:
            self._conn.rollback()
            raise

    def enqueue_whatsapp_messages(self, messages: list[dict]) -> int:
        """Insert a complete Meta webhook batch in one PostgreSQL round trip."""
        if not messages:
            return 0
        now = time.time()
        encoded = json.dumps(
            [
                {
                    "message_id": str(item["message_id"])[:300],
                    "phone_number_id": str(item["phone_number_id"])[:100],
                    "wa_user_id": str(item["wa_user_id"])[:100],
                    "message_type": str(item["message_type"])[:40],
                    "text": str(item.get("text") or "")[:20_000],
                    "payload_json": json.dumps(
                        item.get("payload") or {},
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                }
                for item in messages
            ],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        rows = self._q(
            "WITH incoming AS ("
            " SELECT * FROM jsonb_to_recordset(?::jsonb) AS x("
            " message_id text,phone_number_id text,wa_user_id text,"
            " message_type text,text text,payload_json text)"
            ") INSERT INTO whatsapp_messages("
            " message_id,user_id,phone_number_id,wa_user_id,message_type,text,payload_json,"
            " status,next_attempt_at,created_at,updated_at) "
            "SELECT i.message_id,CASE WHEN u.id IS NOT NULL THEN l.user_id END,"
            "i.phone_number_id,i.wa_user_id,"
            "i.message_type,i.text,i.payload_json,'pending',?,?,? FROM incoming i "
            "LEFT JOIN whatsapp_links l ON l.wa_user_id=i.wa_user_id "
            " AND l.phone_number_id=i.phone_number_id "
            "LEFT JOIN users u ON u.id=l.user_id AND u.account_status='active' "
            "ON CONFLICT(message_id) DO NOTHING RETURNING message_id",
            (encoded, now, now, now),
        )
        return len(rows)

    def enqueue_and_claim_whatsapp_messages(
        self, messages: list[dict]
    ) -> tuple[int, dict | None]:
        """Pipeline durable insertion and the first ordered lease.

        Both commands execute sequentially in one PostgreSQL transaction and
        one client/server synchronization. The claim therefore sees the rows
        inserted by the first command while retaining the existing SKIP LOCKED
        ordering and multi-replica safety guarantees.
        """
        if not messages:
            return 0, None
        now = time.time()
        encoded = json.dumps(
            [
                {
                    "message_id": str(item["message_id"])[:300],
                    "phone_number_id": str(item["phone_number_id"])[:100],
                    "wa_user_id": str(item["wa_user_id"])[:100],
                    "message_type": str(item["message_type"])[:40],
                    "text": str(item.get("text") or "")[:20_000],
                    "payload_json": json.dumps(
                        item.get("payload") or {},
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                }
                for item in messages
            ],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        insert_sql = _postgres_sql(
            "WITH incoming AS ("
            " SELECT * FROM jsonb_to_recordset(?::jsonb) AS x("
            " message_id text,phone_number_id text,wa_user_id text,"
            " message_type text,text text,payload_json text)"
            ") INSERT INTO whatsapp_messages("
            " message_id,user_id,phone_number_id,wa_user_id,message_type,text,payload_json,"
            " status,next_attempt_at,created_at,updated_at) "
            "SELECT i.message_id,CASE WHEN u.id IS NOT NULL THEN l.user_id END,"
            "i.phone_number_id,i.wa_user_id,"
            "i.message_type,i.text,i.payload_json,'pending',?,?,? FROM incoming i "
            "LEFT JOIN whatsapp_links l ON l.wa_user_id=i.wa_user_id "
            " AND l.phone_number_id=i.phone_number_id "
            "LEFT JOIN users u ON u.id=l.user_id AND u.account_status='active' "
            "ON CONFLICT(message_id) DO NOTHING RETURNING message_id"
        )
        claim_sql = _postgres_sql(
            "WITH candidate AS ("
            " SELECT m.message_id FROM whatsapp_messages m "
            " WHERE m.status='pending' AND m.next_attempt_at<=? AND NOT EXISTS ("
            " SELECT 1 FROM whatsapp_messages earlier "
            " WHERE earlier.phone_number_id=m.phone_number_id "
            " AND earlier.wa_user_id=m.wa_user_id "
            " AND earlier.status IN ('pending','processing','sending') AND ("
            " earlier.created_at<m.created_at OR "
            " (earlier.created_at=m.created_at AND earlier.message_id<m.message_id))) "
            " ORDER BY m.created_at,m.message_id FOR UPDATE SKIP LOCKED LIMIT 1"
            "), claimed AS ("
            " UPDATE whatsapp_messages m SET status='processing',"
            " attempts=m.attempts+1,updated_at=? FROM candidate "
            " WHERE m.message_id=candidate.message_id RETURNING m.*"
            ") SELECT claimed.*,to_jsonb(l) AS context_link_json,"
            "to_jsonb(u) AS context_user_json,"
            "gs.id AS context_provider_subscription_id,"
            "gs.api_key_enc AS context_provider_api_key_enc,"
            "gs.key_id AS context_provider_key_id,"
            "gs.key_version AS context_provider_key_version,"
            "gs.status AS context_provider_subscription_status,"
            "gs.assigned_user_id AS context_provider_assigned_user_id,"
            "ast.user_id AS context_state_user_id,"
            "ast.revision AS context_state_revision,"
            "ast.state_json AS context_state_json,"
            "ast.created_at AS context_state_created_at,"
            "ast.updated_at AS context_state_updated_at "
            "FROM claimed "
            "LEFT JOIN whatsapp_links l ON l.wa_user_id=claimed.wa_user_id "
            " AND l.phone_number_id=claimed.phone_number_id "
            "LEFT JOIN users u ON u.id=l.user_id AND u.account_status='active' "
            "LEFT JOIN go_subscriptions gs ON gs.id=u.subscription_id "
            "LEFT JOIN account_states ast ON ast.user_id=u.id"
        )

        for attempt in range(2):
            connection = self._pool.getconn(timeout=10)
            try:
                with connection.pipeline():
                    inserted_cursor = connection.execute(
                        insert_sql, (encoded, now, now, now)
                    )
                    claimed_cursor = connection.execute(claim_sql, (now, now))
                    connection.execute("COMMIT")
                inserted = inserted_cursor.fetchall()
                row = claimed_cursor.fetchone()
                if row is None:
                    return len(inserted), None
                message = dict(row)
                message["_processing_context"] = self._whatsapp_context_from_row(
                    row, prefix="context_"
                )
                return len(inserted), message
            except self._operational_error:
                try:
                    connection.rollback()
                except Exception:
                    pass
                if attempt:
                    raise
            except Exception:
                try:
                    connection.rollback()
                except Exception:
                    pass
                raise
            finally:
                self._pool.putconn(connection)
        raise AssertionError("unreachable")

    def get_whatsapp_processing_context(
        self, *, wa_user_id: str, phone_number_id: str
    ) -> dict:
        """Load link, user and synchronized product state in one DB call."""
        row = self._one(
            "SELECT to_jsonb(l) AS link_json,to_jsonb(u) AS user_json,"
            "gs.id AS provider_subscription_id,"
            "gs.api_key_enc AS provider_api_key_enc,"
            "gs.key_id AS provider_key_id,"
            "gs.key_version AS provider_key_version,"
            "gs.status AS provider_subscription_status,"
            "gs.assigned_user_id AS provider_assigned_user_id,"
            "ast.user_id AS state_user_id,ast.revision AS state_revision,"
            "ast.state_json,ast.created_at AS state_created_at,"
            "ast.updated_at AS state_updated_at "
            "FROM whatsapp_links l JOIN users u ON u.id=l.user_id "
            "LEFT JOIN go_subscriptions gs ON gs.id=u.subscription_id "
            "LEFT JOIN account_states ast ON ast.user_id=u.id "
            "WHERE l.wa_user_id=? AND l.phone_number_id=? "
            "AND u.account_status='active'",
            (wa_user_id, phone_number_id),
        )
        if row is None:
            return {"link": None, "user": None, "account_state": None}
        return self._whatsapp_context_from_row(row)

    @staticmethod
    def _whatsapp_context_from_row(
        row: Any, *, prefix: str = ""
    ) -> dict:
        def decoded(value: Any) -> dict:
            if isinstance(value, dict):
                return dict(value)
            if isinstance(value, str):
                parsed = json.loads(value)
                return dict(parsed) if isinstance(parsed, dict) else {}
            return {}

        link = decoded(row.get(f"{prefix}link_json"))
        user = decoded(row.get(f"{prefix}user_json"))
        if not user:
            return {"link": None, "user": None, "account_state": None}
        for field in (
            "provider_subscription_id",
            "provider_api_key_enc",
            "provider_key_id",
            "provider_key_version",
            "provider_subscription_status",
            "provider_assigned_user_id",
        ):
            value = row.get(f"{prefix}{field}")
            if value is not None:
                user[field] = value

        account_state = None
        if row.get(f"{prefix}state_user_id") is not None:
            account_state = {
                "user_id": row[f"{prefix}state_user_id"],
                "revision": row[f"{prefix}state_revision"],
                "state_json": row[f"{prefix}state_json"],
                "created_at": row[f"{prefix}state_created_at"],
                "updated_at": row[f"{prefix}state_updated_at"],
            }
        return {
            "link": link,
            "user": user,
            "account_state": account_state,
        }

    def append_account_state_messages(
        self,
        *,
        user_id: str,
        bot_id: str,
        messages: list[dict],
        base_revision: int,
        device_hash: str,
        delivery_message_id: str | None = None,
        delivery_result_text: str = "",
    ) -> dict:
        """Append one turn to a bot with one cross-region Postgres call.

        The generic account-state writer intentionally validates tombstones and
        arbitrary client edits. WhatsApp only appends two already-normalized
        messages to an existing bot, so rewriting the complete 100+ KB account
        snapshot through several SQL round trips is unnecessary. This update
        retains optimistic concurrency, message-id deduplication, ordering and
        the product's 200-message cap.
        """
        now = time.time()
        messages_json = json.dumps(
            messages, separators=(",", ":"), ensure_ascii=False
        )
        state_update = (
            "UPDATE account_states ast SET state_json=("
            " jsonb_set(jsonb_set(ast.state_json::jsonb,'{bots}',("
            "  SELECT COALESCE(jsonb_agg(CASE WHEN bot->>'id'=? THEN "
            "   jsonb_set(bot,'{messages}',("
            "    SELECT COALESCE(jsonb_agg(recent.value ORDER BY recent.ord),'[]'::jsonb) FROM ("
            "     SELECT combined.value,combined.ord FROM jsonb_array_elements("
            "      COALESCE(bot->'messages','[]'::jsonb)||COALESCE(("
            "       SELECT jsonb_agg(incoming.value) FROM jsonb_array_elements(?::jsonb) incoming(value)"
            "       WHERE NOT EXISTS (SELECT 1 FROM jsonb_array_elements("
            "        COALESCE(bot->'messages','[]'::jsonb)) existing(value)"
            "        WHERE existing.value->>'id'=incoming.value->>'id')"
            "      ),'[]'::jsonb)"
            "     ) WITH ORDINALITY combined(value,ord)"
            "     ORDER BY combined.ord DESC LIMIT 200"
            "    ) recent"
            "   ),true) ELSE bot END ORDER BY ordinal),'[]'::jsonb)"
            "  FROM jsonb_array_elements(COALESCE(ast.state_json::jsonb->'bots','[]'::jsonb))"
            "  WITH ORDINALITY listed(bot,ordinal)"
            " ),false),'{activeBotId}',to_jsonb(?::text),true)"
            ")::text,revision=ast.revision+1,updated_by_device_hash=?,updated_at=? "
            "WHERE ast.user_id=? AND ast.revision=? AND EXISTS ("
            " SELECT 1 FROM jsonb_array_elements(COALESCE(ast.state_json::jsonb->'bots','[]'::jsonb)) item"
            " WHERE item->>'id'=?) "
            "RETURNING user_id,revision,state_json,created_at,updated_at"
        )
        params: tuple[Any, ...] = (
            bot_id,
            messages_json,
            bot_id,
            device_hash,
            now,
            user_id,
            base_revision,
            bot_id,
        )
        if delivery_message_id:
            row = self._one(
                "WITH updated_state AS (" + state_update + "),claimed_delivery AS ("
                " UPDATE whatsapp_messages SET status='sending',result_text=?,"
                " user_id=COALESCE(?,user_id),updated_at=? "
                " WHERE message_id=? AND status='processing' "
                " AND EXISTS(SELECT 1 FROM updated_state) RETURNING message_id"
                ") SELECT updated_state.*,"
                "EXISTS(SELECT 1 FROM claimed_delivery) AS delivery_prepared "
                "FROM updated_state",
                params + (
                    delivery_result_text[:20_000],
                    user_id,
                    now,
                    delivery_message_id,
                ),
            )
        else:
            row = self._one(state_update, params)
        if row is not None:
            if delivery_message_id and row.get("delivery_prepared") is not True:
                raise RuntimeError("whatsapp_delivery_already_claimed")
            return dict(row)

        current = self.get_account_state(user_id)
        if current is None or int(current.get("revision") or 0) != base_revision:
            raise AccountStateConflict(current or {
                "user_id": user_id,
                "revision": 0,
                "state_json": "",
                "created_at": now,
                "updated_at": now,
            })
        raise RuntimeError("El agente de WhatsApp ya no existe")

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
        """Reserve a metered run in one cross-region database call."""
        now = time.time()
        try:
            row = self._one(
                "SELECT * FROM reserve_agent_run(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    user_id,
                    idempotency_key,
                    model,
                    int(browser),
                    max_credit_milli,
                    max_concurrent_runs,
                    token_hash,
                    token_expires_at,
                    bool(enforce),
                    five_hour_credit_milli,
                    seven_day_credit_milli,
                    new_id("run"),
                    new_id("rsv"),
                    new_id("led"),
                    now,
                ),
            )
        except Exception as exc:
            undefined_function = getattr(self, "_undefined_function_error", ())
            if undefined_function and isinstance(exc, undefined_function):
                # Rolling-deploy bridge: code can safely reach production
                # before schema v23, using the previous transaction until the
                # additive function migration is applied.
                return super().create_agent_run(
                    user_id=user_id,
                    idempotency_key=idempotency_key,
                    model=model,
                    browser=browser,
                    max_credit_milli=max_credit_milli,
                    max_concurrent_runs=max_concurrent_runs,
                    token_hash=token_hash,
                    token_expires_at=token_expires_at,
                    enforce=enforce,
                    five_hour_credit_milli=five_hour_credit_milli,
                    seven_day_credit_milli=seven_day_credit_milli,
                )
            raise
        if row is None:
            raise RuntimeError("agent_run_reservation_failed")
        if row.get("outcome") == "error":
            raise RuntimeError(str(row.get("error_code") or "agent_run_reservation_failed"))
        return {
            "duplicate": row["outcome"] == "duplicate",
            "run": dict(row["run"]),
        }

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
            "), retired AS ("
            " UPDATE agent_runs SET idempotency_key=idempotency_key||':retired:'||id "
            " WHERE user_id=? AND idempotency_key=? "
            " AND status IN ('failed','cancelled','expired','budget_exhausted') RETURNING id"
            "), existing AS MATERIALIZED ("
            " SELECT ar.* FROM agent_runs ar,locked "
            " WHERE ar.user_id=? AND ar.idempotency_key=? "
            " AND (SELECT COUNT(*) FROM retired)>=0"
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
        result: dict | None = None,
    ) -> dict:
        """Finish an unlimited run and revoke its token in one statement."""
        now = time.time()
        encoded_result = None
        if result is not None:
            encoded_result = json.dumps(
                result, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
            if len(encoded_result.encode("utf-8")) > 1_000_000:
                raise ValueError("El resultado durable excede 1 MB")
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
            " warnings_json=?,result_json=COALESCE(?,result_json),finished_at=?,heartbeat_at=? "
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
                encoded_result,
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
            ready = bool(
                row and int(row["v"]) in {SCHEMA_VERSION - 1, SCHEMA_VERSION}
            )
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
        connection = getattr(self, "_conn", None)
        if connection is not None:
            connection.close()
        pool = getattr(self, "_pool", None)
        if pool is not None:
            pool.close()


def create_store(*, database_url: str | None, db_path: Any) -> Store:
    if database_url:
        return PostgresStore(database_url)
    return Store(db_path)
