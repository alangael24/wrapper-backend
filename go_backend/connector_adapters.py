"""Gateway privado de conectores de Agent Genia.

Todo el consentimiento, descubrimiento y uso de herramientas ocurre en este
proceso. Electron conserva solamente la sesion de Agent Genia y una marca local
de UX; Pi recibe un grant efimero sin credenciales. Composio conserva y renueva
los tokens OAuth de cada usuario.
"""

from __future__ import annotations

import json
import jsonschema
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .connectors import CONNECTOR_CATALOG, ConnectorBrokerError
from .native_connectors import NativeConnectorGateway


ACTIVE = "ACTIVE"
TERMINAL_CONNECTION_STATES = frozenset({"FAILED", "EXPIRED", "REVOKED", "DELETED"})
AUTH_ATTEMPT_TTL_SECONDS = 10 * 60
AUTH_STARTS_PER_MINUTE = 12
UPSTREAM_POLL_INTERVAL_SECONDS = 2.0
# OAuth completion and disconnect explicitly invalidate this cache. Keeping a
# healthy snapshot for one minute removes a third-party round trip from bursts
# of tool turns without delaying connection changes made through Agent Genia.
CONNECTION_SNAPSHOT_TTL_SECONDS = 60.0

# Nombres verificados contra el catalogo de toolkits de Composio. Los
# overrides de entorno permiten incorporar o renombrar toolkits sin publicar
# una nueva app de escritorio.
COMPOSIO_TOOLKITS: dict[str, str] = {
    # Composio's public toolkit slug is ``googlesuper`` (without an
    # underscore). ``google_super`` is rejected by Tool Router v2 before the
    # user can reach Google's OAuth screen.
    "google-workspace": "googlesuper",
    "slack": "slack",
    "notion": "notion",
    "salesforce": "salesforce",
    "microsoft-365": "outlook",
    "linkedin": "linkedin",
    "zoom": "zoom",
    "github": "github",
    "jira": "jira",
    "linear": "linear",
    "asana": "asana",
    "clickup": "clickup",
    "figma": "figma",
    "hubspot": "hubspot",
    "canva": "canva",
    "trello": "trello",
    "monday-com": "monday",
    "intercom": "intercom",
    "zendesk": "zendesk",
    "box": "box",
    "dropbox": "dropbox",
    "docusign": "docusign",
    "calendly": "calendly",
    "outreach": "outreach",
    "apollo": "apollo",
    "clay": "clay",
    "zoominfo": "zoominfo",
    "stripe": "stripe",
    "quickbooks": "quickbooks",
    "netsuite": "netsuite",
    "ramp": "ramp",
    "workday": "workday",
    "ashby": "ashby",
    "greenhouse": "greenhouse",
    "vercel": "vercel",
    "tableau": "tableau",
    "hex": "hex",
    "amplitude": "amplitude",
    "mixpanel": "mixpanel",
    "snowflake": "snowflake",
    "databricks": "databricks",
    "mailchimp": "mailchimp",
    "shopify": "shopify",
    "woocommerce": "woocommerce",
}

# Tool Router search is useful for broad discovery, but write operations must
# not depend on whichever result happens to rank first. These slugs are pinned
# to the public Composio action documented for the corresponding Agent Genia
# operation.
COMPOSIO_OPERATION_TO_TOOL: dict[tuple[str, str], str] = {
    ("google-workspace", "draft_email"): "GOOGLESUPER_CREATE_EMAIL_DRAFT",
    ("google-workspace", "send_email"): "GOOGLESUPER_SEND_EMAIL",
    ("google-workspace", "create_calendar_event"): "GOOGLESUPER_CREATE_EVENT",
    ("google-workspace", "delete_calendar_event"): "GOOGLESUPER_DELETE_EVENT",
    ("github", "read_file"): "GITHUB_GET_REPOSITORY_CONTENT",
    ("snowflake", "select_query"): "SNOWFLAKE_EXECUTE_SQL",
    ("snowflake", "execute_sql"): "SNOWFLAKE_EXECUTE_SQL",
    ("databricks", "select_query"): "DATABRICKS_SQL_STATEMENT_EXEC_EXECUTE_STATEMENT",
    ("databricks", "execute_sql"): "DATABRICKS_SQL_STATEMENT_EXEC_EXECUTE_STATEMENT",
}

_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?$"
)
_SQL_FORBIDDEN_READ_TOKENS = frozenset({
    "alter", "begin", "call", "commit", "copy", "create", "delete", "drop",
    "execute", "grant", "insert", "merge", "put", "remove", "replace",
    "revoke", "rollback", "truncate", "undrop", "update", "use",
})

# Estos proveedores no tienen una app administrada estable para todos los
# proyectos. Se habilitan solo cuando el servidor recibe un ac_... propio.
CUSTOM_AUTH_REQUIRED = frozenset({
    "salesforce", "docusign", "outreach", "clay", "zoominfo", "netsuite",
    "ramp", "workday", "tableau", "snowflake", "woocommerce",
})


