"""First-party REST connector gateway for providers absent from Composio.

The browser receives a one-time setup URL, never a provider credential. User
credentials are encrypted by the wrapper and are only decrypted for a bounded
request to a provider API. Pi continues to use the existing ephemeral broker.
"""

from __future__ import annotations

import base64
import html
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from .connectors import CONNECTOR_CATALOG, ConnectorBrokerError
from .crypto_utils import CryptoError, decrypt_api_key, encrypt_api_key


MAX_NATIVE_RESPONSE_BYTES = 2 * 1024 * 1024
NATIVE_REQUEST_TIMEOUT_SECONDS = 20
AUTH_ATTEMPT_TTL_SECONDS = 10 * 60


@dataclass(frozen=True)
class CredentialField:
    name: str
    label: str
    secret: bool = False
    placeholder: str = ""
    required: bool = True


@dataclass(frozen=True)
class Operation:
    method: str
    path: str


@dataclass(frozen=True)
class NativeDefinition:
    fields: tuple[CredentialField, ...]
    base_url: str
    auth: str
    operations: dict[str, Operation]


TOKEN = CredentialField("access_token", "Access token o API key", True)
LABEL = CredentialField("account_label", "Nombre de esta cuenta", False, "Mi cuenta", False)


NATIVE_CONNECTORS: dict[str, NativeDefinition] = {
    "nooks": NativeDefinition(
        (TOKEN, LABEL), "https://partner-api.nooks.in/v1/", "bearer",
        {
            "list_sessions": Operation("GET", "sequences"),
            "get_session": Operation("GET", "sequences/{id}"),
            "list_calls": Operation("GET", "calls"),
            "get_call": Operation("GET", "calls/{id}"),
        },
    ),
    "rippling": NativeDefinition(
        (TOKEN, LABEL), "https://rest.ripplingapis.com/", "bearer",
        {
            "list_employees": Operation("GET", "workers/"),
            "get_employee": Operation("GET", "workers/{id}"),
            "list_payroll_runs": Operation("GET", "payroll-runs/"),
            "list_devices": Operation("GET", "devices/"),
        },
    ),
    "salesloft": NativeDefinition(
        (TOKEN, LABEL), "https://api.salesloft.com/v2/", "bearer",
        {
            "search_people": Operation("GET", "people"),
            "get_person": Operation("GET", "people/{id}"),
            "list_cadences": Operation("GET", "cadences"),
            "create_activity": Operation("POST", "activities"),
            "update_person": Operation("PUT", "people/{id}"),
        },
    ),
    "tiendanube": NativeDefinition(
        (TOKEN, CredentialField("store_id", "ID de la tienda"), LABEL),
        "https://api.tiendanube.com/2025-03/{store_id}/", "bearer",
        {
            "search_products": Operation("GET", "products"),
            "get_product": Operation("GET", "products/{id}"),
            "list_orders": Operation("GET", "orders"),
            "get_order": Operation("GET", "orders/{id}"),
        },
    ),
    "clay": NativeDefinition(
        (TOKEN, CredentialField("api_base_url", "URL base de Clay", False, "https://api.clay.com/v3/"), LABEL),
        "{api_base_url}", "bearer",
        {
            "list_tables": Operation("GET", "tables"),
            "get_table": Operation("GET", "tables/{id}"),
            "list_records": Operation("GET", "tables/{table_id}/records"),
            "update_record": Operation("PATCH", "tables/{table_id}/records/{id}"),
        },
    ),
    "docusign": NativeDefinition(
        (
            TOKEN,
            CredentialField("api_base_url", "URL base de eSignature", False, "https://www.docusign.net/restapi/v2.1/"),
            CredentialField("account_id", "Account ID"),
            LABEL,
        ),
        "{api_base_url}accounts/{account_id}/", "bearer",
        {
            "list_envelopes": Operation("GET", "envelopes"),
            "get_envelope": Operation("GET", "envelopes/{id}"),
            "create_envelope": Operation("POST", "envelopes"),
            "send_envelope": Operation("PUT", "envelopes/{id}"),
        },
    ),
    "netsuite": NativeDefinition(
        (TOKEN, CredentialField("account_id", "NetSuite Account ID"), LABEL),
        "https://{account_id}.suitetalk.api.netsuite.com/services/rest/record/v1/", "bearer",
        {
            "search_records": Operation("GET", "{record_type}"),
            "get_record": Operation("GET", "{record_type}/{id}"),
            "create_record": Operation("POST", "{record_type}"),
            "update_record": Operation("PATCH", "{record_type}/{id}"),
        },
    ),
    "outreach": NativeDefinition(
        (TOKEN, LABEL), "https://api.outreach.io/api/v2/", "bearer",
        {
            "search_prospects": Operation("GET", "prospects"),
            "get_prospect": Operation("GET", "prospects/{id}"),
            "list_sequences": Operation("GET", "sequences"),
            "create_task": Operation("POST", "tasks"),
            "update_prospect": Operation("PATCH", "prospects/{id}"),
        },
    ),
    "ramp": NativeDefinition(
        (TOKEN, CredentialField("api_base_url", "URL base de Ramp", False, "https://api.ramp.com/developer/v1/"), LABEL),
        "{api_base_url}", "bearer",
        {
            "list_cards": Operation("GET", "cards"),
            "list_transactions": Operation("GET", "transactions"),
            "list_reimbursements": Operation("GET", "reimbursements"),
            "get_transaction": Operation("GET", "transactions/{id}"),
        },
    ),
    "tableau": NativeDefinition(
        (
            CredentialField("credentials_token", "Tableau credentials token", True),
            CredentialField("server_url", "Tableau Server URL", False, "https://prod-useast-a.online.tableau.com/"),
            CredentialField("site_id", "Site LUID"),
            LABEL,
        ),
        "{server_url}api/3.29/sites/{site_id}/", "tableau",
        {
            "search_workbooks": Operation("GET", "workbooks"),
            "get_workbook": Operation("GET", "workbooks/{id}"),
            "list_views": Operation("GET", "views"),
            "query_view": Operation("GET", "views/{id}/data"),
        },
    ),
    "woocommerce": NativeDefinition(
        (
            CredentialField("store_url", "URL de WooCommerce", False, "https://tienda.example/"),
            CredentialField("consumer_key", "Consumer key", True),
            CredentialField("consumer_secret", "Consumer secret", True),
            LABEL,
        ),
        "{store_url}wp-json/wc/v3/", "basic",
        {
            "search_products": Operation("GET", "products"),
            "get_product": Operation("GET", "products/{id}"),
            "list_orders": Operation("GET", "orders"),
            "get_order": Operation("GET", "orders/{id}"),
        },
    ),
    "workday": NativeDefinition(
        (TOKEN, CredentialField("api_base_url", "Workday REST API endpoint"), LABEL),
        "{api_base_url}", "bearer",
        {
            "search_workers": Operation("GET", "workers"),
            "get_worker": Operation("GET", "workers/{id}"),
            "list_positions": Operation("GET", "positions"),
            "list_time_off": Operation("GET", "workers"),
        },
    ),
    "zoominfo": NativeDefinition(
        (TOKEN, CredentialField("api_base_url", "URL base de ZoomInfo", False, "https://api.zoominfo.com/"), LABEL),
        "{api_base_url}", "bearer",
        {
            "search_contacts": Operation("POST", "search/contact"),
            "search_companies": Operation("POST", "search/company"),
            "get_contact": Operation("POST", "enrich/contact"),
            "get_company": Operation("POST", "enrich/company"),
        },
    ),
}


