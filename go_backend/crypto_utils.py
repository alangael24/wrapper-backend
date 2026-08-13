"""Encryption for server-side connector and identity credentials.

Dos backends:
1. AES-256-GCM via `cryptography` (recomendado, portable).
   La clave maestra se deriva de WRAPPER_SECRET con PBKDF2-HMAC-SHA256; si no
   existe, se genera una aleatoria y se persiste en secret.key (0600).
2. Fallback macOS Keychain via `security` CLI (cero dependencias): el blob
   guardado en la BD es "kc:<key_id>" y el valor vive en el Keychain.

Nunca se guardan keys Go en claro.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
from pathlib import Path

_PBKDF2_ITERATIONS = 200_000
_SALT_LEN = 16
_NONCE_LEN = 12

KEYCHAIN_SERVICE = "wrapper-backend-go-keys"


class CryptoError(Exception):
    pass


def _load_or_create_secret(secret_path: Path) -> bytes:
    if secret_path.exists():
        raw = secret_path.read_bytes()
        if len(raw) != 32:
            raise CryptoError("secret.key invalido (se esperaban 32 bytes)")
        return raw
    raw = secrets.token_bytes(32)
    secret_path.write_bytes(raw)
    os.chmod(secret_path, 0o600)
    return raw


def _master_key(secret_env: str | None, secret_path: Path, *, allow_create: bool = True) -> bytes:
    if secret_env:
        salt = b"wrapper-backend"
        return hashlib.pbkdf2_hmac("sha256", secret_env.encode(), salt, _PBKDF2_ITERATIONS, dklen=32)
    if not allow_create:
        raise CryptoError("WRAPPER_SECRET es obligatorio en producción")
    return _load_or_create_secret(secret_path)


def parse_secret_versions(raw: str, *, current_version: int, current_secret: str | None) -> dict[int, str]:
    """Parsea secretos anteriores para rotación dual-read/new-write.

    ``WRAPPER_SECRET_PREVIOUS_JSON`` usa el formato ``{"1":"secret-anterior"}``.
    La versión actual siempre viene de ``WRAPPER_SECRET`` y no puede repetirse.
    """
    if current_version < 1:
        raise CryptoError("WRAPPER_SECRET_VERSION debe ser un entero positivo")
    versions: dict[int, str] = {}
    if raw.strip():
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise CryptoError("WRAPPER_SECRET_PREVIOUS_JSON debe ser JSON válido") from exc
        if not isinstance(value, dict):
            raise CryptoError("WRAPPER_SECRET_PREVIOUS_JSON debe ser un objeto")
        for raw_version, secret in value.items():
            try:
                version = int(raw_version)
            except (TypeError, ValueError) as exc:
                raise CryptoError("Las versiones de cifrado deben ser enteros positivos") from exc
            if version < 1 or not isinstance(secret, str) or len(secret) < 24:
                raise CryptoError("Cada secreto anterior debe tener al menos 24 caracteres")
            versions[version] = secret
    if current_secret:
        if current_version in versions:
            raise CryptoError("La versión actual no debe repetirse entre los secretos anteriores")
        versions[current_version] = current_secret
    return versions


def _secret_for_version(
    *,
    key_version: int,
    current_secret: str | None,
    secret_versions: dict[int, str] | None,
) -> str | None:
    if secret_versions:
        secret = secret_versions.get(key_version)
        if not secret:
            raise CryptoError(f"No existe WRAPPER_SECRET para la versión {key_version}")
        return secret
    return current_secret


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM
    except ImportError:
        return None


def encrypt_api_key(
    plaintext: str,
    key_id: str,
    secret_env: str | None,
    secret_path: Path,
    *,
    key_version: int = 1,
    secret_versions: dict[int, str] | None = None,
    allow_secret_file: bool = True,
) -> bytes:
    """Cifra una key Go y devuelve el blob para guardar en la BD."""
    selected_secret = _secret_for_version(
        key_version=key_version,
        current_secret=secret_env,
        secret_versions=secret_versions,
    )
    if not allow_secret_file and not selected_secret:
        raise CryptoError("WRAPPER_SECRET es obligatorio en producción")
    aesgcm = _aesgcm()
    if aesgcm is not None:
        master = _master_key(selected_secret, secret_path, allow_create=allow_secret_file)
        salt = secrets.token_bytes(_SALT_LEN)
        key = hashlib.pbkdf2_hmac("sha256", master, salt, _PBKDF2_ITERATIONS, dklen=32)
        nonce = secrets.token_bytes(_NONCE_LEN)
        ct = aesgcm(key).encrypt(nonce, plaintext.encode(), None)
        return f"aesv{key_version}:".encode() + salt + nonce + ct
    # Fallback: Keychain de macOS
    keychain_put(key_id, plaintext)
    return b"kc:" + key_id.encode()


def decrypt_api_key(
    blob: bytes,
    key_id: str,
    secret_env: str | None,
    secret_path: Path,
    *,
    key_version: int = 1,
    secret_versions: dict[int, str] | None = None,
    allow_secret_file: bool = True,
) -> str:
    aesgcm = _aesgcm()
    prefix = f"aesv{key_version}:".encode()
    if blob.startswith(prefix) or (key_version == 1 and blob.startswith(b"aes:")):
        if aesgcm is None:
            raise CryptoError("blob AES pero cryptography no esta instalado")
        raw = blob[len(prefix):] if blob.startswith(prefix) else blob[4:]
        if len(raw) < _SALT_LEN + _NONCE_LEN:
            raise CryptoError("blob cifrado invalido")
        salt, nonce, ct = raw[:_SALT_LEN], raw[_SALT_LEN:_SALT_LEN + _NONCE_LEN], raw[_SALT_LEN + _NONCE_LEN:]
        selected_secret = _secret_for_version(
            key_version=key_version,
            current_secret=secret_env,
            secret_versions=secret_versions,
        )
        master = _master_key(selected_secret, secret_path, allow_create=allow_secret_file)
        key = hashlib.pbkdf2_hmac("sha256", master, salt, _PBKDF2_ITERATIONS, dklen=32)
        return aesgcm(key).decrypt(nonce, ct, None).decode()
    if blob.startswith(b"kc:"):
        return keychain_get(blob[3:].decode())
    raise CryptoError("blob desconocido (corrupto?)")


def keychain_put(key_id: str, value: str) -> None:
    """Guarda un secreto en el Keychain de macOS (solo si 'security' existe)."""
    import shutil

    if not shutil.which("security"):
        raise CryptoError("no hay cryptography ni Keychain disponible para cifrar")
    subprocess.run(
        ["security", "add-generic-password", "-U", "-a", "wrapper-backend", "-s", f"{KEYCHAIN_SERVICE}:{key_id}", "-w", value],
        check=True,
        capture_output=True,
    )


def keychain_get(key_id: str) -> str:
    import shutil

    if not shutil.which("security"):
        raise CryptoError("no hay cryptography ni Keychain disponible para descifrar")
    out = subprocess.run(
        ["security", "find-generic-password", "-a", "wrapper-backend", "-s", f"{KEYCHAIN_SERVICE}:{key_id}", "-w"],
        check=True,
        capture_output=True,
    )
    return out.stdout.decode().rstrip("\n")


def hash_wrapper_key(api_key: str) -> str:
    """Hash de las keys de los usuarios del wrapper (nunca se guardan en claro)."""
    return hashlib.sha256(("wrapper|" + api_key).encode()).hexdigest()
