import unittest

from go_backend.postgres_store import PostgresStore, _postgres_sql, normalize_database_url


class PostgresStoreConfigurationTests(unittest.TestCase):
    def test_close_is_safe_after_partial_initialization(self):
        store = PostgresStore.__new__(PostgresStore)
        store.close()

    def test_remote_database_requires_tls(self):
        normalized = normalize_database_url(
            "postgresql://agent:secret@db.example.com:5432/postgres"
        )
        self.assertIn("sslmode=verify-full", normalized)

    def test_rejects_weak_remote_tls_and_non_postgres_schemes(self):
        with self.assertRaises(ValueError):
            normalize_database_url(
                "postgresql://agent:secret@db.example.com/postgres?sslmode=disable"
            )
        with self.assertRaises(ValueError):
            normalize_database_url(
                "postgresql://agent:secret@db.example.com/postgres?sslmode=require"
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

    def test_unmetered_run_is_created_atomically_and_already_running(self):
        store = PostgresStore.__new__(PostgresStore)
        captured = {}

        def one(sql, params=()):
            captured["sql"] = sql
            captured["params"] = params
            return {"outcome": "inserted", "run": {"id": "run_test", "status": "running"}}

        store._one = one
        prepared = store.create_unmetered_agent_run(
            user_id="usr_test",
            idempotency_key="request-test",
            model="deepseek-v4-flash",
            browser=False,
            max_credit_milli=15_000,
            max_concurrent_runs=4,
            token_hash="token-hash",
            token_expires_at=1234.0,
        )

        self.assertFalse(prepared["duplicate"])
        self.assertEqual(prepared["run"]["status"], "running")
        self.assertIn("'running'", captured["sql"])
        self.assertIn("pg_advisory_xact_lock", captured["sql"])
        self.assertEqual(captured["sql"].count("?"), len(captured["params"]))

    def test_unmetered_run_is_settled_in_one_statement(self):
        store = PostgresStore.__new__(PostgresStore)
        captured = {}

        def one(sql, params=()):
            captured["sql"] = sql
            captured["params"] = params
            return {"run": {"id": "run_test", "status": "succeeded"}}

        store._one = one
        settled = store.settle_unmetered_agent_run(
            run_id="run_test",
            final_status="succeeded",
            duration_seconds=1.25,
            warnings=["timing:{}"],
        )

        self.assertEqual(settled["status"], "succeeded")
        self.assertIn("UPDATE agent_run_tokens", captured["sql"])
        self.assertIn("UPDATE credit_reservations", captured["sql"])
        self.assertIn("UPDATE agent_runs", captured["sql"])
        self.assertEqual(captured["sql"].count("?"), len(captured["params"]))


if __name__ == "__main__":
    unittest.main()