@dataclass
class _NativeAttempt:
    user_id: str
    connector_id: str
    expires_at: float
    complete: bool = False
    account_label: str = ""


class NativeConnectorGateway:
    def __init__(
        self,
        *,
        store: Any,
        secret_env: str | None,
        secret_path: Path,
        public_base_url: str,
        now=time.monotonic,
        attempt_ttl_seconds: int = AUTH_ATTEMPT_TTL_SECONDS,
    ):
        self.store = store
        self.secret_env = secret_env
        self.secret_path = secret_path
        self.public_base_url = public_base_url.rstrip("/")
        self._now = now
        self._ttl = max(60, min(int(attempt_ttl_seconds), 1800))
        self._attempts: dict[str, _NativeAttempt] = {}
        self._lock = threading.RLock()

    @property
    def configured(self) -> bool:
        return bool(self.public_base_url and (self.secret_env or self.secret_path))

    def supports(self, connector_id: str) -> bool:
        return connector_id in NATIVE_CONNECTORS and self.configured

    def describe(self, connector_id: str) -> dict[str, Any]:
        return {
            "connector_id": connector_id,
            "toolkit": f"native:{connector_id}",
            "driver": "native",
            "available": self.supports(connector_id),
            "reason": "" if self.supports(connector_id) else "El adaptador first-party no esta configurado.",
        }

    def status(self, user_id: str, connector_id: str) -> dict[str, Any]:
        description = self.describe(connector_id)
        row = self.store.get_connector_credentials(user_id, connector_id)
        return {
            **description,
            "connected": bool(description["available"] and row),
            "account": str((row or {}).get("account_label") or "")[:160],
        }

    def start(self, user_id: str, connector_id: str) -> dict[str, str]:
        if not self.supports(connector_id):
            raise ConnectorBrokerError(409, "Conector first-party no configurado", "connector_not_configured")
        self._prune()
        attempt_id = "nat_" + secrets.token_urlsafe(32)
        with self._lock:
            self._attempts[attempt_id] = _NativeAttempt(
                user_id=user_id,
                connector_id=connector_id,
                expires_at=self._now() + self._ttl,
            )
        return {
            "attempt_id": attempt_id,
            "authorize_url": f"{self.public_base_url}/v1/connectors/native/setup/{attempt_id}",
        }

    def setup_html(self, attempt_id: str, *, error: str = "", saved: bool = False) -> bytes:
        attempt = self._attempt(attempt_id)
        saved = saved or attempt.complete
        definition = NATIVE_CONNECTORS[attempt.connector_id]
        name = CONNECTOR_CATALOG[attempt.connector_id]["name"]
        if saved:
            content = (
                "<h1>Cuenta conectada</h1><p>Ya puedes cerrar esta ventana y volver a Agent Genia.</p>"
            )
        else:
            fields = []
            for field in definition.fields:
                field_type = "password" if field.secret else "text"
                fields.append(
                    f'<label>{html.escape(field.label)}<input type="{field_type}" '
                    f'name="{html.escape(field.name)}" placeholder="{html.escape(field.placeholder)}" '
                    f'{"required " if field.required else ""}autocomplete="off"></label>'
                )
            notice = f'<p class="error">{html.escape(error)}</p>' if error else ""
            content = (
                f"<h1>Conectar {html.escape(name)}</h1>"
                "<p>Estos datos se cifran en el servidor y nunca se envían a Electron ni a Pi.</p>"
                f"{notice}<form method=\"post\">{''.join(fields)}"
                '<button type="submit">Conectar cuenta</button></form>'
            )
        return (
            "<!doctype html><html lang=\"es\"><meta charset=\"utf-8\"><meta name=\"viewport\" "
            "content=\"width=device-width,initial-scale=1\"><title>Agent Genia</title><style>"
            "body{font:16px system-ui;background:#f7f7f7;color:#171717;margin:0;padding:48px}"
            "main{max-width:520px;margin:auto;background:#fff;padding:32px;border-radius:20px;box-shadow:0 8px 30px #0001}"
            "label{display:block;margin:18px 0 6px}input{box-sizing:border-box;width:100%;padding:13px;margin-top:7px;border:1px solid #bbb;border-radius:10px}"
            "button{margin-top:22px;width:100%;padding:14px;border:0;border-radius:12px;background:#111;color:#fff;font-weight:650}"
            ".error{color:#b42318}</style><main>" + content + "</main></html>"
        ).encode()

    def submit(self, attempt_id: str, values: dict[str, str]) -> bytes:
        attempt = self._attempt(attempt_id)
        if attempt.complete:
            return self.setup_html(attempt_id, saved=True)
        definition = NATIVE_CONNECTORS[attempt.connector_id]
        allowed = {field.name for field in definition.fields}
        credentials: dict[str, str] = {}
        for field in definition.fields:
            value = str(values.get(field.name, "")).strip()
            if not value and field.required:
                return self.setup_html(attempt_id, error=f"Falta {field.label}.")
            if not value:
                continue
            if len(value) > 4096:
                return self.setup_html(attempt_id, error=f"{field.label} es demasiado largo.")
            credentials[field.name] = value
        if set(values) - allowed:
            raise ConnectorBrokerError(400, "Campos de conexion invalidos", "bad_connector_credentials")
        try:
            _build_base_url(definition, credentials, resolve=False)
            key_id = f"connector:{attempt.user_id}:{attempt.connector_id}"
            encrypted = encrypt_api_key(
                json.dumps(credentials, separators=(",", ":"), ensure_ascii=False),
                key_id,
                self.secret_env,
                self.secret_path,
            )
        except (ValueError, CryptoError) as exc:
            return self.setup_html(attempt_id, error=str(exc))
        label = credentials.get("account_label") or CONNECTOR_CATALOG[attempt.connector_id]["name"]
        self.store.upsert_connector_credentials(
            user_id=attempt.user_id,
            connector_id=attempt.connector_id,
            credentials_enc=encrypted,
            key_id=key_id,
            account_label=label,
        )
        with self._lock:
            attempt.complete = True
            attempt.account_label = label[:160]
        return self.setup_html(attempt_id, saved=True)

    def poll(self, user_id: str, attempt_id: str) -> dict[str, Any]:
        attempt = self._attempt(attempt_id)
        if attempt.user_id != user_id:
            raise ConnectorBrokerError(404, "Conexion desconocida o expirada", "connector_auth_not_found")
        if not attempt.complete:
            return {"status": "pending"}
        with self._lock:
            self._attempts.pop(attempt_id, None)
        return {
            "status": "complete",
            "session": {
                "managed_connection_id": f"native:{attempt.connector_id}",
                "connector_id": attempt.connector_id,
                "account_label": attempt.account_label,
            },
        }

    def disconnect(self, user_id: str, connector_id: str) -> dict[str, bool]:
        self.store.delete_connector_credentials(user_id, connector_id)
        return {"disconnected": True}

    def execute(self, user_id: str, connector_id: str, operation: str, arguments: dict[str, Any]) -> Any:
        definition = NATIVE_CONNECTORS.get(connector_id)
        spec = (definition.operations.get(operation) if definition else None)
        if definition is None or spec is None:
            raise ConnectorBrokerError(404, "Operacion first-party no encontrada", "connector_operation_not_found")
        credentials = self._credentials(user_id, connector_id)
        base_url = _build_base_url(definition, credentials)
        args = dict(arguments)
        path = spec.path
        for marker in _path_markers(path):
            value = args.pop(marker, None)
            if not isinstance(value, (str, int)) or not str(value):
                raise ConnectorBrokerError(400, f"Falta {marker}", "bad_connector_arguments")
            safe = str(value)
            if not all(ch.isalnum() or ch in "-_." for ch in safe):
                raise ConnectorBrokerError(400, f"{marker} invalido", "bad_connector_arguments")
            path = path.replace("{" + marker + "}", safe)
        url = urljoin(base_url, path)
        headers = {"Accept": "application/json", "User-Agent": "AgentGenia/1.0"}
        if definition.auth == "bearer":
            headers["Authorization"] = f"Bearer {credentials['access_token']}"
        elif definition.auth == "basic":
            raw = f"{credentials['consumer_key']}:{credentials['consumer_secret']}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        elif definition.auth == "tableau":
            headers["X-Tableau-Auth"] = credentials["credentials_token"]
        body = None
        if spec.method == "GET":
            query = _query_arguments(args)
            if query:
                url += ("&" if "?" in url else "?") + urlencode(query, doseq=True)
        else:
            body = json.dumps(args, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        return _request_json(url, method=spec.method, headers=headers, body=body)

    def _credentials(self, user_id: str, connector_id: str) -> dict[str, str]:
        row = self.store.get_connector_credentials(user_id, connector_id)
        if not row:
            raise ConnectorBrokerError(409, "Cuenta no conectada", "connector_not_connected")
        try:
            raw = decrypt_api_key(
                bytes(row["credentials_enc"]),
                str(row["key_id"]),
                self.secret_env,
                self.secret_path,
            )
            value = json.loads(raw)
        except (CryptoError, ValueError, TypeError) as exc:
            raise ConnectorBrokerError(500, "Credenciales cifradas invalidas", "connector_credentials_invalid") from exc
        if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            raise ConnectorBrokerError(500, "Credenciales cifradas invalidas", "connector_credentials_invalid")
        return value

    def _attempt(self, attempt_id: str) -> _NativeAttempt:
        self._prune()
        with self._lock:
            attempt = self._attempts.get(attempt_id)
        if attempt is None:
            raise ConnectorBrokerError(404, "Conexion desconocida o expirada", "connector_auth_not_found")
        return attempt

    def _prune(self) -> None:
        now = self._now()
        with self._lock:
            for attempt_id, attempt in list(self._attempts.items()):
                if attempt.expires_at <= now:
                    self._attempts.pop(attempt_id, None)


def _path_markers(value: str) -> tuple[str, ...]:
    markers: list[str] = []
    start = 0
    while True:
        left = value.find("{", start)
        if left < 0:
            return tuple(markers)
        right = value.find("}", left + 1)
        if right < 0:
            return tuple(markers)
        markers.append(value[left + 1:right])
        start = right + 1


def _build_base_url(
    definition: NativeDefinition, credentials: dict[str, str], *, resolve: bool = True
) -> str:
    value = definition.base_url
    for marker in _path_markers(value):
        item = credentials.get(marker, "").strip()
        if marker.endswith("_url"):
            item = _safe_https_base(item, resolve=resolve)
        elif not item or not all(ch.isalnum() or ch in "-_." for ch in item):
            raise ValueError(f"Valor invalido para {marker}")
        value = value.replace("{" + marker + "}", item)
    return _safe_https_base(value, resolve=resolve)


def _safe_https_base(value: str, *, resolve: bool = True) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("La URL del proveedor debe usar HTTPS")
    try:
        literal_ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        raise ValueError("La URL del proveedor no puede apuntar a una red privada")
    if resolve:
        try:
            addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
        except socket.gaierror as exc:
            raise ValueError("No se pudo resolver el host del proveedor") from exc
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if not ip.is_global:
                raise ValueError("La URL del proveedor no puede apuntar a una red privada")
    return value.strip().rstrip("/") + "/"


def _query_arguments(arguments: dict[str, Any]) -> list[tuple[str, Any]]:
    query: list[tuple[str, Any]] = []
    for key, value in arguments.items():
        if not isinstance(key, str) or not key or len(key) > 100:
            raise ConnectorBrokerError(400, "Parametro invalido", "bad_connector_arguments")
        if isinstance(value, (str, int, float, bool)):
            query.append((key, value))
        elif isinstance(value, list) and all(isinstance(item, (str, int, float, bool)) for item in value):
            query.extend((key, item) for item in value)
        else:
            raise ConnectorBrokerError(400, f"Parametro {key} invalido", "bad_connector_arguments")
    return query


def _request_json(url: str, *, method: str, headers: dict[str, str], body: bytes | None) -> Any:
    # Pin the TCP connection to the validated public IP while retaining the
    # provider hostname for TLS SNI/certificate verification. This closes the
    # DNS-rebinding gap that exists between validating and opening a URL.
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port or 443
    try:
        candidates = [item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]
    except socket.gaierror as exc:
        raise ConnectorBrokerError(502, "No se pudo resolver el proveedor", "connector_upstream_error") from exc
    public_ips = [address for address in candidates if ipaddress.ip_address(address).is_global]
    if not public_ips or len(public_ips) != len(candidates):
        raise ConnectorBrokerError(400, "La URL del proveedor apunta a una red privada", "bad_connector_url")
    target = parsed.path or "/"
    if parsed.query:
        target += "?" + parsed.query
    connection = _PinnedHTTPSConnection(
        logical_host=host,
        pinned_ip=public_ips[0],
        port=port,
        timeout=NATIVE_REQUEST_TIMEOUT_SECONDS,
    )
    try:
        connection.request(method, target, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_NATIVE_RESPONSE_BYTES + 1)
        content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].lower()
    except (OSError, TimeoutError, http.client.HTTPException, ssl.SSLError) as exc:
        raise ConnectorBrokerError(502, "No se pudo contactar al proveedor", "connector_upstream_error") from exc
    finally:
        connection.close()
    if response.status < 200 or response.status >= 300:
        message = "El proveedor rechazo la operacion"
        try:
            payload = json.loads(raw)
            if isinstance(payload, dict):
                candidate = payload.get("message") or payload.get("error")
                if isinstance(candidate, str) and candidate:
                    message = candidate[:500]
        except (ValueError, TypeError):
            pass
        raise ConnectorBrokerError(502, message, "connector_upstream_error")
    if len(raw) > MAX_NATIVE_RESPONSE_BYTES:
        raise ConnectorBrokerError(502, "La respuesta del proveedor excede 2 MiB", "connector_result_too_large")
    if not raw:
        return {"ok": True}
    if content_type not in {"application/json", "application/problem+json"} and not raw.lstrip().startswith((b"{", b"[")):
        raise ConnectorBrokerError(502, "El proveedor devolvio un formato no compatible", "connector_upstream_error")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ConnectorBrokerError(502, "El proveedor devolvio JSON invalido", "connector_upstream_error") from exc


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        *,
        logical_host: str,
        pinned_ip: str,
        port: int,
        timeout: int,
    ):
        super().__init__(
            logical_host,
            port=port,
            timeout=timeout,
            context=ssl.create_default_context(),
        )
        self._logical_host = logical_host
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw_socket = socket.create_connection(
            (self._pinned_ip, self.port),
            self.timeout,
            self.source_address,
        )
        self.sock = self._context.wrap_socket(raw_socket, server_hostname=self._logical_host)
