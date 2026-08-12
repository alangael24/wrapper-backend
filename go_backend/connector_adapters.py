"""Gateway privado de conectores de Agent Genia.

Todo el consentimiento, descubrimiento y uso de herramientas ocurre en este
proceso. Electron conserva solamente la sesion de Agent Genia y una marca local
de UX; Pi recibe un grant efimero sin credenciales. Composio conserva y renueva
los tokens OAuth de cada usuario.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .connectors import CONNECTOR_CATALOG, ConnectorBrokerError
from .native_connectors import NativeConnectorGateway


ACTIVE = "ACTIVE"
TERMINAL_CONNECTION_STATES = frozenset({"FAILED", "EXPIRED", "REVOKED", "DELETED"})
AUTH_ATTEMPT_TTL_SECONDS = 10 * 60
AUTH_STARTS_PER_MINUTE = 12
UPSTREAM_POLL_INTERVAL_SECONDS = 2.0

# Nombres verificados contra el catalogo de toolkits de Composio. Los
# overrides de entorno permiten incorporar o renombrar toolkits sin publicar
# una nueva app de escritorio.
COMPOSIO_TOOLKITS: dict[str, str] = {
    "google-workspace": "google_super",
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

# Estos proveedores no tienen una app administrada estable para todos los
# proyectos. Se habilitan solo cuando el servidor recibe un ac_... propio.
CUSTOM_AUTH_REQUIRED = frozenset({
    "salesforce", "docusign", "outreach", "clay", "zoominfo", "netsuite",
    "ramp", "workday", "tableau", "snowflake", "woocommerce",
})


@dataclass
class _AuthAttempt:
    user_id: str
    connector_id: str
    connected_account_id: str
    expires_at: float
    session: Any
    next_upstream_poll_at: float = 0.0


class ComposioConnectorGateway:
    """Administra Connect Links y ejecuciones, siempre aisladas por user_id."""

    def __init__(
        self,
        *,
        api_key: str = "",
        public_base_url: str = "",
        auth_configs: dict[str, str] | None = None,
        toolkit_overrides: dict[str, str] | None = None,
        client: Any = None,
        native_gateway: NativeConnectorGateway | None = None,
        now=time.monotonic,
        attempt_ttl_seconds: int = AUTH_ATTEMPT_TTL_SECONDS,
    ):
        self.api_key = api_key.strip()
        self.public_base_url = _validate_public_base_url(public_base_url)
        self.auth_configs = _validated_mapping(auth_configs or {}, prefix="ac_")
        self.toolkits = dict(COMPOSIO_TOOLKITS)
        self.toolkits.update(_validated_toolkit_overrides(toolkit_overrides or {}))
        self._now = now
        self._attempt_ttl_seconds = max(60, min(int(attempt_ttl_seconds), 1800))
        self._attempts: dict[str, _AuthAttempt] = {}
        self._start_rate: dict[str, deque[float]] = defaultdict(deque)
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

    @property
    def configured(self) -> bool:
        return self.client is not None

    def health(self) -> dict[str, Any]:
        available = sum(
            1 for connector_id in CONNECTOR_CATALOG if self.describe(connector_id)["available"]
        )
        return {
            "configured": self.configured or bool(self.native_gateway and self.native_gateway.configured),
            "available_connectors": available,
            "catalog_connectors": len(CONNECTOR_CATALOG),
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
        descriptions = [self.describe(connector_id) for connector_id in CONNECTOR_CATALOG]
        if not self.configured:
            return [
                self.native_gateway.status(user_id, item["connector_id"])
                if item.get("driver") == "native" and self.native_gateway
                else {**item, "connected": False, "account": ""}
                for item in descriptions
            ]
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
            toolkit = _account_toolkit(account)
            if toolkit and toolkit not in by_toolkit:
                by_toolkit[toolkit] = account
        snapshot: list[dict[str, Any]] = []
        for item in descriptions:
            if item.get("driver") == "native":
                snapshot.append(self.native_gateway.status(user_id, item["connector_id"]))  # type: ignore[union-attr]
            else:
                snapshot.append({
                    **item,
                    "connected": item["available"] and item["toolkit"] in by_toolkit,
                    "account": _account_label(by_toolkit.get(item["toolkit"])),
                })
        return snapshot

    def start(self, user_id: str, connector_id: str) -> dict[str, str]:
        self._prune()
        self._check_start_rate(user_id)
        description = self.describe(connector_id)
        if not description["available"]:
            raise ConnectorBrokerError(409, description["reason"], "connector_not_configured")
        if description.get("driver") == "native":
            return self.native_gateway.start(user_id, connector_id)  # type: ignore[union-attr]
        session = self._session(user_id, connector_id)
        callback_url = (
            f"{self.public_base_url}/connections/complete" if self.public_base_url else None
        )
        try:
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
        with self._lock:
            self._attempts[attempt_id] = _AuthAttempt(
                user_id=user_id,
                connector_id=connector_id,
                connected_account_id=account_id,
                expires_at=self._now() + self._attempt_ttl_seconds,
                session=session,
            )
        return {"attempt_id": attempt_id, "authorize_url": authorize_url}

    def poll(self, user_id: str, attempt_id: str) -> dict[str, Any]:
        if attempt_id.startswith("nat_") and self.native_gateway:
            return self.native_gateway.poll(user_id, attempt_id)
        self._prune()
        with self._lock:
            attempt = self._attempts.get(attempt_id)
        if attempt is None or attempt.user_id != user_id:
            raise ConnectorBrokerError(
                404, "Conexion desconocida o expirada", "connector_auth_not_found"
            )
        now = self._now()
        with self._lock:
            if attempt.next_upstream_poll_at > now:
                return {"status": "pending"}
            attempt.next_upstream_poll_at = now + UPSTREAM_POLL_INTERVAL_SECONDS
        try:
            account = self.client.connected_accounts.get(attempt.connected_account_id)
            state = str(getattr(account, "status", "") or "").upper()
        except Exception as exc:
            raise _upstream_error(exc, "No se pudo consultar la autorizacion") from exc
        if state == ACTIVE:
            self._finish_attempt(attempt_id)
            return {
                "status": "complete",
                "session": {
                    "managed_connection_id": attempt.connected_account_id,
                    "connector_id": attempt.connector_id,
                    "account_label": _account_label(account) or attempt.connected_account_id,
                },
            }
        if state in TERMINAL_CONNECTION_STATES:
            self._finish_attempt(attempt_id)
            return {
                "status": "error",
                "message": str(getattr(account, "status_reason", "") or "El proveedor rechazo la conexion"),
            }
        return {"status": "pending"}

    def disconnect(self, user_id: str, connector_id: str) -> dict[str, bool]:
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
        except Exception as exc:
            raise _upstream_error(exc, "No se pudo desconectar la cuenta") from exc
        return {"disconnected": True}

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
        session = self._session(user_id, connector_id)
        try:
            search = session.search(
                query=(
                    f"Use {CONNECTOR_CATALOG[connector_id]['name']} to perform the "
                    f"operation '{operation.replace('_', ' ')}'."
                )
            )
            results = list(getattr(search, "results", []) or [])
            slugs = list(getattr(results[0], "primary_tool_slugs", []) or []) if results else []
            if not slugs:
                raise ConnectorBrokerError(
                    404,
                    "No encontramos una operacion compatible en el toolkit",
                    "connector_operation_not_found",
                )
            result = session.execute(slugs[0], arguments=arguments)
            error = getattr(result, "error", None)
            if error:
                raise ConnectorBrokerError(502, str(error)[:500], "connector_upstream_error")
            return _json_value(getattr(result, "data", result))
        except ConnectorBrokerError:
            raise
        except Exception as exc:
            raise _upstream_error(exc, "El proveedor rechazo la operacion") from exc
        finally:
            _delete_session(session)

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

    def _finish_attempt(self, attempt_id: str) -> None:
        with self._lock:
            attempt = self._attempts.pop(attempt_id, None)
        if attempt is not None:
            _delete_session(attempt.session)

    def _prune(self) -> None:
        expired: list[_AuthAttempt] = []
        now = self._now()
        with self._lock:
            for attempt_id, attempt in list(self._attempts.items()):
                if attempt.expires_at <= now:
                    expired.append(self._attempts.pop(attempt_id))
            for user_id, bucket in list(self._start_rate.items()):
                while bucket and bucket[0] <= now - 60:
                    bucket.popleft()
                if not bucket:
                    self._start_rate.pop(user_id, None)
        for attempt in expired:
            _delete_session(attempt.session)

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


def _validated_mapping(value: dict[str, str], *, prefix: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, item in value.items():
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", key) or not item.startswith(prefix):
            raise ValueError("COMPOSIO_AUTH_CONFIGS_JSON contiene un valor invalido")
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


def _delete_session(session: Any) -> None:
    try:
        session.delete()
    except Exception:
        pass


def _upstream_error(exc: Exception, fallback: str) -> ConnectorBrokerError:
    message = str(exc).strip()
    # Nunca devolver representaciones que pudieran incluir headers o secretos.
    if not message or any(marker in message.lower() for marker in ("x-api-key", "bearer ", "api_key")):
        message = fallback
    return ConnectorBrokerError(502, message[:500], "connector_upstream_error")


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
