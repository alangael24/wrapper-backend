import unittest

from go_backend.postgres_store import _postgres_sql, normalize_database_url


class PostgresStoreConfigurationTests(unittest.TestCase):
    def test_remote_database_requires_tls(self):
        normalized = normalize_database_url(
            "postgresql://agent:secret@db.example.com:5432/postgres"
        )
        self.assertIn("sslmode=require", normalized)

    def test_rejects_weak_remote_tls_and_non_postgres_schemes(self):
        with self.assertRaises(ValueError):
            normalize_database_url(
                "postgresql://agent:secret@db.example.com/postgres?sslmode=disable"
            )
        with self.assertRaises(ValueError):
            normalize_database_url("https://db.example.com/postgres")

    def test_loopback_postgres_can_run_without_tls(self):
        self.assertEqual(
            normalize_database_url("postgresql://localhost/postgres"),
            "postgresql://localhost/postgres",
        )

    def test_translates_store_placeholders_for_psycopg(self):
        self.assertEqual(
            _postgres_sql("SELECT * FROM users WHERE id=? AND tier=?"),
            "SELECT * FROM users WHERE id=%s AND tier=%s",
        )


if __name__ == "__main__":
    unittest.main()
