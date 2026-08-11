"""OAuth 2.0/OIDC de Google para cuentas personales de Agent Genia.

El backend usa Authorization Code + PKCE, obtiene la identidad desde UserInfo
y emite sus propios tokens opacos. Los tokens de Google no se persisten: solo
se guardan hashes de las sesiones de Agent Genia ligados al dispositivo.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from .store import Store, new_id

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_SCOPES = "openid email profile"


class GoogleAuthError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400, code: str = "google_auth_error"):
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class _Attempt:
    id: str
    state: str
    device_id: str
    verifier: str
    expires_at: float
    status: str = "pending"
    message: str = ""
    result: dict[str, Any] | None = None


@dataclass
class _RateBucket:
    timestamps: deque[float] = field(default_factory=deque)


class GoogleAccountAuth:
    def __init__(
        self,
        *,
        store: Store,
        client_id: str | None,
        client_secret: str | None,
        redirect_uri: str | None,
        access_ttl_seconds: int = 900,
        refresh_ttl_seconds: int = 30 * 86400,
        attempt_ttl_seconds: int = 600,
    ):
        self.store = store
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.redirect_uri = (redirect_uri or "").strip()
        self.access_ttl_seconds = max(300, min(int(access_ttl_seconds), 3600))
        self.refresh_ttl_seconds = max(3600, min(int(refresh_ttl_seconds), 90 * 86400))
        self.attempt_ttl_seconds = max(120, min(int(attempt_ttl_seconds), 900))
        self._attempts: dict[str, _Attempt] = {}
        self._attempt_by_state: dict[str, str] = {}
        self._rate: dict[str, _RateBucket] = defaultdict(_RateBucket)
        self._lock = threading.RLock()
        self._validate_configuration()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.redirect_uri)

    def _validate_configuration(self) -> None:
        values = (self.client_id, self.client_secret, self.redirect_uri)
        if any(values) and not all(values):
            raise GoogleAuthError(
                "Google OAuth está configurado parcialmente; define client id, client secret y redirect URI.",
                status=500,
                code="unsafe_configuration",
            )
        if not self.configured:
            return
        parsed = urllib.parse.urlparse(self.redirect_uri)
        loopback = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if (parsed.scheme != "https" and not loopback) or parsed.username or parsed.password:
            raise GoogleAuthError(
                "GOOGLE_OAUTH_REDIRECT_URI debe ser HTTPS (salvo loopback local).",
                status=500,
                code="unsafe_configuration",
            )

    def start(self, *, device_id: str, app_version: str, remote_key: str) -> dict:
        self._require_configured()
        self._validate_device_id(device_id)
        if len(app_version) > 100:
            raise GoogleAuthError("app_version inválido", code="invalid_request")
        # El límite por dispositivo evita loops del cliente y el límite global
        # por origen contiene abuso sin impedir varios logins legítimos detrás
        # del mismo proxy corporativo.
        self._check_rate(f"start-device:{device_id}", limit=5, window_seconds=60)
        self._check_rate(f"start-origin:{remote_key}", limit=120, window_seconds=60)
        self._cleanup()

        attempt_id = new_id("auth")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).rstrip(b"=").decode()
        attempt = _Attempt(
            id=attempt_id,
            state=state,
            device_id=device_id,
            verifier=verifier,
            expires_at=time.time() + self.attempt_ttl_seconds,
        )
        with self._lock:
            self._attempts[attempt_id] = attempt
            self._attempt_by_state[state] = attempt_id

        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": GOOGLE_SCOPES,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "include_granted_scopes": "true",
            }
        )
        return {
            "attempt_id": attempt_id,
            "authorize_url": f"{GOOGLE_AUTHORIZE_URL}?{query}",
            "expires_in": self.attempt_ttl_seconds,
            "provider": "google",
        }

    def status(self, *, attempt_id: str, device_id: str) -> dict:
        self._validate_device_id(device_id)
        self._cleanup()
        with self._lock:
            attempt = self._attempts.get(attempt_id)
            if attempt is None or not secrets.compare_digest(attempt.device_id, device_id):
                raise GoogleAuthError("Intento de acceso no encontrado", status=404, code="not_found")
            if attempt.expires_at <= time.time() and attempt.status != "complete":
                attempt.status = "error"
                attempt.message = "El inicio de sesión expiró. Inténtalo de nuevo."
            if attempt.status == "complete" and attempt.result:
                return {"status": "complete", **attempt.result}
            if attempt.status == "error":
                return {"status": "error", "message": attempt.message}
            return {"status": "pending"}

    def callback(self, params: dict[str, list[str]]) -> None:
        state = self._first(params, "state")
        if not state:
            raise GoogleAuthError("Google no devolvió state", code="invalid_state")
        with self._lock:
            attempt_id = self._attempt_by_state.pop(state, None)
            attempt = self._attempts.get(attempt_id or "")
            if attempt is None or not secrets.compare_digest(attempt.state, state):
                raise GoogleAuthError("El state de Google no es válido o ya fue usado", code="invalid_state")
            if attempt.expires_at <= time.time():
                attempt.status = "error"
                attempt.message = "El inicio de sesión expiró. Inténtalo de nuevo."
                raise GoogleAuthError(attempt.message, code="expired_attempt")
            if attempt.status != "pending":
                raise GoogleAuthError("Este inicio de sesión ya fue procesado", code="replayed_callback")
            attempt.status = "exchanging"

        provider_error = self._first(params, "error")
        if provider_error:
            self._fail(attempt, "Google canceló o rechazó el inicio de sesión.")
            return
        code = self._first(params, "code")
        if not code:
            self._fail(attempt, "Google no devolvió un código de autorización.")
            return

        try:
            google_tokens = self._exchange_code(code=code, verifier=attempt.verifier)
            access_token = google_tokens.get("access_token")
            if not isinstance(access_token, str) or len(access_token) < 10:
                raise GoogleAuthError("Google no devolvió un access token válido")
            profile = self._fetch_userinfo(access_token)
            subject = profile.get("sub")
            email = profile.get("email")
            if not isinstance(subject, str) or not subject:
                raise GoogleAuthError("Google no devolvió un identificador de cuenta")
            if not isinstance(email, str) or "@" not in email or profile.get("email_verified") is not True:
                raise GoogleAuthError("Google no confirmó una dirección de correo verificada")
            name = profile.get("name") if isinstance(profile.get("name"), str) else None
            picture = profile.get("picture") if isinstance(profile.get("picture"), str) else None
            account = self.store.get_or_create_google_account(
                subject=subject,
                email=email,
                name=name,
                picture=picture,
            )
            token_result = self._create_session(account=account, device_id=attempt.device_id)
            with self._lock:
                attempt.result = token_result
                attempt.status = "complete"
                attempt.verifier = ""
        except GoogleAuthError as exc:
            self._fail(attempt, str(exc))
        except Exception:
            self._fail(attempt, "No fue posible verificar tu cuenta con Google.")

    def refresh(self, *, refresh_token: str, device_id: str, remote_key: str) -> dict:
        self._validate_device_id(device_id)
        self._check_rate(f"refresh-device:{device_id}", limit=20, window_seconds=60)
        self._check_rate(f"refresh-origin:{remote_key}", limit=300, window_seconds=60)
        if not refresh_token.startswith("agr_") or len(refresh_token) < 40:
            raise GoogleAuthError("Refresh token inválido", status=401, code="unauthorized")
        now = time.time()
        access_token = "aga_" + secrets.token_urlsafe(48)
        next_refresh = "agr_" + secrets.token_urlsafe(64)
        access_expires_at = now + self.access_ttl_seconds
        refresh_expires_at = now + self.refresh_ttl_seconds
        account = self.store.rotate_account_session(
            refresh_token=refresh_token,
            device_id=device_id,
            new_access_token=access_token,
            new_refresh_token=next_refresh,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
        )
        if account is None:
            raise GoogleAuthError(
                "La sesión expiró o pertenece a otro dispositivo. Inicia sesión nuevamente.",
                status=401,
                code="unauthorized",
            )
        return {
            "token": access_token,
            "refresh_token": next_refresh,
            "expires_at": round(access_expires_at * 1000),
            "account": self._account_payload(account),
        }

    def authenticate(self, access_token: str) -> dict | None:
        if not access_token.startswith("aga_"):
            return None
        return self.store.get_user_by_access_token(access_token)

    def logout(self, access_token: str) -> bool:
        return self.store.revoke_account_session(access_token)

    def _create_session(self, *, account: dict, device_id: str) -> dict:
        now = time.time()
        access_token = "aga_" + secrets.token_urlsafe(48)
        refresh_token = "agr_" + secrets.token_urlsafe(64)
        access_expires_at = now + self.access_ttl_seconds
        refresh_expires_at = now + self.refresh_ttl_seconds
        self.store.create_account_session(
            account_id=account["id"],
            device_id=device_id,
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=access_expires_at,
            refresh_expires_at=refresh_expires_at,
        )
        return {
            "token": access_token,
            "refresh_token": refresh_token,
            "expires_at": round(access_expires_at * 1000),
            "account": self._account_payload(account),
        }

    @staticmethod
    def _account_payload(account: dict) -> dict:
        return {
            "id": account["id"],
            "email": account["email"],
            "name": account.get("name") or "",
            "picture": account.get("picture") or "",
        }

    def _exchange_code(self, *, code: str, verifier: str) -> dict:
        body = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri,
            }
        ).encode()
        request = urllib.request.Request(
            GOOGLE_TOKEN_URL,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return self._read_google_json(request)

    def _fetch_userinfo(self, access_token: str) -> dict:
        request = urllib.request.Request(
            GOOGLE_USERINFO_URL,
            headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        )
        return self._read_google_json(request)

    @staticmethod
    def _read_google_json(request: urllib.request.Request) -> dict:
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise GoogleAuthError("Google rechazó el intercambio de autorización") from exc
        if not isinstance(data, dict):
            raise GoogleAuthError("Google devolvió una respuesta inválida")
        return data

    def _require_configured(self) -> None:
        if not self.configured:
            raise GoogleAuthError(
                "Google todavía no está configurado en este servidor.",
                status=503,
                code="google_not_configured",
            )

    @staticmethod
    def _validate_device_id(device_id: str) -> None:
        try:
            parsed = uuid.UUID(device_id)
        except (ValueError, AttributeError) as exc:
            raise GoogleAuthError("device_id inválido", code="invalid_request") from exc
        if str(parsed) != device_id.lower():
            raise GoogleAuthError("device_id inválido", code="invalid_request")

    def _check_rate(self, key: str, *, limit: int, window_seconds: int) -> None:
        now = time.monotonic()
        with self._lock:
            if len(self._rate) > 10_000:
                stale = [
                    bucket_key
                    for bucket_key, bucket_value in self._rate.items()
                    if not bucket_value.timestamps
                    or bucket_value.timestamps[-1] <= now - 3600
                ]
                for bucket_key in stale:
                    self._rate.pop(bucket_key, None)
                if len(self._rate) > 10_000 and key not in self._rate:
                    raise GoogleAuthError(
                        "Demasiados intentos. Espera un minuto.",
                        status=429,
                        code="rate_limit",
                    )
            bucket = self._rate[key].timestamps
            while bucket and bucket[0] <= now - window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                raise GoogleAuthError(
                    "Demasiados intentos. Espera un minuto.",
                    status=429,
                    code="rate_limit",
                )
            bucket.append(now)

    def _cleanup(self) -> None:
        cutoff = time.time() - 60
        with self._lock:
            expired = [
                attempt_id
                for attempt_id, attempt in self._attempts.items()
                if attempt.expires_at < cutoff
            ]
            for attempt_id in expired:
                attempt = self._attempts.pop(attempt_id)
                self._attempt_by_state.pop(attempt.state, None)

    def _fail(self, attempt: _Attempt, message: str) -> None:
        with self._lock:
            attempt.status = "error"
            attempt.message = message
            attempt.verifier = ""

    @staticmethod
    def _first(params: dict[str, list[str]], key: str) -> str:
        values = params.get(key) or []
        return values[0] if values else ""


def completion_html() -> bytes:
    return (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
        "<title>Agent Genia</title><style>body{font:16px system-ui;background:#f7f7f7;color:#171717;display:grid;place-items:center;min-height:100vh;margin:0}"
        "main{background:white;border:1px solid #ddd;border-radius:20px;padding:32px;max-width:430px;text-align:center}"
        "h1{font-size:24px;margin:0 0 10px}p{color:#666;line-height:1.5;margin:0}</style></head>"
        "<body><main><h1>Listo</h1><p>Regresa a Agent Genia para continuar.</p></main></body></html>"
    ).encode()