class ComposioConnectorGateway:
    """Administra Connect Links y ejecuciones, siempre aisladas por user_id."""

    def __init__(
        self,
        *,
        api_key: str = "",
        public_base_url: str = "",
        auth_configs: dict[str, str] | None = None,
        direct_auth_configs: dict[str, str] | None = None,
        toolkit_overrides: dict[str, str] | None = None,
        client: Any = None,
        native_gateway: NativeConnectorGateway | None = None,
        store: Any = None,
        now=time.time,
        attempt_ttl_seconds: int = AUTH_ATTEMPT_TTL_SECONDS,
    ):
        self.api_key = api_key.strip()
        self.public_base_url = _validate_public_base_url(public_base_url)
        self.auth_configs = _validated_mapping(
            auth_configs or {},
            prefix="ac_",
            name="COMPOSIO_AUTH_CONFIGS_JSON",
        )
        self.direct_auth_configs = _validated_mapping(
            direct_auth_configs or {},
            prefix="ac_",
            name="COMPOSIO_DIRECT_AUTH_CONFIGS_JSON",
        )
        for key, auth_config in self.direct_auth_configs.items():
            if self.auth_configs.get(key) != auth_config:
                raise ValueError(
                    "COMPOSIO_DIRECT_AUTH_CONFIGS_JSON solo puede habilitar "
                    "Auth Configs presentes con el mismo valor en "
                    "COMPOSIO_AUTH_CONFIGS_JSON"
                )
        self.toolkits = dict(COMPOSIO_TOOLKITS)
        self.toolkits.update(_validated_toolkit_overrides(toolkit_overrides or {}))
        self._now = now
        self._attempt_ttl_seconds = max(60, min(int(attempt_ttl_seconds), 1800))
        self.store = store
        self._start_rate: dict[str, deque[float]] = defaultdict(deque)
        self._snapshot_cache: dict[str, tuple[float, tuple[dict[str, Any], ...]]] = {}
        self._operation_cache: dict[tuple[str, str], tuple[float, str]] = {}
        self._lock = threading.RLock()
        self.client = client
        self.native_gateway = native_gateway
        if self.client is None and self.api_key:
            try:
                from composio import Composio
            except ImportError as exc:  # pragma: no cover - guard de despliegue
                raise RuntimeError(
                    "COMPOSIO_API_KEY requiere instalar composio desde requirements.txt"
                ) from exc
            self.client = Composio(api_key=self.api_key)
        if (self.client is not None or native_gateway is not None) and self.store is None:
            raise ValueError("El gateway de conectores requiere un Store persistente")

    @property
    def configured(self) -> bool:
        return self.client is not None

    def health(self) -> dict[str, Any]:
        unavailable = [
            connector_id
            for connector_id in CONNECTOR_CATALOG
            if not self.describe(connector_id)["available"]
        ]
        return {
            "configured": self.configured or bool(self.native_gateway and self.native_gateway.configured),
            "available_connectors": len(CONNECTOR_CATALOG) - len(unavailable),
            "catalog_connectors": len(CONNECTOR_CATALOG),
            "all_connectors_available": not unavailable,
            "unavailable_connectors": unavailable,
        }

    def describe(self, connector_id: str) -> dict[str, Any]:
        if connector_id not in CONNECTOR_CATALOG:
            raise ConnectorBrokerError(404, "Conector desconocido", "connector_not_found")
        toolkit = self.toolkits.get(connector_id, "")
        if not self.configured:
            reason = "El gateway privado de Composio no esta configurado."
        elif not toolkit:
            reason = "Composio no publica un toolkit compatible para este conector."
        elif connector_id in CUSTOM_AUTH_REQUIRED and not self._auth_config(connector_id, toolkit):
            reason = "Este conector requiere un Auth Config propio en Composio."
        else:
            reason = ""
        if reason and self.native_gateway and self.native_gateway.supports(connector_id):
            return self.native_gateway.describe(connector_id)
        return {
            "connector_id": connector_id,
            "toolkit": toolkit,
            "driver": "composio",
            "available": not reason,
            "reason": reason,
        }

    def status(self, user_id: str, connector_id: str) -> dict[str, Any]:
        description = self.describe(connector_id)
        if description.get("driver") == "native":
            return self.native_gateway.status(user_id, connector_id)  # type: ignore[union-attr]
        if not description["available"]:
            return {**description, "connected": False, "account": ""}
        cached = self._cached_snapshot(user_id)
        if cached is not None:
            for item in cached:
                if item["connector_id"] == connector_id:
                    return item
        accounts = self.client.connected_accounts.list(
            user_ids=[user_id],
            toolkit_slugs=[description["toolkit"]],
            statuses=[ACTIVE],
            limit=20,
        )
        items = list(getattr(accounts, "items", []) or [])
        account = items[0] if items else None
        return {
            **description,
            "connected": account is not None,
            "account": _account_label(account),
        }

    def snapshot(self, user_id: str) -> list[dict[str, Any]]:
        """Devuelve todo el catalogo usando una sola consulta a Composio."""
        cached = self._cached_snapshot(user_id)
        if cached is not None:
            return cached
        descriptions = [self.describe(connector_id) for connector_id in CONNECTOR_CATALOG]
        if not self.configured:
            snapshot = [
                self.native_gateway.status(user_id, item["connector_id"])
                if item.get("driver") == "native" and self.native_gateway
                else {**item, "connected": False, "account": ""}
                for item in descriptions
            ]
            self._store_snapshot(user_id, snapshot)
            return snapshot
        try:
            accounts_page = self.client.connected_accounts.list(
                user_ids=[user_id],
                statuses=[ACTIVE],
                limit=100,
            )
            accounts = list(getattr(accounts_page, "items", []) or [])
        except Exception as exc:
            raise _upstream_error(exc, "No se pudo consultar las cuentas conectadas") from exc
        by_toolkit: dict[str, Any] = {}
        for account in accounts:
            toolkit = _normalized_toolkit_slug(_account_toolkit(account))
            if toolkit and toolkit not in by_toolkit:
                by_toolkit[toolkit] = account
        snapshot: list[dict[str, Any]] = []
        for item in descriptions:
            if item.get("driver") == "native":
                snapshot.append(self.native_gateway.status(user_id, item["connector_id"]))  # type: ignore[union-attr]
            else:
                toolkit = _normalized_toolkit_slug(item["toolkit"])
                snapshot.append({
                    **item,
                    "connected": item["available"] and toolkit in by_toolkit,
                    "account": _account_label(by_toolkit.get(toolkit)),
                })
        self._store_snapshot(user_id, snapshot)
        return snapshot

    def connected_connector_ids(self, user_id: str) -> tuple[str, ...]:
        """Lista autoritativa usada al emitir un grant, no una preferencia del cliente."""
        return tuple(
            item["connector_id"]
            for item in self.snapshot(user_id)
            if item.get("connected") is True
        )

    def start(self, user_id: str, connector_id: str) -> dict[str, str]:
        self._check_start_rate(user_id)
        self._invalidate_snapshot(user_id)
        description = self.describe(connector_id)
        if not description["available"]:
            raise ConnectorBrokerError(409, description["reason"], "connector_not_configured")
        self.store.prune_auth_attempts()
        if description.get("driver") == "native":
            return self.native_gateway.start(user_id, connector_id)  # type: ignore[union-attr]
        session = None
        callback_url = (
            f"{self.public_base_url}/connections/complete" if self.public_base_url else None
        )
        try:
            auth_config = self._auth_config(connector_id, description["toolkit"])
            if auth_config:
                request = self._authorize_with_auth_config(
                    user_id=user_id,
                    auth_config=auth_config,
                    callback_url=callback_url,
                    direct=(
                        self._direct_auth_config(connector_id, description["toolkit"])
                        == auth_config
                    ),
                )
            else:
                session = self._session(user_id, connector_id)
                request = session.authorize(
                    description["toolkit"],
                    **({"callback_url": callback_url} if callback_url else {}),
                )
            account_id = str(getattr(request, "id", "") or "")
            authorize_url = _safe_authorize_url(str(getattr(request, "redirect_url", "") or ""))
            if not account_id:
                raise ValueError("Composio no devolvio connected_account_id")
        except ConnectorBrokerError:
            _delete_session(session)
            raise
        except Exception as exc:
            _delete_session(session)
            raise _upstream_error(exc, "No se pudo crear el enlace de autorizacion") from exc
        attempt_id = secrets.token_urlsafe(24)
        _delete_session(session)
        self.store.create_connector_auth_attempt(
            attempt_id=attempt_id,
            user_id=user_id,
            connector_id=connector_id,
            driver="composio",
            connected_account_id=account_id,
            expires_at=self._now() + self._attempt_ttl_seconds,
        )
        return {"attempt_id": attempt_id, "authorize_url": authorize_url}

    def poll(self, user_id: str, attempt_id: str) -> dict[str, Any]:
        if attempt_id.startswith("nat_") and self.native_gateway:
            result = self.native_gateway.poll(user_id, attempt_id)
            if result.get("status") == "complete":
                self._invalidate_snapshot(user_id)
            return result
        attempt, should_poll = self.store.claim_connector_poll(
            attempt_id,
            user_id,
            UPSTREAM_POLL_INTERVAL_SECONDS,
            now=self._now(),
        )
        if attempt is None:
            raise ConnectorBrokerError(
                404, "Conexion desconocida o expirada", "connector_auth_not_found"
            )
        if attempt["status"] in {"complete", "error"}:
            return self._consume_terminal_attempt(user_id, attempt_id)
        if not should_poll:
            return {"status": "pending"}
        try:
            account = self.client.connected_accounts.get(attempt["connected_account_id"])
            state = str(getattr(account, "status", "") or "").upper()
        except Exception as exc:
            raise _upstream_error(exc, "No se pudo consultar la autorizacion") from exc
        if state == ACTIVE:
            self._invalidate_snapshot(user_id)
            self.store.finish_connector_auth_attempt(
                attempt_id=attempt_id,
                status="complete",
                account_label=_account_label(account) or attempt["connected_account_id"],
            )
            return self._consume_terminal_attempt(user_id, attempt_id)
        if state in TERMINAL_CONNECTION_STATES:
            self.store.finish_connector_auth_attempt(
                attempt_id=attempt_id,
                status="error",
                message=str(getattr(account, "status_reason", "") or "El proveedor rechazo la conexion"),
            )
            return self._consume_terminal_attempt(user_id, attempt_id)
        return {"status": "pending"}

    def _consume_terminal_attempt(self, user_id: str, attempt_id: str) -> dict[str, Any]:
        attempt = self.store.consume_connector_auth_attempt(attempt_id, user_id)
        if attempt is None:
            raise ConnectorBrokerError(
                404, "Conexion desconocida o ya consumida", "connector_auth_not_found"
            )
        if attempt["status"] == "error":
            return {"status": "error", "message": attempt.get("message") or "El proveedor rechazó la conexión"}
        return {
            "status": "complete",
            "session": {
                "managed_connection_id": attempt["connected_account_id"],
                "connector_id": attempt["connector_id"],
                "account_label": attempt.get("account_label") or attempt["connected_account_id"],
            },
        }

    def disconnect(self, user_id: str, connector_id: str) -> dict[str, bool]:
        self._invalidate_snapshot(user_id)
        description = self.describe(connector_id)
        native_result = None
        if self.native_gateway and self.native_gateway.supports(connector_id):
            native_result = self.native_gateway.disconnect(user_id, connector_id)
        if not description["toolkit"] or not self.configured:
            return native_result or {"disconnected": True}
        try:
            accounts = self.client.connected_accounts.list(
                user_ids=[user_id],
                toolkit_slugs=[description["toolkit"]],
                statuses=[ACTIVE],
                limit=100,
            )
            for account in list(getattr(accounts, "items", []) or []):
                account_id = str(getattr(account, "id", "") or "")
                if account_id:
                    self.client.connected_accounts.delete(account_id)
            self._invalidate_snapshot(user_id)
        except Exception as exc:
            raise _upstream_error(exc, "No se pudo desconectar la cuenta") from exc
        return {"disconnected": True}

    def disconnect_all(self, user_id: str) -> int:
        """Revoca las cuentas administradas del usuario durante incident response."""
        if not self.configured:
            return 0
        try:
            accounts = self.client.connected_accounts.list(
                user_ids=[user_id], statuses=[ACTIVE], limit=100
            )
            deleted = 0
            for account in list(getattr(accounts, "items", []) or []):
                account_id = str(getattr(account, "id", "") or "")
                if account_id:
                    self.client.connected_accounts.delete(account_id)
                    deleted += 1
            self._invalidate_snapshot(user_id)
            return deleted
        except Exception as exc:
            raise _upstream_error(exc, "No se pudieron revocar las cuentas conectadas") from exc

    def execute(
        self,
        user_id: str,
        connector_id: str,
        operation: str,
        arguments: dict[str, Any],
    ) -> Any:
        description = self.describe(connector_id)
        if not description["available"]:
            raise ConnectorBrokerError(409, description["reason"], "connector_not_configured")
        if description.get("driver") == "native":
            return self.native_gateway.execute(user_id, connector_id, operation, arguments)  # type: ignore[union-attr]
        normalized_arguments = _normalize_operation_arguments(
            connector_id, operation, arguments
        )
        session = self._session(user_id, connector_id)
        try:
            slug, search = self._resolve_operation(session, connector_id, operation)
            normalized_arguments = _validated_operation_arguments(
                connector_id, operation, normalized_arguments, search, slug
            )
            logging.info(
                "Executing connector operation connector=%s operation=%s tool=%s",
                connector_id,
                operation,
                slug,
            )
            result = session.execute(slug, arguments=normalized_arguments)
            error = getattr(result, "error", None)
            if error:
                logging.warning(
                    "Connector provider rejected operation connector=%s operation=%s tool=%s: %s",
                    connector_id,
                    operation,
                    slug,
                    error,
                )
                raise ConnectorBrokerError(
                    502,
                    "El proveedor rechazo la operacion",
                    "connector_upstream_error",
                )
            return _json_value(getattr(result, "data", result))
        except ConnectorBrokerError:
            raise
        except Exception as exc:
            raise _upstream_error(exc, "El proveedor rechazo la operacion") from exc
        finally:
            _delete_session(session)

    def validate_arguments(
        self,
        user_id: str,
        connector_id: str,
        operation: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve the current provider schema without executing the action."""
        description = self.describe(connector_id)
        if not description["available"]:
            raise ConnectorBrokerError(409, description["reason"], "connector_not_configured")
        if description.get("driver") == "native":
            if self.native_gateway is None:
                raise ConnectorBrokerError(409, "Conector first-party no disponible", "connector_not_configured")
            return self.native_gateway.validate_arguments(connector_id, operation, arguments)
        normalized_arguments = _normalize_operation_arguments(
            connector_id, operation, arguments
        )
        session = self._session(user_id, connector_id)
        try:
            slug, search = self._resolve_operation(session, connector_id, operation)
            return _validated_operation_arguments(
                connector_id,
                operation,
                normalized_arguments,
                search,
                slug,
                require_schema=True,
            )
        finally:
            _delete_session(session)

    def normalize_arguments(
        self, connector_id: str, operation: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Canonicalize aliases locally without contacting the provider."""
        description = self.describe(connector_id)
        if description.get("driver") == "native":
            if self.native_gateway is None:
                raise ConnectorBrokerError(409, "Conector first-party no disponible", "connector_not_configured")
            return self.native_gateway.validate_arguments(connector_id, operation, arguments)
        return _normalize_operation_arguments(connector_id, operation, arguments)

    def resolvable_operations(
        self, user_id: str, connector_id: str, operations: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Return only operations backed by the provider's current catalog."""
        description = self.describe(connector_id)
        if not description["available"]:
            return ()
        if description.get("driver") == "native":
            return operations
        with self._lock:
            cached = {
                operation: self._operation_cache.get((connector_id, operation))
                for operation in operations
            }
        if cached and all(
            value is not None and value[0] > self._now()
            for value in cached.values()
        ):
            return tuple(
                operation
                for operation in operations
                if cached[operation] is not None and bool(cached[operation][1])
            )
        session = self._session(user_id, connector_id)
        try:
            search = session.search(
                query=(
                    f"Resolve these exact {CONNECTOR_CATALOG[connector_id]['name']} "
                    f"operations: {', '.join(operations)}."
                )
            )
            results = list(getattr(search, "results", []) or [])
            schemas = getattr(search, "tool_schemas", None)
            available_slugs = {
                str(raw).upper()
                for result in results
                for raw in list(getattr(result, "primary_tool_slugs", []) or [])
                if isinstance(raw, str) and raw
            }
            if isinstance(schemas, dict):
                available_slugs.update(str(key).upper() for key in schemas)
            resolved: list[str] = []
            for operation in operations:
                pinned = COMPOSIO_OPERATION_TO_TOOL.get((connector_id, operation))
                slug = pinned or _select_composio_tool_slug(
                    connector_id, operation, results
                )
                if not slug or (
                    pinned and available_slugs and pinned.upper() not in available_slugs
                ):
                    logging.warning(
                        "Hiding unresolved connector operation connector=%s operation=%s",
                        connector_id,
                        operation,
                    )
                    with self._lock:
                        self._operation_cache[(connector_id, operation)] = (
                            self._now() + 300,
                            "",
                        )
                    continue
                resolved.append(operation)
                with self._lock:
                    self._operation_cache[(connector_id, operation)] = (
                        self._now() + 900,
                        slug,
                    )
            return tuple(resolved)
        finally:
            _delete_session(session)

    def _resolve_operation(
        self, session: Any, connector_id: str, operation: str
    ) -> tuple[str, Any]:
        with self._lock:
            cached = self._operation_cache.get((connector_id, operation))
        if cached is not None and cached[0] > self._now() and not cached[1]:
            raise ConnectorBrokerError(
                404,
                "No encontramos una acción inequívoca para esta operación",
                "connector_operation_not_found",
            )
        search = session.search(
            query=(
                f"Use {CONNECTOR_CATALOG[connector_id]['name']} to perform the "
                f"operation '{operation}'."
            )
        )
        results = list(getattr(search, "results", []) or [])
        pinned = COMPOSIO_OPERATION_TO_TOOL.get((connector_id, operation))
        available_slugs = {
            str(raw).upper()
            for result in results
            for raw in list(getattr(result, "primary_tool_slugs", []) or [])
            if isinstance(raw, str) and raw
        }
        schemas = getattr(search, "tool_schemas", None)
        if isinstance(schemas, dict):
            available_slugs.update(str(key).upper() for key in schemas)
        slug = pinned or _select_composio_tool_slug(connector_id, operation, results)
        if not slug:
            raise ConnectorBrokerError(
                404,
                "No encontramos una acción inequívoca para esta operación",
                "connector_operation_not_found",
            )
        with self._lock:
            self._operation_cache[(connector_id, operation)] = (
                self._now() + 900,
                slug,
            )
        return slug, search

    def _session(self, user_id: str, connector_id: str) -> Any:
        toolkit = self.toolkits[connector_id]
        auth_config = self._auth_config(connector_id, toolkit)
        options: dict[str, Any] = {
            "user_id": user_id,
            "toolkits": [toolkit],
            "manage_connections": False,
            "workbench": {"enable": False},
        }
        if auth_config:
            options["auth_configs"] = {toolkit: auth_config}
        try:
            return self.client.sessions.create(**options)
        except Exception as exc:
            raise _upstream_error(exc, "No se pudo crear la sesion del conector") from exc

    def _auth_config(self, connector_id: str, toolkit: str) -> str:
        return self.auth_configs.get(connector_id) or self.auth_configs.get(toolkit, "")

    def _direct_auth_config(self, connector_id: str, toolkit: str) -> str:
        return (
            self.direct_auth_configs.get(connector_id)
            or self.direct_auth_configs.get(toolkit, "")
        )

    def _authorize_with_auth_config(
        self,
        *,
        user_id: str,
        auth_config: str,
        callback_url: str | None,
        direct: bool,
    ) -> Any:
        """Use Connect Link by default; direct OAuth requires an explicit opt-in."""
        options = {"callback_url": callback_url} if callback_url else {}
        if not direct:
            return self.client.connected_accounts.link(user_id, auth_config, **options)
        try:
            # Only operator-verified custom OAuth apps may use the legacy
            # direct endpoint. Merely appearing in COMPOSIO_AUTH_CONFIGS_JSON
            # is not proof that an Auth Config is custom.
            return self.client.connected_accounts.initiate(user_id, auth_config, **options)
        except Exception as exc:
            # Fail safely if Composio changes an explicitly opted-in config to
            # Managed Auth: retry through the supported v3 Connect Link route.
            if not _requires_connect_link(exc):
                raise
            return self.client.connected_accounts.link(user_id, auth_config, **options)

    def _check_start_rate(self, user_id: str) -> None:
        now = self._now()
        with self._lock:
            bucket = self._start_rate[user_id]
            while bucket and bucket[0] <= now - 60:
                bucket.popleft()
            if len(bucket) >= AUTH_STARTS_PER_MINUTE:
                raise ConnectorBrokerError(
                    429,
                    "Espera un minuto antes de iniciar otra conexion",
                    "connector_rate_limit",
                )
            bucket.append(now)

    def _cached_snapshot(self, user_id: str) -> list[dict[str, Any]] | None:
        with self._lock:
            cached = self._snapshot_cache.get(user_id)
            if cached is None:
                return None
            expires_at, items = cached
            if expires_at <= self._now():
                self._snapshot_cache.pop(user_id, None)
                return None
            return [dict(item) for item in items]

    def _store_snapshot(self, user_id: str, snapshot: list[dict[str, Any]]) -> None:
        with self._lock:
            self._snapshot_cache[user_id] = (
                self._now() + CONNECTION_SNAPSHOT_TTL_SECONDS,
                tuple(dict(item) for item in snapshot),
            )

    def _invalidate_snapshot(self, user_id: str) -> None:
        with self._lock:
            self._snapshot_cache.pop(user_id, None)


class ComposioConnectorAdapter:
    """Adaptador del broker Pi hacia el gateway interno, sin HTTP intermedio."""

    def __init__(self, gateway: ComposioConnectorGateway, connector_id: str):
        if connector_id not in CONNECTOR_CATALOG:
            raise ValueError(f"Conector desconocido: {connector_id}")
        self.gateway = gateway
        self.connector_id = connector_id

    def is_connected(self, user_id: str) -> bool:
        return bool(self.gateway.status(user_id, self.connector_id)["connected"])

    def execute(self, user_id: str, operation: str, arguments: dict[str, Any]) -> Any:
        return self.gateway.execute(user_id, self.connector_id, operation, arguments)

    def normalize_arguments(
        self, operation: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return self.gateway.normalize_arguments(
            self.connector_id, operation, arguments
        )

    def validate_arguments(
        self, user_id: str, operation: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        return self.gateway.validate_arguments(
            user_id, self.connector_id, operation, arguments
        )

    def available_operations(
        self, user_id: str, operations: tuple[str, ...]
    ) -> tuple[str, ...]:
        return self.gateway.resolvable_operations(user_id, self.connector_id, operations)


def parse_config_mapping(raw: str, *, name: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{name} debe ser un objeto JSON") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"{name} debe mapear strings a strings")
    return {key.strip(): item.strip() for key, item in value.items() if key.strip() and item.strip()}


def _validated_mapping(
    value: dict[str, str],
    *,
    prefix: str,
    name: str,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in value.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", key) or not item.startswith(prefix):
            raise ValueError(f"{name} contiene un valor invalido")
        result[key] = item
    return result


def _validated_toolkit_overrides(value: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for connector_id, toolkit in value.items():
        if connector_id not in CONNECTOR_CATALOG or not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", toolkit):
            raise ValueError("COMPOSIO_TOOLKIT_OVERRIDES_JSON contiene un valor invalido")
        result[connector_id] = toolkit
    return result


def _validate_public_base_url(value: str) -> str:
    if not value.strip():
        return ""
    parsed = urlparse(value.strip())
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
    ):
        raise ValueError("COMPOSIO_PUBLIC_URL debe ser HTTPS o loopback HTTP")
    return value.strip().rstrip("/")


def _safe_authorize_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ConnectorBrokerError(502, "Composio devolvio una URL insegura", "connector_auth_failed")
    return value


def _account_label(account: Any) -> str:
    if account is None:
        return ""
    alias = getattr(account, "alias", None)
    if isinstance(alias, str) and alias.strip():
        return alias.strip()[:160]
    data = getattr(account, "data", None)
    if isinstance(data, dict):
        for key in ("email", "name", "username"):
            item = data.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()[:160]
    return str(getattr(account, "id", "") or "")[:160]


def _account_toolkit(account: Any) -> str:
    if account is None:
        return ""
    toolkit = getattr(account, "toolkit", None)
    if isinstance(toolkit, str):
        return toolkit
    if isinstance(toolkit, dict):
        slug = toolkit.get("slug")
        if isinstance(slug, str):
            return slug
    if toolkit is not None:
        slug = getattr(toolkit, "slug", None)
        if isinstance(slug, str):
            return slug
    for key in ("toolkit_slug", "toolkit"):
        value = getattr(account, key, None)
        if isinstance(value, str):
            return value
    data = getattr(account, "data", None)
    if isinstance(data, dict):
        for key in ("toolkit_slug", "toolkit"):
            value = data.get(key)
            if isinstance(value, str):
                return value
    return ""


def _normalized_toolkit_slug(value: str) -> str:
    """Tolerate SDK casing/separators while preserving exact toolkit identity."""
    return re.sub(r"[^a-z0-9]", "", value.strip().lower())


def _select_composio_tool_slug(
    connector_id: str, operation: str, results: list[Any]
) -> str:
    """Choose by action identity, never by whichever search result ranks first."""
    candidates: list[str] = []
    for result in results:
        for raw in list(getattr(result, "primary_tool_slugs", []) or []):
            if isinstance(raw, str) and raw and raw not in candidates:
                candidates.append(raw)
    target = operation.upper()
    exact = [
        slug for slug in candidates
        if slug.upper() == target or slug.upper().endswith("_" + target)
    ]
    if len(exact) == 1:
        return exact[0]
    toolkit = COMPOSIO_TOOLKITS.get(connector_id, "").upper().replace("-", "_")
    toolkit_exact = [slug for slug in exact if slug.upper().startswith(toolkit + "_")]
    if len(toolkit_exact) == 1:
        return toolkit_exact[0]
    operation_tokens = set(target.split("_"))
    scored = []
    for slug in candidates:
        slug_tokens = set(re.split(r"[^A-Z0-9]+", slug.upper()))
        if operation_tokens <= slug_tokens and (
            not toolkit
            or _normalized_toolkit_slug(toolkit) in _normalized_toolkit_slug(slug)
        ):
            scored.append(slug)
    return scored[0] if len(scored) == 1 else ""


def _composio_input_schema(search: Any, slug: str) -> dict[str, Any] | None:
    """Return the exact selected action schema from Tool Router search."""
    schemas = getattr(search, "tool_schemas", None)
    if not isinstance(schemas, dict):
        return None
    selected = next(
        (value for key, value in schemas.items() if str(key).upper() == slug.upper()),
        None,
    )
    if selected is None:
        return None
    schema = (
        selected.get("input_schema")
        if isinstance(selected, dict)
        else getattr(selected, "input_schema", None)
    )
    return schema if isinstance(schema, dict) else None


def _validated_operation_arguments(
    connector_id: str,
    operation: str,
    arguments: dict[str, Any],
    search: Any,
    slug: str,
    *,
    require_schema: bool = False,
) -> dict[str, Any]:
    normalized = _normalize_operation_arguments(connector_id, operation, arguments)
    input_schema = _composio_input_schema(search, slug)
    if input_schema is None:
        if require_schema:
            raise ConnectorBrokerError(
                503,
                "El proveedor no publicó el esquema de esta acción; no se puede autorizar con seguridad",
                "connector_schema_unavailable",
            )
        return normalized
    try:
        jsonschema.validate(normalized, input_schema)
    except jsonschema.ValidationError as exc:
        field = ".".join(str(item) for item in exc.absolute_path)
        suffix = f" en {field}" if field else ""
        raise ConnectorBrokerError(
            400,
            f"Argumentos inválidos para {operation}{suffix}",
            "bad_connector_arguments",
        ) from exc
    except jsonschema.SchemaError as exc:
        if require_schema:
            raise ConnectorBrokerError(
                503,
                "El proveedor publicó un esquema inválido; no se puede autorizar con seguridad",
                "connector_schema_unavailable",
            ) from exc
        logging.warning(
            "Ignoring invalid provider schema connector=%s operation=%s tool=%s",
            connector_id, operation, slug,
        )
    return normalized


def _delete_session(session: Any) -> None:
    try:
        session.delete()
    except Exception:
        pass


def _requires_connect_link(exc: Exception) -> bool:
    if exc.__class__.__name__ == "ComposioLegacyConnectedAccountsEndpointRetiredError":
        return True
    message = str(exc).lower()
    return "no longer supported" in message and "connected_accounts/link" in message


def _normalize_operation_arguments(
    connector_id: str,
    operation: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Translate friendly aliases into the exact schema of pinned actions."""
    if connector_id in {"snowflake", "databricks"} and operation in {
        "select_query", "execute_sql",
    }:
        normalized = dict(arguments)
        statement = normalized.get("statement")
        if statement is None:
            statement = normalized.get("query", normalized.get("sql"))
        normalized.pop("query", None)
        normalized.pop("sql", None)
        if not isinstance(statement, str) or not statement.strip() or len(statement) > 100_000:
            raise ConnectorBrokerError(
                400, f"{operation} requiere statement", "bad_connector_arguments"
            )
        statement = statement.strip()
        if operation == "select_query":
            statement = _validated_select_statement(statement)
        normalized["statement"] = statement
        return normalized

    if connector_id == "github" and operation == "read_file":
        normalized = dict(arguments)
        aliases = {
            "owner": ("repository_owner", "repo_owner", "org"),
            "repo": ("repository", "repository_name", "repo_name"),
            "path": ("file", "file_path"),
        }
        for canonical, alternatives in aliases.items():
            if canonical not in normalized:
                for alias in alternatives:
                    if alias in normalized:
                        normalized[canonical] = normalized[alias]
                        break
            for alias in alternatives:
                normalized.pop(alias, None)
        for field in ("owner", "repo", "path"):
            if not isinstance(normalized.get(field), str) or not normalized[field].strip():
                raise ConnectorBrokerError(
                    400, f"read_file requiere {field}", "bad_connector_arguments"
                )
            normalized[field] = normalized[field].strip()
        return normalized

    if connector_id != "google-workspace":
        return arguments

    if operation in {"draft_email", "send_email"}:
        normalized = dict(arguments)
        aliases = {
            "recipient_email": (
                "to", "recipient", "recipientEmail", "to_email", "email",
            ),
            "subject": ("title",),
            "body": ("message", "content", "text"),
            "is_html": ("isHtml",),
        }
        for canonical, alternatives in aliases.items():
            if canonical not in normalized:
                for alias in alternatives:
                    if alias in normalized:
                        normalized[canonical] = normalized[alias]
                        break
            for alias in alternatives:
                normalized.pop(alias, None)

        recipient = normalized.get("recipient_email")
        if (
            not isinstance(recipient, str)
            or not recipient.strip()
            or len(recipient.strip()) > 320
        ):
            raise ConnectorBrokerError(
                400,
                f"{operation} requiere recipient_email",
                "bad_connector_arguments",
            )
        body = normalized.get("body")
        if not isinstance(body, str) or not body.strip() or len(body) > 100_000:
            raise ConnectorBrokerError(
                400,
                f"{operation} requiere body",
                "bad_connector_arguments",
            )
        normalized["recipient_email"] = recipient.strip()
        normalized["body"] = body.strip()
        subject = normalized.get("subject")
        if subject is not None:
            if not isinstance(subject, str) or len(subject) > 998:
                raise ConnectorBrokerError(
                    400,
                    "subject no es válido",
                    "bad_connector_arguments",
                )
            normalized["subject"] = subject.strip()
        for field in ("cc", "bcc", "extra_recipients"):
            value = normalized.get(field)
            if isinstance(value, str):
                normalized[field] = [
                    item.strip() for item in value.split(",") if item.strip()
                ]
        return normalized

    if operation == "delete_calendar_event":
        normalized = dict(arguments)
        event_id = normalized.get("event_id", normalized.get("eventId", normalized.get("id")))
        if not isinstance(event_id, str) or not event_id.strip() or len(event_id.strip()) > 2048:
            raise ConnectorBrokerError(
                400,
                "delete_calendar_event requiere el event_id exacto; usa list_calendar_events primero",
                "bad_connector_arguments",
            )
        calendar_id = normalized.get("calendar_id", normalized.get("calendarId", "primary"))
        if not isinstance(calendar_id, str) or not calendar_id.strip() or len(calendar_id.strip()) > 1024:
            raise ConnectorBrokerError(
                400,
                "calendar_id no es válido",
                "bad_connector_arguments",
            )
        result: dict[str, Any] = {
            "event_id": event_id.strip(),
            "calendar_id": calendar_id.strip(),
        }
        if isinstance(normalized.get("send_updates"), str):
            result["send_updates"] = normalized["send_updates"]
        return result

    if operation != "create_calendar_event":
        return arguments

    normalized = dict(arguments)
    aliases = {
        "summary": ("title", "name", "subject"),
        "start_datetime": ("start", "start_time", "startTime", "startDateTime"),
        "end_datetime": ("end", "end_time", "endTime", "endDateTime"),
        "timezone": ("time_zone", "timeZone"),
        "calendar_id": ("calendar", "calendarId"),
    }
    for canonical, alternatives in aliases.items():
        if canonical not in normalized:
            for alias in alternatives:
                if alias in normalized:
                    normalized[canonical] = normalized[alias]
                    break
        for alias in alternatives:
            normalized.pop(alias, None)

    start = normalized.get("start_datetime")
    if not isinstance(start, str) or not _ISO_DATETIME_RE.fullmatch(start.strip()):
        raise ConnectorBrokerError(
            400,
            "create_calendar_event requiere start_datetime en ISO 8601 exacto "
            "(por ejemplo 2026-08-18T15:00:00), no texto como 'manana a las 3'",
            "bad_connector_arguments",
        )
    normalized["start_datetime"] = start.strip()

    end = normalized.get("end_datetime")
    if end is not None:
        if not isinstance(end, str) or not _ISO_DATETIME_RE.fullmatch(end.strip()):
            raise ConnectorBrokerError(
                400,
                "end_datetime debe usar ISO 8601 exacto",
                "bad_connector_arguments",
            )
        normalized["end_datetime"] = end.strip()

    timezone_name = normalized.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ConnectorBrokerError(
            400,
            "create_calendar_event requiere timezone IANA, por ejemplo America/Denver",
            "bad_connector_arguments",
        )
    timezone_name = timezone_name.strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConnectorBrokerError(
            400,
            "timezone debe ser una zona IANA valida, por ejemplo America/Denver",
            "bad_connector_arguments",
        ) from exc
    normalized["timezone"] = timezone_name

    duration = normalized.pop("duration_minutes", None)
    if duration is not None:
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
            raise ConnectorBrokerError(
                400,
                "duration_minutes debe ser un numero positivo",
                "bad_connector_arguments",
            )
        whole_minutes = int(duration)
        normalized.setdefault("event_duration_hour", whole_minutes // 60)
        normalized.setdefault("event_duration_minutes", whole_minutes % 60)

    if "end_datetime" not in normalized and not any(
        key in normalized for key in ("event_duration_hour", "event_duration_minutes")
    ):
        normalized["event_duration_hour"] = 1
        normalized["event_duration_minutes"] = 0
    normalized.setdefault("calendar_id", "primary")
    return normalized


def _validated_select_statement(value: str) -> str:
    """Accept one conservative SELECT statement and reject mutating SQL.

    Composio's Execute SQL action accepts arbitrary statements. This parser is
    deliberately stricter than a general SQL grammar: comments, batches, CTEs
    and procedural constructs are refused on the read-only path. Customers
    that need those operations must use ``execute_sql`` and approve the exact
    statement.
    """
    if "--" in value or "/*" in value or "*/" in value:
        raise ConnectorBrokerError(
            400, "select_query no admite comentarios SQL", "unsafe_select_query"
        )
    trimmed = value.strip()
    if trimmed.endswith(";"):
        trimmed = trimmed[:-1].rstrip()
    if ";" in trimmed:
        raise ConnectorBrokerError(
            400, "select_query admite una sola sentencia", "unsafe_select_query"
        )
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", trimmed.casefold())
    if not tokens or tokens[0] != "select":
        raise ConnectorBrokerError(
            400, "select_query debe comenzar con SELECT", "unsafe_select_query"
        )
    forbidden = sorted(_SQL_FORBIDDEN_READ_TOKENS.intersection(tokens))
    if forbidden:
        raise ConnectorBrokerError(
            400,
            "select_query contiene SQL mutante; usa execute_sql con aprobación explícita",
            "unsafe_select_query",
        )
    return trimmed


def _upstream_error(exc: Exception, fallback: str) -> ConnectorBrokerError:
    # El detalle del proveedor puede contener IDs internos, URLs o headers. Se
    # conserva únicamente en logs privados y nunca se devuelve al dispositivo.
    logging.warning("%s: %s", fallback, exc, exc_info=True)
    return ConnectorBrokerError(502, fallback, "connector_upstream_error")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="json"))
    if hasattr(value, "dict"):
        return _json_value(value.dict())
    raise ConnectorBrokerError(502, "Composio devolvio un resultado invalido", "connector_adapter_error")
