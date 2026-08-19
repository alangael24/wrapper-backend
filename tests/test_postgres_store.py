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

    def test_metered_run_uses_one_server_side_reservation_call(self):
        store = PostgresStore.__new__(PostgresStore)
        captured = {}

        def one(sql, params=()):
            captured["sql"] = sql
            captured["params"] = params
            return {
                "outcome": "inserted",
                "run": {"id": "run_metered", "status": "reserved"},
                "error_code": None,
            }

        store._one = one
        prepared = store.create_agent_run(
            user_id="usr_test",
            idempotency_key="request-metered",
            model="deepseek-v4-flash",
            browser=False,
            max_credit_milli=1000,
            max_concurrent_runs=2,
            token_hash="token-hash",
            token_expires_at=1234.0,
            enforce=True,
            five_hour_credit_milli=200_000,
            seven_day_credit_milli=500_000,
        )

        self.assertFalse(prepared["duplicate"])
        self.assertEqual(prepared["run"]["status"], "reserved")
        self.assertEqual(
            captured["sql"],
            "SELECT * FROM reserve_agent_run(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        )
        self.assertEqual(captured["sql"].count("?"), len(captured["params"]))

    def test_whatsapp_claim_query_serializes_each_chat(self):
        store = PostgresStore.__new__(PostgresStore)

        class Connection:
            def __init__(self):
                self.sql = ""
                self.params = ()

            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return self

            def fetchone(self):
                return None

            def commit(self):
                pass

            def rollback(self):
                pass

        connection = Connection()
        store._conn = connection
        self.assertIsNone(store.claim_whatsapp_message())
        self.assertIn("FOR UPDATE SKIP LOCKED", connection.sql)
        self.assertIn("earlier.wa_user_id=m.wa_user_id", connection.sql)
        self.assertIn("earlier.status IN ('pending','processing','sending')", connection.sql)

    def test_whatsapp_claim_embeds_account_context_in_same_call(self):
        store = PostgresStore.__new__(PostgresStore)

        class Connection:
            def execute(self, sql, params=()):
                self.sql = sql
                self.params = params
                return self

            def fetchone(self):
                return {
                    "message_id": "wamid.test",
                    "wa_user_id": "15557654321",
                    "phone_number_id": "123456789012345",
                    "context_link_json": {"user_id": "usr_test"},
                    "context_user_json": {
                        "id": "usr_test",
                        "tier": "free",
                        "model_provider_override": "opencode",
                    },
                    "context_provider_subscription_id": "sub_test",
                    "context_provider_api_key_enc": b"encrypted",
                    "context_provider_key_id": "key_test",
                    "context_provider_key_version": 2,
                    "context_provider_subscription_status": "assigned",
                    "context_provider_assigned_user_id": "usr_test",
                    "context_state_user_id": "usr_test",
                    "context_state_revision": 9,
                    "context_state_json": '{"bots":[]}',
                    "context_state_created_at": 10.0,
                    "context_state_updated_at": 20.0,
                }

            def commit(self):
                pass

            def rollback(self):
                pass

        connection = Connection()
        store._conn = connection
        message = store.claim_whatsapp_message()

        self.assertEqual(message["_processing_context"]["user"]["provider_subscription_id"], "sub_test")
        self.assertEqual(message["_processing_context"]["user"]["provider_api_key_enc"], b"encrypted")
        self.assertEqual(message["_processing_context"]["account_state"]["revision"], 9)
        self.assertIn("LEFT JOIN go_subscriptions", connection.sql)
        self.assertIn("LEFT JOIN account_states", connection.sql)

    def test_whatsapp_processing_context_uses_one_joined_database_call(self):
        store = PostgresStore.__new__(PostgresStore)
        captured = {}

        def one(sql, params=()):
            captured["sql"] = sql
            captured["params"] = params
            return {
                "link_json": {
                    "wa_user_id": "15557654321",
                    "phone_number_id": "123456789012345",
                    "user_id": "usr_test",
                },
                "user_json": {"id": "usr_test", "tier": "pro"},
                "provider_subscription_id": "sub_test",
                "provider_api_key_enc": b"encrypted",
                "provider_key_id": "key_test",
                "provider_key_version": 2,
                "provider_subscription_status": "assigned",
                "provider_assigned_user_id": "usr_test",
                "state_user_id": "usr_test",
                "state_revision": 7,
                "state_json": '{"bots":[]}',
                "state_created_at": 10.0,
                "state_updated_at": 20.0,
            }

        store._one = one
        context = store.get_whatsapp_processing_context(
            wa_user_id="15557654321", phone_number_id="123456789012345"
        )

        self.assertEqual(context["link"]["user_id"], "usr_test")
        self.assertEqual(context["user"]["tier"], "pro")
        self.assertEqual(context["user"]["provider_subscription_id"], "sub_test")
        self.assertEqual(context["user"]["provider_api_key_enc"], b"encrypted")
        self.assertEqual(context["account_state"]["revision"], 7)
        self.assertIn("to_jsonb(l)", captured["sql"])
        self.assertIn("to_jsonb(u)", captured["sql"])
        self.assertIn("LEFT JOIN account_states", captured["sql"])
        self.assertIn("LEFT JOIN go_subscriptions", captured["sql"])
        self.assertEqual(captured["params"], ("15557654321", "123456789012345"))

    def test_whatsapp_history_append_is_one_atomic_database_call(self):
        store = PostgresStore.__new__(PostgresStore)
        captured = {}

        def one(sql, params=()):
            captured["sql"] = sql
            captured["params"] = params
            return {
                "user_id": "usr_test",
                "revision": 8,
                "state_json": '{"bots":[]}',
                "created_at": 10.0,
                "updated_at": 20.0,
            }

        store._one = one
        saved = store.append_account_state_messages(
            user_id="usr_test",
            bot_id="bot_test",
            messages=[
                {"id": "message_user", "role": "user", "text": "hola"},
                {"id": "message_agent", "role": "assistant", "text": "listo"},
            ],
            base_revision=7,
            device_hash="device-hash",
        )

        self.assertEqual(saved["revision"], 8)
        self.assertIn("UPDATE account_states", captured["sql"])
        self.assertIn("WITH ORDINALITY", captured["sql"])
        self.assertIn("LIMIT 200", captured["sql"])
        self.assertIn("existing.value->>'id'=incoming.value->>'id'", captured["sql"])
        self.assertEqual(captured["sql"].count("?"), len(captured["params"]))

    def test_whatsapp_history_and_delivery_claim_share_one_database_call(self):
        store = PostgresStore.__new__(PostgresStore)
        captured = {}

        def one(sql, params=()):
            captured["sql"] = sql
            captured["params"] = params
            return {
                "user_id": "usr_test",
                "revision": 8,
                "state_json": '{"bots":[]}',
                "created_at": 10.0,
                "updated_at": 20.0,
                "delivery_prepared": True,
            }

        store._one = one
        saved = store.append_account_state_messages(
            user_id="usr_test",
            bot_id="bot_test",
            messages=[{"id": "message_agent", "role": "assistant", "text": "listo"}],
            base_revision=7,
            device_hash="device-hash",
            delivery_message_id="wamid.test",
            delivery_result_text="listo",
        )

        self.assertTrue(saved["delivery_prepared"])
        self.assertIn("WITH updated_state AS", captured["sql"])
        self.assertIn("claimed_delivery AS", captured["sql"])
        self.assertIn("status='sending'", captured["sql"])
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
            result={"run_id": "run_test", "status": "succeeded", "answer": "listo"},
        )

        self.assertEqual(settled["status"], "succeeded")
        self.assertIn("UPDATE agent_run_tokens", captured["sql"])
        self.assertIn("UPDATE credit_reservations", captured["sql"])
        self.assertIn("UPDATE agent_runs", captured["sql"])
        self.assertIn("result_json=COALESCE", captured["sql"])
        self.assertEqual(captured["sql"].count("?"), len(captured["params"]))

    def test_agent_auth_reads_only_bot_connectors_in_the_same_statement(self):
        store = PostgresStore.__new__(PostgresStore)
        captured = {}

        def one(sql, params=()):
            captured["sql"] = sql
            captured["params"] = params
            return {
                "id": "usr_test",
                "account_status": "active",
                "assigned_connector_ids_json": '["google-workspace"]',
            }

        store._one = one
        user = store.get_agent_user_by_access_token("aga_test-token", "bot_test")

        self.assertEqual(user["assigned_connector_ids_json"], '["google-workspace"]')
        self.assertIn("jsonb_array_elements", captured["sql"])
        self.assertIn("account_sessions", captured["sql"])
        self.assertNotIn("SELECT ast.state_json", captured["sql"])
        self.assertEqual(captured["sql"].count("?"), len(captured["params"]))


if __name__ == "__main__":
    unittest.main()
