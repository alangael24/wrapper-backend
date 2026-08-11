"""Cifrado de las API keys de OpenCode Go en reposo.

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


def _master_key(secret_env: str | None, secret_path: Path) -> bytes:
    if secret_env:
        salt = b"wrapper-backend"
        return hashlib.pbkdf2_hmac("sha256", secret_env.encode(), salt, _PBKDF2_ITERATIONS, dklen=32)
    return _load_or_create_secret(secret_path)


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM
    except ImportError:
        return None


def encrypt_api_key(plaintext: str, key_id: str, secret_env: str | None, secret_path: Path) -> bytes:
    """Cifra una key Go y devuelve el blob para guardar en la BD."""
    aesgcm = _aesgcm()
    if aesgcm is not None:
        master = _master_key(secret_env, secret_path)
        salt = secrets.token_bytes(_SALT_LEN)
        key = hashlib.pbkdf2_hmac("sha256", master, salt, _PBKDF2_ITERATIONS, dklen=32)
        nonce = secrets.token_bytes(_NONCE_LEN)
        ct = aesgcm(key).encrypt(nonce, plaintext.encode(), None)
        return b"aes:" + salt + nonce + ct
    # Fallback: Keychain de macOS
    keychain_put(key_id, plaintext)
    return b"kc:" + key_id.encode()


def decrypt_api_key(blob: bytes, key_id: str, secret_env: str | None, secret_path: Path) -> str:
    aesgcm = _aesgcm()
    if blob.startswith(b"aes:"):
        if aesgcm is None:
            raise CryptoError("blob AES pero cryptography no esta instalado")
        raw = blob[4:]
        if len(raw) < _SALT_LEN + _NONCE_LEN:
            raise CryptoError("blob cifrado invalido")
        salt, nonce, ct = raw[:_SALT_LEN], raw[_SALT_LEN:_SALT_LEN + _NONCE_LEN], raw[_SALT_LEN + _NONCE_LEN:]
        master = _master_key(secret_env, secret_path)
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
