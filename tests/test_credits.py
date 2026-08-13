from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from go_backend.credits import billable_credit_milli
from go_backend.store import Store, hash_agent_run_token


class CreditLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "credits.sqlite")
        self.user = self.store.create_user("credits-user-key", "Credits", None)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def grant(
        self,
        amount_milli: int,
        source_key: str,
        *,
        source_type: str = "subscription",
        expires_at: float | None = None,
    ) -> dict:
        return self.store.grant_credits(
            user_id=self.user["id"],
            amount_milli=amount_milli,
            source_type=source_type,
            source_key=source_key,
            expires_at=expires_at,
        )

    def prepare_run(
        self,
        key: str,
        *,
        maximum: int,
        expires_at: float | None = None,
        concurrency: int = 1,
    ) -> dict:
        token = f"agrn_{key}"
        return self.store.create_agent_run(
            user_id=self.user["id"],
            idempotency_key=key,
            model="deepseek-v4-flash",
            browser=False,
            max_credit_milli=maximum,
            max_concurrent_runs=concurrency,
            token_hash=hash_agent_run_token(token),
            token_expires_at=expires_at or time.time() + 3600,
            enforce=True,
        )

    def test_integer_conversion_rounds_once_per_run(self) -> None:
        self.assertEqual(billable_credit_milli(llm_cost_microusd=48_370), 6_100)
        self.assertEqual(billable_credit_milli(llm_cost_microusd=80_970), 10_200)
        self.assertEqual(billable_credit_milli(llm_cost_microusd=165_520), 20_700)
        self.assertEqual(billable_credit_milli(llm_cost_microusd=0), 0)
        self.assertEqual(
            billable_credit_milli(llm_cost_microusd=0, extra_cost_microusd=1_000),
            100,
        )
        # Summing calls before conversion avoids a per-call rounding tax.
        whole_run = billable_credit_milli(llm_cost_microusd=8_000)
        per_call = sum(billable_credit_milli(llm_cost_microusd=80) for _ in range(100))
        self.assertEqual(whole_run, 1_000)
        self.assertGreater(per_call, whole_run)

    def test_grant_is_idempotent_and_upgrade_only_adds_delta(self) -> None:
        first = self.grant(300_000, "stripe-period:sub:period")
        duplicate = self.grant(300_000, "stripe-period:sub:period")
        upgraded = self.store.grant_credits(
            user_id=self.user["id"],
            amount_milli=1_000_000,
            source_type="subscription",
            source_key="stripe-period:sub:period",
            allow_increase=True,
        )
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual(first["id"], upgraded["id"])
        self.assertEqual(upgraded["remaining_milli"], 1_000_000)
        ledger = self.store._q(
            "SELECT amount_milli FROM credit_ledger WHERE user_id=? ORDER BY created_at",
            (self.user["id"],),
        )
        self.assertEqual([row["amount_milli"] for row in ledger], [300_000, 700_000])

    def test_reservation_uses_earliest_expiring_grant_and_settles_actual(self) -> None:
        now = time.time()
        later = self.grant(2_000, "topup", source_type="topup")
        sooner = self.grant(1_000, "monthly", expires_at=now + 600)
        prepared = self.prepare_run("earliest-expiry", maximum=1_500)
        run_id = prepared["run"]["id"]
        allocations = self.store._q(
            "SELECT grant_id,allocated_milli FROM credit_reservation_allocations "
            "WHERE reservation_id=(SELECT id FROM credit_reservations WHERE run_id=?)",
            (run_id,),
        )
        allocated = {row["grant_id"]: row["allocated_milli"] for row in allocations}
        self.assertEqual(allocated, {sooner["id"]: 1_000, later["id"]: 500})

        settled = self.store.settle_agent_run(
            run_id=run_id,
            charged_milli=600,
            final_status="succeeded",
            duration_seconds=2.0,
        )
        self.assertEqual(settled["charged_credit_milli"], 600)
        summary = self.store.credit_summary(self.user["id"])
        self.assertEqual(summary["available_milli"], 2_400)
        self.assertEqual(summary["reserved_milli"], 0)

    def test_retry_and_concurrency_cannot_double_reserve(self) -> None:
        self.grant(50_000, "trial", source_type="trial")
        first = self.prepare_run("same-request", maximum=25_000)
        duplicate = self.prepare_run("same-request", maximum=25_000)
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(first["run"]["id"], duplicate["run"]["id"])
        with self.assertRaisesRegex(RuntimeError, "credit_concurrency_limit"):
            self.prepare_run("second-request", maximum=25_000)
        summary = self.store.credit_summary(self.user["id"])
        self.assertEqual(summary["reserved_milli"], 25_000)

    def test_simultaneous_runs_cannot_overdraw_the_same_balance(self) -> None:
        self.grant(25_000, "single-balance", source_type="trial")
        barrier = threading.Barrier(3)
        outcomes: list[str] = []
        outcomes_lock = threading.Lock()

        def reserve(key: str) -> None:
            barrier.wait()
            try:
                self.prepare_run(key, maximum=25_000, concurrency=2)
                outcome = "reserved"
            except RuntimeError as exc:
                outcome = str(exc)
            with outcomes_lock:
                outcomes.append(outcome)

        threads = [
            threading.Thread(target=reserve, args=("parallel-a",)),
            threading.Thread(target=reserve, args=("parallel-b",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(sorted(outcomes), ["insufficient_credits", "reserved"])
        summary = self.store.credit_summary(self.user["id"])
        self.assertEqual(summary["available_milli"], 0)
        self.assertEqual(summary["reserved_milli"], 25_000)

    def test_thousands_of_integer_settlements_have_no_balance_drift(self) -> None:
        self.grant(1_000_000, "large-wallet")
        for index in range(1_000):
            prepared = self.prepare_run(
                f"integer-charge-{index}", maximum=1_000, concurrency=1
            )
            self.store.settle_agent_run(
                run_id=prepared["run"]["id"],
                charged_milli=100,
                final_status="succeeded",
                duration_seconds=0.001,
            )
        summary = self.store.credit_summary(self.user["id"], recent_limit=0)
        self.assertEqual(summary["available_milli"], 900_000)
        charged = self.store._one(
            "SELECT COALESCE(SUM(amount_milli),0) AS n FROM credit_ledger "
            "WHERE user_id=? AND entry_type='charge'",
            (self.user["id"],),
        )
        self.assertEqual(charged["n"], -100_000)

    def test_expired_reservation_releases_balance_and_revokes_token(self) -> None:
        self.grant(25_000, "trial", source_type="trial")
        prepared = self.prepare_run(
            "crashed-run",
            maximum=25_000,
            expires_at=time.time() - 1,
        )
        run_id = prepared["run"]["id"]
        self.assertEqual(self.store.expire_stale_reservations(), 1)
        self.assertEqual(self.store.get_agent_run(run_id)["status"], "expired")
        self.assertIsNone(self.store.get_agent_run_by_token("agrn_crashed-run"))
        self.assertEqual(self.store.credit_summary(self.user["id"])["available_milli"], 25_000)

    def test_charge_is_capped_and_usage_matches_run(self) -> None:
        self.grant(25_000, "trial", source_type="trial")
        prepared = self.prepare_run("bounded-run", maximum=25_000)
        run_id = prepared["run"]["id"]
        self.store.record_usage(
            self.user["id"],
            None,
            "deepseek-v4-flash",
            "/chat/completions",
            100,
            50,
            25,
            0,
            0.03,
            200,
            run_id=run_id,
            estimated_cost_microusd=30_000,
        )
        settled = self.store.settle_agent_run(
            run_id=run_id,
            charged_milli=99_999,
            final_status="budget_exhausted",
            duration_seconds=1.0,
        )
        self.assertEqual(settled["charged_credit_milli"], 25_000)
        self.assertEqual(settled["llm_cost_microusd"], 30_000)
        self.assertEqual(self.store.agent_run_cost_microusd(run_id), 30_000)
        self.assertEqual(self.store.credit_summary(self.user["id"])["available_milli"], 0)

    def test_platform_failure_refunds_all_but_cancellation_charges_actual(self) -> None:
        self.grant(50_000, "failure-wallet")
        failed = self.prepare_run("platform-failure", maximum=25_000)
        failed_run_id = failed["run"]["id"]
        self.store.record_usage(
            self.user["id"], None, "deepseek-v4-flash", "/chat/completions",
            100, 50, 0, 0, 0.01, 502,
            run_id=failed_run_id, estimated_cost_microusd=10_000,
        )
        released = self.store.release_agent_run(
            run_id=failed_run_id,
            final_status="failed",
            error_code="internal_error",
            duration_seconds=1.0,
        )
        self.assertEqual(released["charged_credit_milli"], 0)
        reservation = self.store._one(
            "SELECT status FROM credit_reservations WHERE run_id=?", (failed_run_id,)
        )
        self.assertEqual(reservation["status"], "released")
        self.assertEqual(self.store.credit_summary(self.user["id"])["available_milli"], 50_000)

        cancelled = self.prepare_run("user-cancelled", maximum=25_000)
        settled = self.store.settle_agent_run(
            run_id=cancelled["run"]["id"],
            charged_milli=3_400,
            final_status="cancelled",
            duration_seconds=2.0,
        )
        self.assertEqual(settled["charged_credit_milli"], 3_400)
        self.assertEqual(self.store.credit_summary(self.user["id"])["available_milli"], 46_600)

    def test_account_deletion_cascades_wallet_and_run_secrets(self) -> None:
        self.grant(25_000, "trial", source_type="trial")
        self.prepare_run("delete-run", maximum=25_000)
        self.store.delete_user_account(self.user["id"])
        for table in (
            "credit_ledger",
            "credit_reservation_allocations",
            "credit_reservations",
            "agent_run_tokens",
            "agent_runs",
            "credit_grants",
        ):
            count = self.store._one(f"SELECT COUNT(*) AS n FROM {table}")
            self.assertEqual(count["n"], 0, table)


if __name__ == "__main__":
    unittest.main()
