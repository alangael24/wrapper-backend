import tempfile
import unittest
from pathlib import Path

from go_backend.crypto_utils import CryptoError, decrypt_api_key, encrypt_api_key


class EncryptionRotationTests(unittest.TestCase):
    def test_old_ciphertext_remains_readable_during_key_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.key"
            old_secret = "old-secret-with-at-least-twenty-four-characters"
            new_secret = "new-secret-with-at-least-twenty-four-characters"
            old_blob = encrypt_api_key(
                "sensitive",
                "item-1",
                old_secret,
                path,
                key_version=1,
                secret_versions={1: old_secret},
                allow_secret_file=False,
            )
            self.assertTrue(old_blob.startswith(b"aesv1:"))
            self.assertEqual(
                decrypt_api_key(
                    old_blob,
                    "item-1",
                    new_secret,
                    path,
                    key_version=1,
                    secret_versions={1: old_secret, 2: new_secret},
                    allow_secret_file=False,
                ),
                "sensitive",
            )

    def test_production_never_creates_a_local_master_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secret.key"
            with self.assertRaisesRegex(CryptoError, "WRAPPER_SECRET"):
                encrypt_api_key(
                    "sensitive",
                    "item-1",
                    None,
                    path,
                    allow_secret_file=False,
                )
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
