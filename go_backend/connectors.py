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
            "trello", "Trello", "Tableros, listas, tarjetas y responsables.",
            ("trello", "board", "list", "card", "task"),
            ("list_boards", "get_board", "list_cards", "create_card", "update_card"),
        ),
        _connector(
            "monday-com", "monday.com", "Tableros, proyectos y automatizaciones de trabajo.",
            ("monday", "board", "item", "project", "work management"),
            ("list_boards", "get_board", "list_items", "create_item", "update_item"),
        ),
        _connector(
            "intercom", "Intercom", "Conversaciones, usuarios y atencion al cliente.",
            ("intercom", "support", "conversation", "contact", "customer"),
            ("search_contacts", "list_conversations", "get_conversation", "reply_conversation"),
        ),
        _connector(
            "zendesk", "Zendesk", "Tickets, usuarios y operaciones de soporte.",
            ("zendesk", "support", "ticket", "user", "customer"),
            ("search_tickets", "get_ticket", "create_ticket", "update_ticket"),
        ),
        _connector(
            "box", "Box", "Archivos, carpetas y colaboracion empresarial.",
            ("box", "file", "folder", "document", "storage"),
            ("search_files", "get_file", "list_folder", "upload_file"),
        ),
        _connector(
            "dropbox", "Dropbox", "Archivos, carpetas y contenido compartido.",
            ("dropbox", "file", "folder", "document", "storage"),
            ("search_files", "get_file", "list_folder", "upload_file"),
        ),
        _connector(
            "docusign", "DocuSign", "Sobres, firmas y seguimiento de documentos.",
            ("docusign", "signature", "envelope", "document", "contract"),
            ("list_envelopes", "get_envelope", "create_envelope", "send_envelope"),
        ),
        _connector(
            "calendly", "Calendly", "Tipos de evento, disponibilidad y reuniones.",
            ("calendly", "calendar", "meeting", "event", "availability"),
            ("list_event_types", "list_scheduled_events", "get_event", "cancel_event"),
        ),
        _connector(
            "loom", "Loom", "Videos, transcripciones y espacios de equipo.",
            ("loom", "video", "recording", "transcript", "workspace"),
            ("search_videos", "get_video", "list_transcripts"),
        ),
        _connector(
            "outreach", "Outreach", "Prospectos, secuencias y actividades comerciales.",
            ("outreach", "sales", "prospect", "sequence", "activity"),
            ("search_prospects", "get_prospect", "list_sequences", "create_task", "update_prospect"),
        ),
        _connector(
            "salesloft", "Salesloft", "Cadencias, personas y actividades de ventas.",
            ("salesloft", "sales", "person", "cadence", "activity"),
            ("search_people", "get_person", "list_cadences", "create_activity", "update_person"),
        ),
        _connector(
            "apollo", "Apollo", "Personas, empresas y enriquecimiento comercial.",
            ("apollo", "sales", "person", "company", "enrichment"),
            ("search_people", "search_organizations", "enrich_person", "enrich_organization"),
        ),
        _connector(
            "clay", "Clay", "Tablas, enriquecimiento y flujos de prospeccion.",
            ("clay", "sales", "table", "enrichment", "prospecting"),
            ("list_tables", "get_table", "list_records", "update_record"),
        ),
        _connector(
            "zoominfo", "ZoomInfo", "Contactos, empresas e inteligencia comercial.",
            ("zoominfo", "sales", "contact", "company", "intelligence"),
            ("search_contacts", "search_companies", "get_contact", "get_company"),
        ),
        _connector(
            "nooks", "Nooks", "Marcador, sesiones y productividad de ventas.",
            ("nooks", "sales", "dialer", "call", "session"),
            ("list_sessions", "get_session", "list_calls", "get_call"),
        ),
        _connector(
            "stripe", "Stripe", "Clientes, pagos, facturas y suscripciones.",
            ("stripe", "payment", "customer", "invoice", "subscription"),
            ("search_customers", "get_customer", "list_payments", "list_invoices", "list_subscriptions"),
        ),
        _connector(
            "quickbooks", "QuickBooks", "Contabilidad, facturas, gastos y clientes.",
            ("quickbooks", "accounting", "invoice", "expense", "customer"),
            ("search_customers", "get_customer", "list_invoices", "create_invoice", "list_expenses"),
        ),
        _connector(
            "netsuite", "NetSuite", "ERP, finanzas, clientes y operaciones.",
            ("netsuite", "erp", "finance", "customer", "record"),
            ("search_records", "get_record", "create_record", "update_record"),
        ),
        _connector(
            "ramp", "Ramp", "Tarjetas, gastos, reembolsos y proveedores.",
            ("ramp", "finance", "card", "expense", "reimbursement"),
            ("list_cards", "list_transactions", "list_reimbursements", "get_transaction"),
        ),
        _connector(
            "workday", "Workday", "Personas, puestos y operaciones de recursos humanos.",
            ("workday", "hr", "worker", "position", "time off"),
            ("search_workers", "get_worker", "list_positions", "list_time_off"),
        ),
        _connector(
            "rippling", "Rippling", "Empleados, nomina, dispositivos y aplicaciones.",
            ("rippling", "hr", "employee", "payroll", "device"),
            ("list_employees", "get_employee", "list_payroll_runs", "list_devices"),
        ),
        _connector(
            "ashby", "Ashby", "Candidatos, vacantes y procesos de contratacion.",
            ("ashby", "recruiting", "candidate", "job", "interview"),
            ("list_jobs", "search_candidates", "get_candidate", "list_interviews"),
        ),
        _connector(
            "greenhouse", "Greenhouse", "Candidatos, entrevistas y vacantes.",
            ("greenhouse", "recruiting", "candidate", "job", "application"),
            ("list_jobs", "search_candidates", "get_candidate", "list_applications"),
        ),
        _connector(
            "vercel", "Vercel", "Proyectos, deployments, dominios y logs.",
            ("vercel", "deployment", "project", "domain", "log"),
            ("list_projects", "get_project", "list_deployments", "get_deployment", "list_domains"),
        ),
        _connector(
            "tableau", "Tableau", "Fuentes, workbooks y visualizaciones.",
            ("tableau", "analytics", "workbook", "dashboard", "view"),
            ("search_workbooks", "get_workbook", "list_views", "query_view"),
        ),
        _connector(
            "hex", "Hex", "Proyectos, notebooks y analisis colaborativo.",
            ("hex", "analytics", "project", "notebook", "query"),
            ("list_projects", "get_project", "run_project", "get_run"),
        ),
        _connector(
            "amplitude", "Amplitude", "Analitica de producto, eventos y cohortes.",
            ("amplitude", "analytics", "event", "funnel", "cohort"),
            ("query_events", "query_funnel", "query_retention", "list_cohorts"),
        ),
        _connector(
            "mixpanel", "Mixpanel", "Eventos, funnels, retencion y perfiles.",
            ("mixpanel", "analytics", "event", "funnel", "retention"),
            ("query_events", "query_funnel", "query_retention", "list_profiles"),
        ),
        _connector(
            "snowflake", "Snowflake", "Warehouses, bases de datos y consultas.",
            ("snowflake", "data warehouse", "database", "table", "sql"),
            ("list_databases", "list_schemas", "list_tables", "describe_table", "run_query"),
        ),
        _connector(
            "databricks", "Databricks", "Lakehouse, notebooks, jobs y consultas.",
            ("databricks", "lakehouse", "notebook", "job", "sql"),
            ("list_catalogs", "list_schemas", "list_tables", "run_query", "list_jobs"),
        ),
        _connector(
            "mailchimp", "Mailchimp", "Audiencias, campanas y automatizaciones.",
            ("mailchimp", "marketing", "audience", "campaign", "automation"),
            ("list_audiences", "search_members", "get_campaign", "create_campaign", "list_automations"),
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
    computer_id: str | None
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
        computer_id: str | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        if not connector_ids and not computer_id:
            raise ValueError("No se emiten grants vacios")
        expires_in = self.default_ttl_seconds if ttl_seconds is None else max(
            1, min(int(ttl_seconds), self.default_ttl_seconds)
        )
        token = secrets.token_urlsafe(32)
        grant = ConnectorRunGrant(
            user_id=user_id,
            connector_ids=frozenset(connector_ids),
            computer_id=computer_id,
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

    def computer(self, token: str) -> tuple[str, str]:
        """Devuelve el único (usuario, bot) autorizado para esta ejecución."""
        grant = self._require_grant(token)
        if not grant.computer_id:
            raise ConnectorBrokerError(
                403,
                "Esta ejecución no tiene una computadora autorizada",
                "computer_forbidden",
            )
        return grant.user_id, grant.computer_id

    def has_computer(self, token: str) -> bool:
        return bool(self._require_grant(token).computer_id)

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
