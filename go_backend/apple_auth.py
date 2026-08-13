"""Native Sign in with Apple for Agent Genia iOS.

The app sends the short-lived identity token, one-time authorization code and
the original nonce. The backend verifies Apple's signature and claims, consumes
the token once, exchanges the code, and stores only an encrypted refresh token
so Apple can be revoked when the user deletes the Agent Genia account.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict, deque
from pathlib import Path
import threading
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from .crypto_utils import decrypt_api_key, encrypt_api_key


APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
APPLE_TOKEN_URL = "https://appleid.apple.com/auth/token"
APPLE_REVOKE_URL = "https://appleid.apple.com/auth/revoke"


class AppleAuthError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400, code: str = "apple_auth_error"):
        super().__init__(message)
        self.status = status
        self.code = code


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _json_segment(value: dict[str, Any]) -> str:
    return _b64url_encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())


class AppleAccountAuth:
    def __init__(
        self,
        *,
        store: Any,
        session_issuer: Any,
        client_id: str | None,
        team_id: str | None,
        key_id: str | None,
        private_key_base64: str | None,
        secret_env: str | None,
        secret_path: Path,
        key_version: int,
        secret_versions: dict[int, str],
        allow_secret_file: bool,
    ):
        self.store = store
        self.session_issuer = session_issuer
        self.client_id = (client_id or "").strip()
        self.team_id = (team_id or "").strip()
        self.key_id = (key_id or "").strip()
        self.private_key_base64 = (private_key_base64 or "").strip()
        self.secret_env = secret_env
        self.secret_path = secret_path
        self.key_version = key_version
        self.secret_versions = secret_versions
        self.allow_secret_file = allow_secret_file
        self._jwks: dict[str, Any] = {}
        self._jwks_expires_at = 0.0
        self._private_key: ec.EllipticCurvePrivateKey | None = None
        self._rate: dict[str, deque[float]] = defaultdict(deque)
        self._rate_lock = threading.RLock()
        self._validate_configuration()

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.team_id and self.key_id and self.private_key_base64)

    def _validate_configuration(self) -> None:
        values = (self.client_id, self.team_id, self.key_id, self.private_key_base64)
        if any(values) and not all(values):
            raise AppleAuthError(
                "Sign in with Apple está configurado parcialmente.",
                status=500,
                code="unsafe_configuration",
            )
        if not self.configured:
            return
        try:
            raw = base64.b64decode(self.private_key_base64, validate=True)
            key = serialization.load_pem_private_key(raw, password=None)
        except Exception as exc:
            raise AppleAuthError(
                "APPLE_PRIVATE_KEY_BASE64 no contiene una llave privada PEM válida.",
                status=500,
                code="unsafe_configuration",
            ) from exc
        if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise AppleAuthError(
                "La llave de Apple debe ser EC P-256.",
                status=500,
                code="unsafe_configuration",
            )
        self._private_key = key

    def login(
        self,
        *,
        identity_token: str,
        authorization_code: str,
        nonce: str,
        device_id: str,
        name: str | None,
        remote_key: str = "",
    ) -> dict[str, Any]:
        self._require_configured()
        self._validate_device_id(device_id)
        self._check_rate(f"device:{device_id}", limit=5)
        self._check_rate(f"origin:{remote_key}", limit=120)
        if not 16 <= len(nonce) <= 256:
            raise AppleAuthError("Nonce de Apple inválido", code="invalid_request")
        if not 20 <= len(identity_token) <= 20_000 or not 4 <= len(authorization_code) <= 4_096:
            raise AppleAuthError("Credenciales de Apple inválidas", code="invalid_request")
        expected_nonce = hashlib.sha256(nonce.encode()).hexdigest()
        claims = self._verify_identity_token(identity_token, expected_nonce=expected_nonce)
        refresh_token = self._exchange_code(authorization_code)
        subject = claims["sub"]
        email_value = claims.get("email")
        email = email_value if isinstance(email_value, str) and "@" in email_value else None
        token_hash = hashlib.sha256(("apple-identity|" + identity_token).encode()).hexdigest()
        try:
            account = self.store.get_or_create_federated_account(
                provider="apple",
                subject=subject,
                email=email,
                name=(name or "").strip()[:200] or None,
                picture=None,
                identity_token_hash=token_hash,
                token_expires_at=float(claims["exp"]),
            )
        except PermissionError as exc:
            raise AppleAuthError(str(exc), status=401, code="apple_token_reused") from exc
        except ValueError as exc:
            raise AppleAuthError(str(exc), status=400, code="apple_identity_incomplete") from exc

        credential_key_id = f"apple-refresh|{account['id']}"
        credential_enc = encrypt_api_key(
            refresh_token,
            credential_key_id,
            self.secret_env,
            self.secret_path,
            key_version=self.key_version,
            secret_versions=self.secret_versions,
            allow_secret_file=self.allow_secret_file,
        )
        self.store.put_account_provider_credential(
            account_id=account["id"],
            provider="apple",
            credential_enc=credential_enc,
            key_id=credential_key_id,
            key_version=self.key_version,
        )
        return self.session_issuer.issue_session(account=account, device_id=device_id)

    def _check_rate(self, key: str, *, limit: int) -> None:
        now = time.monotonic()
        with self._rate_lock:
            if len(self._rate) > 10_000 and key not in self._rate:
                stale = [name for name, bucket in self._rate.items() if not bucket or bucket[-1] <= now - 60]
                for name in stale:
                    self._rate.pop(name, None)
                if len(self._rate) > 10_000:
                    raise AppleAuthError(
                        "Demasiados intentos de acceso. Espera un minuto.",
                        status=429,
                        code="rate_limit",
                    )
            bucket = self._rate[key]
            while bucket and bucket[0] <= now - 60:
                bucket.popleft()
            if len(bucket) >= limit:
                raise AppleAuthError(
                    "Demasiados intentos de acceso. Espera un minuto.",
                    status=429,
                    code="rate_limit",
                )
            bucket.append(now)

    def revoke_user(self, user_id: str) -> bool:
        credential = self.store.get_account_provider_credential(user_id, "apple")
        if not credential:
            return False
        refresh_token = decrypt_api_key(
            bytes(credential["credential_enc"]),
            credential["key_id"],
            self.secret_env,
            self.secret_path,
            key_version=int(credential.get("key_version") or 1),
            secret_versions=self.secret_versions,
            allow_secret_file=self.allow_secret_file,
        )
        body = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "client_secret": self._client_secret(),
                "token": refresh_token,
                "token_type_hint": "refresh_token",
            }
        ).encode()
        request = urllib.request.Request(
            APPLE_REVOKE_URL,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                if response.status not in {200, 201}:
                    raise AppleAuthError("Apple rechazó la revocación", status=502)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            raise AppleAuthError(
                "No fue posible revocar Sign in with Apple. Intenta nuevamente.",
                status=502,
                code="apple_revoke_failed",
            ) from exc
        return True

    def _exchange_code(self, authorization_code: str) -> str:
        body = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "client_secret": self._client_secret(),
                "code": authorization_code,
                "grant_type": "authorization_code",
            }
        ).encode()
        request = urllib.request.Request(
            APPLE_TOKEN_URL,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read())
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AppleAuthError(
                "Apple rechazó el código de autorización.",
                status=401,
                code="apple_code_rejected",
            ) from exc
        refresh_token = payload.get("refresh_token") if isinstance(payload, dict) else None
        if not isinstance(refresh_token, str) or len(refresh_token) < 20:
            raise AppleAuthError(
                "Apple no devolvió una credencial revocable.",
                status=502,
                code="apple_response_invalid",
            )
        return refresh_token

    def _verify_identity_token(self, token: str, *, expected_nonce: str) -> dict[str, Any]:
        parts = token.split(".")
        if len(parts) != 3:
            raise AppleAuthError("Identity token de Apple inválido", status=401)
        try:
            header = json.loads(_b64url_decode(parts[0]))
            claims = json.loads(_b64url_decode(parts[1]))
            signature = _b64url_decode(parts[2])
        except (ValueError, json.JSONDecodeError) as exc:
            raise AppleAuthError("Identity token de Apple inválido", status=401) from exc
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise AppleAuthError("Identity token de Apple inválido", status=401)
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise AppleAuthError("Algoritmo de Apple no permitido", status=401)
        key = self._apple_public_key(header["kid"])
        try:
            key.verify(
                signature,
                f"{parts[0]}.{parts[1]}".encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except Exception as exc:
            raise AppleAuthError("Firma de Apple inválida", status=401) from exc
        now = int(time.time())
        audience = claims.get("aud")
        audience_ok = audience == self.client_id or (
            isinstance(audience, list) and self.client_id in audience
        )
        if claims.get("iss") != APPLE_ISSUER or not audience_ok:
            raise AppleAuthError("Claims de Apple inválidos", status=401)
        exp = claims.get("exp")
        iat = claims.get("iat")
        if (
            not isinstance(exp, int)
            or isinstance(exp, bool)
            or exp <= now
            or not isinstance(iat, int)
            or isinstance(iat, bool)
            or iat > now + 300
        ):
            raise AppleAuthError("Identity token de Apple expirado", status=401)
        if not isinstance(claims.get("sub"), str) or not claims["sub"]:
            raise AppleAuthError("Apple no devolvió un identificador", status=401)
        if not secrets.compare_digest(str(claims.get("nonce") or ""), expected_nonce):
            raise AppleAuthError("Nonce de Apple inválido", status=401, code="invalid_nonce")
        return claims

    def _apple_public_key(self, key_id: str) -> rsa.RSAPublicKey:
        now = time.monotonic()
        if now >= self._jwks_expires_at or key_id not in self._jwks:
            request = urllib.request.Request(APPLE_JWKS_URL, headers={"Accept": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = json.loads(response.read())
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise AppleAuthError(
                    "No fue posible verificar la firma de Apple.",
                    status=503,
                    code="apple_keys_unavailable",
                ) from exc
            keys = payload.get("keys") if isinstance(payload, dict) else None
            if not isinstance(keys, list):
                raise AppleAuthError("Apple devolvió llaves inválidas", status=503)
            self._jwks = {
                item["kid"]: item
                for item in keys
                if isinstance(item, dict) and isinstance(item.get("kid"), str)
            }
            self._jwks_expires_at = now + 6 * 3600
        jwk = self._jwks.get(key_id)
        if not isinstance(jwk, dict) or jwk.get("kty") != "RSA":
            raise AppleAuthError("Llave de firma de Apple desconocida", status=401)
        try:
            modulus = int.from_bytes(_b64url_decode(jwk["n"]), "big")
            exponent = int.from_bytes(_b64url_decode(jwk["e"]), "big")
            return rsa.RSAPublicNumbers(exponent, modulus).public_key()
        except Exception as exc:
            raise AppleAuthError("Llave de firma de Apple inválida", status=503) from exc

    def _client_secret(self) -> str:
        if self._private_key is None:  # pragma: no cover - validated during startup
            raise AppleAuthError("Sign in with Apple no está configurado", status=503)
        now = int(time.time())
        header = _json_segment({"alg": "ES256", "kid": self.key_id, "typ": "JWT"})
        payload = _json_segment(
            {
                "iss": self.team_id,
                "iat": now,
                "exp": now + 300,
                "aud": APPLE_ISSUER,
                "sub": self.client_id,
            }
        )
        signing_input = f"{header}.{payload}".encode()
        der_signature = self._private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der_signature)
        signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{header}.{payload}.{_b64url_encode(signature)}"

    def _require_configured(self) -> None:
        if not self.configured:
            raise AppleAuthError(
                "Sign in with Apple todavía no está configurado.",
                status=503,
                code="apple_not_configured",
            )

    @staticmethod
    def _validate_device_id(device_id: str) -> None:
        try:
            parsed = uuid.UUID(device_id)
        except (ValueError, AttributeError) as exc:
            raise AppleAuthError("device_id inválido", code="invalid_request") from exc
        if str(parsed) != device_id.lower():
            raise AppleAuthError("device_id inválido", code="invalid_request")
