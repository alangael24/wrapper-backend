from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from go_backend.apple_auth import AppleAccountAuth, AppleAuthError
from go_backend.store import Store


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class SessionIssuer:
    def issue_session(self, *, account: dict, device_id: str) -> dict:
        return {
            "token": "aga_test",
            "refresh_token": "agr_test",
            "expires_at": int((time.time() + 900) * 1000),
            "account": {
                "id": account["id"],
                "email": account["email"],
                "name": account.get("name") or "",
                "picture": account.get("picture") or "",
            },
        }


class AppleAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "apple.sqlite")
        apple_key = ec.generate_private_key(ec.SECP256R1())
        apple_pem = apple_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        self.auth = AppleAccountAuth(
            store=self.store,
            session_issuer=SessionIssuer(),
            client_id="com.agentgenia.ios",
            team_id="TEAM123456",
            key_id="KEY1234567",
            private_key_base64=base64.b64encode(apple_pem).decode(),
            secret_env="s" * 64,
            secret_path=Path(self.temp.name) / "secret.key",
            key_version=1,
            secret_versions={1: "s" * 64},
            allow_secret_file=False,
        )
        self.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.signing_key.public_key().public_numbers()
        self.auth._jwks = {
            "apple-test": {
                "kid": "apple-test",
                "kty": "RSA",
                "n": b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            }
        }
        self.auth._jwks_expires_at = time.monotonic() + 3600

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def token(self, nonce: str, *, subject: str = "apple-user-1") -> str:
        header = b64url(json.dumps({"alg": "RS256", "kid": "apple-test"}).encode())
        claims = b64url(
            json.dumps(
                {
                    "iss": "https://appleid.apple.com",
                    "aud": "com.agentgenia.ios",
                    "sub": subject,
                    "email": "alan@example.com",
                    "email_verified": True,
                    "nonce": hashlib.sha256(nonce.encode()).hexdigest(),
                    "iat": int(time.time()),
                    "exp": int(time.time()) + 300,
                }
            ).encode()
        )
        signing_input = f"{header}.{claims}".encode()
        signature = self.signing_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}.{claims}.{b64url(signature)}"

    def test_native_apple_login_is_one_time_and_persists_revocation_credential(self):
        nonce = "secure-nonce-value-123456789"
        identity_token = self.token(nonce)
        with patch.object(self.auth, "_exchange_code", return_value="refresh-token-from-apple-123"):
            result = self.auth.login(
                identity_token=identity_token,
                authorization_code="authorization-code",
                nonce=nonce,
                device_id=str(uuid.uuid4()),
                name="Alan",
            )
            self.assertEqual(result["account"]["email"], "alan@example.com")
            account = self.store.get_account_identity(result["account"]["id"])
            credential = self.store.get_account_provider_credential(account["user_id"], "apple")
            self.assertIsNotNone(credential)
            with self.assertRaises(AppleAuthError) as error:
                self.auth.login(
                    identity_token=identity_token,
                    authorization_code="authorization-code-2",
                    nonce=nonce,
                    device_id=str(uuid.uuid4()),
                    name=None,
                )
        self.assertEqual(error.exception.code, "apple_token_reused")

    def test_rejects_nonce_mismatch(self):
        with self.assertRaises(AppleAuthError) as error:
            self.auth._verify_identity_token(self.token("correct-nonce-123456"), expected_nonce="wrong")
        self.assertEqual(error.exception.code, "invalid_nonce")

    def test_client_secret_is_es256_jwt_for_apple(self):
        token = self.auth._client_secret()
        header, claims, signature = token.split(".")
        decoded_header = json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))
        decoded_claims = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
        self.assertEqual(decoded_header["alg"], "ES256")
        self.assertEqual(decoded_claims["iss"], "TEAM123456")
        self.assertEqual(decoded_claims["sub"], "com.agentgenia.ios")
        self.assertEqual(len(base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))), 64)

    def test_native_login_is_rate_limited_before_provider_exchange(self):
        nonce = "secure-nonce-value-123456789"
        with patch.object(self.auth, "_exchange_code", return_value="refresh-token-from-apple-123"):
            for attempt in range(5):
                self.auth.login(
                    identity_token=self.token(nonce, subject=f"apple-user-{attempt}"),
                    authorization_code=f"authorization-code-{attempt}",
                    nonce=nonce,
                    device_id="f51dd5d8-d02e-4e22-a8da-14b8d1878821",
                    name="Alan",
                    remote_key="203.0.113.7",
                )
            with self.assertRaises(AppleAuthError) as error:
                self.auth.login(
                    identity_token=self.token(nonce),
                    authorization_code="authorization-code-final",
                    nonce=nonce,
                    device_id="f51dd5d8-d02e-4e22-a8da-14b8d1878821",
                    name="Alan",
                    remote_key="203.0.113.7",
                )
        self.assertEqual(error.exception.status, 429)
        self.assertEqual(error.exception.code, "rate_limit")


if __name__ == "__main__":
    unittest.main()
