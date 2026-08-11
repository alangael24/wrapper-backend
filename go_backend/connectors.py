"""Broker local y efimero para herramientas de conectores de Pi.

La extension TypeScript nunca recibe secretos OAuth. Solo obtiene un token
aleatorio ligado a una ejecucion, un usuario y una lista cerrada de conectores.
Los adaptadores reales viven en el proceso del backend y se registran aqui.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol


MAX_CONNECTOR_ARGUMENTS_BYTES = 64 * 1024
MAX_CONNECTOR_RESULT_BYTES = 64 * 1024


def _connector(
    connector_id: str,
    name: str,
    description: str,
    keywords: tuple[str, ...],
    operations: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "id": connector_id,
        "name": name,
        "description": description,
        "keywords": keywords,
        "operations": operations,
    }


CONNECTOR_CATALOG: dict[str, dict[str, Any]] = {
    item["id"]: item
    for item in (
        _connector(
            "google-workspace",
            "Google Workspace",
            "Gmail, Calendar, Drive, Contacts y Sheets.",
            ("google", "gmail", "email", "calendar", "drive", "contacts", "sheets"),
            (
                "search_email", "read_email", "draft_email", "list_calendar_events",
                "create_calendar_event", "search_drive", "read_drive_file",
                "list_contacts", "read_sheet", "update_sheet",
            ),
        ),
        _connector(
            "slack", "Slack", "Canales, mensajes y coordinacion de equipo.",
            ("slack", "chat", "channel", "message", "thread"),
            ("list_channels", "search_messages", "read_thread", "post_message"),
        ),
        _connector(
            "notion", "Notion", "Paginas, bases de datos y conocimiento.",
            ("notion", "page", "database", "wiki", "knowledge"),
            ("search", "read_page", "create_page", "query_database", "update_page"),
        ),
        _connector(
            "salesforce", "Salesforce", "Cuentas, contactos y oportunidades.",
            ("salesforce", "crm", "account", "contact", "opportunity", "lead"),
            ("search_records", "get_record", "create_record", "update_record"),
        ),
        _connector(
            "microsoft-365", "Microsoft 365", "Outlook, OneDrive, Calendar y Teams.",
            ("microsoft", "outlook", "email", "calendar", "onedrive", "teams"),
            (
                "search_email", "read_email", "draft_email", "list_calendar_events",
                "create_calendar_event", "search_files", "read_file", "post_teams_message",
            ),
        ),
        _connector(
            "linkedin", "LinkedIn", "Perfiles y relaciones profesionales.",
            ("linkedin", "profile", "professional", "network"),
            ("get_profile", "search_connections"),
        ),
        _connector(
            "zoom", "Zoom", "Reuniones y seguimiento de llamadas.",
            ("zoom", "meeting", "call", "recording"),
            ("list_meetings", "get_meeting", "create_meeting", "list_recordings"),
        ),
        _connector(
            "github", "GitHub", "Repositorios, issues y pull requests.",
            ("github", "git", "repository", "repo", "issue", "pull request", "code"),
            ("search_repositories", "read_file", "list_issues", "get_issue", "create_issue", "list_pull_requests"),
        ),
        _connector(
            "jira", "Jira", "Proyectos, tickets y ciclos de trabajo.",
            ("jira", "ticket", "issue", "project", "sprint"),
            ("search_issues", "get_issue", "create_issue", "update_issue"),
        ),
        _connector(
            "linear", "Linear", "Issues, proyectos y ciclos de producto.",
            ("linear", "issue", "project", "cycle", "product"),
            ("search_issues", "get_issue", "create_issue", "update_issue"),
        ),
        _connector(
            "asana", "Asana", "Proyectos, tareas y responsables.",
            ("asana", "project", "task", "assignee"),
            ("search_tasks", "get_task", "create_task", "update_task"),
        ),
        _connector(
            "clickup", "ClickUp", "Tareas, documentos y proyectos.",
            ("clickup", "project", "task", "document"),
            ("search_tasks", "get_task", "create_task", "update_task"),
        ),
        _connector(
            "figma", "Figma", "Archivos, comentarios y entregables de diseno.",
            ("figma", "design", "file", "comment", "prototype"),
            ("search_files", "get_file", "list_comments", "post_comment"),
        ),
        _connector(
            "hubspot", "HubSpot", "Contactos, empresas y oportunidades.",
            ("hubspot", "crm", "contact", "company", "deal"),
            ("search_contacts", "get_contact", "create_contact", "update_contact", "search_deals"),
        ),
        _connector(
            "canva", "Canva", "Disenos, plantillas y contenido de marca.",
            ("canva", "design", "template", "brand"),
            ("search_designs", "get_design", "create_design"),
        ),
        _connector(
            "shopify", "Shopify", "Catalogo y contexto de la tienda.",
            ("shopify", "commerce", "store", "product", "order", "customer"),
            ("search_products", "get_product", "list_orders", "get_order", "list_customers"),
        ),
        _connector(
            "tiendanube", "Tiendanube", "Catalogo y contexto de la tienda.",
            ("tiendanube", "nuvemshop", "commerce", "store", "product", "order"),
            ("search_products", "get_product", "list_orders", "get_order"),
        ),
        _connector(
            "woocommerce", "WooCommerce", "Productos y contexto de WordPress Commerce.",
            ("woocommerce", "wordpress", "commerce", "store", "product", "order"),
            ("search_products", "get_product", "list_orders", "get_order"),
        ),
    )
}


class ConnectorBrokerError(RuntimeError):
    def __init__(self, status: int, message: str, code: str):
        super().__init__(message)
        self.status = status
        self.code = code


class ConnectorAdapter(Protocol):
    """Contrato de un adaptador que conserva sus credenciales en el backend."""

    def is_connected(self, user_id: str) -> bool: ...

    def execute(self, user_id: str, operation: str, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class ConnectorRunGrant:
    user_id: str
    connector_ids: frozenset[str]
    expires_at: float


class ConnectorBroker:
    """Emite grants de corta vida y despacha solo adaptadores autorizados."""

    def __init__(self, *, default_ttl_seconds: int = 1900, now=time.monotonic):
        self.default_ttl_seconds = max(1, min(int(default_ttl_seconds), 3600))
        self._now = now
        self._grants: dict[str, ConnectorRunGrant] = {}
        self._adapters: dict[str, ConnectorAdapter] = {}
        self._lock = threading.RLock()

    @staticmethod
    def normalize_connector_ids(value: Any) -> tuple[str, ...]:
        if not isinstance(value, list):
            raise ConnectorBrokerError(400, "connector_ids debe ser una lista", "bad_connectors")
        if len(value) > len(CONNECTOR_CATALOG):
            raise ConnectorBrokerError(400, "Demasiados conectores", "bad_connectors")
        normalized: list[str] = []
        for connector_id in value:
            if not isinstance(connector_id, str) or connector_id not in CONNECTOR_CATALOG:
                raise ConnectorBrokerError(
                    400, f"Conector desconocido: {connector_id!r}", "bad_connector"
                )
            if connector_id not in normalized:
                normalized.append(connector_id)
        return tuple(normalized)

    def register_adapter(self, connector_id: str, adapter: ConnectorAdapter) -> None:
        if connector_id not in CONNECTOR_CATALOG:
            raise ValueError(f"Conector desconocido: {connector_id}")
        with self._lock:
            self._adapters[connector_id] = adapter

    def issue(
        self,
        *,
        user_id: str,
        connector_ids: tuple[str, ...],
        ttl_seconds: int | None = None,
    ) -> str:
        if not connector_ids:
            raise ValueError("No se emiten grants vacios")
        expires_in = self.default_ttl_seconds if ttl_seconds is None else max(
            1, min(int(ttl_seconds), self.default_ttl_seconds)
        )
        token = secrets.token_urlsafe(32)
        grant = ConnectorRunGrant(
            user_id=user_id,
            connector_ids=frozenset(connector_ids),
            expires_at=self._now() + expires_in,
        )
        with self._lock:
            self._prune_locked()
            self._grants[token] = grant
        return token

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._grants.pop(token, None)

    def catalog(self, token: str) -> list[dict[str, Any]]:
        grant = self._require_grant(token)
        with self._lock:
            adapters = dict(self._adapters)
        result: list[dict[str, Any]] = []
        for connector_id in sorted(grant.connector_ids):
            item = CONNECTOR_CATALOG[connector_id]
            adapter = adapters.get(connector_id)
            connected = False
            if adapter is not None:
                try:
                    connected = bool(adapter.is_connected(grant.user_id))
                except Exception:
                    connected = False
            result.append({**item, "connected": connected})
        return result

    def execute(
        self,
        *,
        token: str,
        connector_id: Any,
        operation: Any,
        arguments: Any,
    ) -> dict[str, Any]:
        grant = self._require_grant(token)
        if not isinstance(connector_id, str) or connector_id not in CONNECTOR_CATALOG:
            raise ConnectorBrokerError(404, "Conector desconocido", "connector_not_found")
        if connector_id not in grant.connector_ids:
            raise ConnectorBrokerError(403, "Conector fuera del grant de esta ejecucion", "connector_forbidden")
        item = CONNECTOR_CATALOG[connector_id]
        if not isinstance(operation, str) or operation not in item["operations"]:
            raise ConnectorBrokerError(400, "Operacion no permitida para el conector", "bad_connector_operation")
        if not isinstance(arguments, dict):
            raise ConnectorBrokerError(400, "arguments debe ser un objeto JSON", "bad_connector_arguments")
        if len(json.dumps(arguments, ensure_ascii=False).encode("utf-8")) > MAX_CONNECTOR_ARGUMENTS_BYTES:
            raise ConnectorBrokerError(413, "arguments excede 64 KiB", "connector_arguments_too_large")

        adapter = self._adapter_for(connector_id)
        try:
            connected = bool(adapter.is_connected(grant.user_id))
        except Exception as exc:
            raise ConnectorBrokerError(502, "No se pudo consultar la conexion", "connector_adapter_error") from exc
        if not connected:
            raise ConnectorBrokerError(
                409,
                f"{item['name']} todavia no esta autenticado para este usuario",
                "connector_not_connected",
            )
        try:
            result = adapter.execute(grant.user_id, operation, arguments)
        except ConnectorBrokerError:
            raise
        except Exception as exc:
            raise ConnectorBrokerError(502, "El proveedor rechazo la operacion", "connector_upstream_error") from exc
        payload = {"connector_id": connector_id, "operation": operation, "result": result}
        try:
            result_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ConnectorBrokerError(
                502, "El adaptador devolvio un resultado no serializable", "connector_adapter_error"
            ) from exc
        if result_size > MAX_CONNECTOR_RESULT_BYTES:
            raise ConnectorBrokerError(
                502, "El resultado del conector excedio 64 KiB", "connector_result_too_large"
            )
        return payload

    def _adapter_for(self, connector_id: str) -> ConnectorAdapter:
        with self._lock:
            adapter = self._adapters.get(connector_id)
        if adapter is None:
            item = CONNECTOR_CATALOG[connector_id]
            raise ConnectorBrokerError(
                409,
                f"{item['name']} no tiene un adaptador configurado en este servidor",
                "connector_not_configured",
            )
        return adapter

    def _require_grant(self, token: str) -> ConnectorRunGrant:
        if not token:
            raise ConnectorBrokerError(401, "Falta el token interno de ejecucion", "connector_token_required")
        with self._lock:
            self._prune_locked()
            grant = self._grants.get(token)
        if grant is None:
            raise ConnectorBrokerError(401, "Token interno invalido o vencido", "connector_token_invalid")
        return grant

    def _prune_locked(self) -> None:
        now = self._now()
        expired = [token for token, grant in self._grants.items() if grant.expires_at <= now]
        for token in expired:
            self._grants.pop(token, None)
