from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from go_backend.billing import (
    BillingConfig,
    BillingError,
    BillingService,
    verify_webhook_signature,
)
from go_backend.store import Store


PLUS_PRICE = "price_plus_live"
PRO_PRICE = "price_pro_live"
BUSINESS_PRICE = "price_business_live"


def config(*, live_mode: bool = True) -> BillingConfig:
    return BillingConfig.from_values(
        enabled=True,
        live_mode=live_mode,
        secret_key="sk_live_example" if live_mode else "sk_test_example",
        webhook_secret="whsec_example",
        basic_price_id=PLUS_PRICE,
        pro_price_id=PRO_PRICE,
        business_price_id=BUSINESS_PRICE,
        success_url="https://agentgenia.example/billing/success",
        cancel_url="https://agentgenia.example/pricing",
        portal_return_url="https://agentgenia.example/account",
    )


def signed(event: dict, secret: str = "whsec_example") -> tuple[bytes, str]:
    payload = json.dumps(event, separators=(",", ":")).encode()
    timestamp = int(time.time())
    digest = hmac.new(secret.encode(), str(timestamp).encode() + b"." + payload, hashlib.sha256).hexdigest()
    return payload, f"t={timestamp},v1={digest}"


class FakeStripeClient:
    def __init__(self):
        self.checkout_calls: list[dict] = []
        self.portal_calls: list[dict] = []
        self.cancel_calls: list[tuple[str, str]] = []
        self.retrieve_calls: list[str] = []
        self.subscriptions: dict[str, dict] = {}

    def create_checkout_session(self, **kwargs):
        self.checkout_calls.append(kwargs)
        return {"url": "https://checkout.stripe.com/c/pay/cs_live_example"}

    def create_portal_session(self, **kwargs):
        self.portal_calls.append(kwargs)
        return {"url": "https://billing.stripe.com/p/session/example"}

    def retrieve_subscription(self, subscription_id: str):
        self.retrieve_calls.append(subscription_id)
        return self.subscriptions[subscription_id]

    def cancel_subscription(self, subscription_id: str, *, idempotency_key: str):
        self.cancel_calls.append((subscription_id, idempotency_key))
        return {"id": subscription_id, "status": "canceled"}


class TestBilling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "billing.sqlite")
        self.user = self.store.create_user("wrapper-key", "Alan", "alan@example.com")
        self.client = FakeStripeClient()
        self.service = BillingService(self.store, config(), client=self.client)

    def tearDown(self):
        self.tmp.cleanup()

    def event(
        self,
        event_id: str,
        event_type: str,
        obj: dict,
        *,
        created: int = 1_800_000_000,
    ) -> dict:
        return {
            "id": event_id,
            "type": event_type,
            "created": created,
            "livemode": True,
            "data": {"object": obj},
        }

    def process(self, event: dict) -> dict:
        payload, signature = signed(event)
        return self.service.process_webhook(payload, signature)

    def activate_plus(
        self,
        event_id: str = "evt_checkout",
        *,
        created: int = 1_800_000_000,
    ) -> dict:
        self.client.subscriptions["sub_stripe"] = {
            "id": "sub_stripe",
            "customer": "cus_live",
            "status": "active",
            "cancel_at_period_end": False,
            "current_period_end": 1_900_000_000,
            "metadata": {"user_id": self.user["id"], "tier": "basic"},
            "items": {"data": [{"price": {"id": PLUS_PRICE}}]},
        }
        return self.process(self.event(event_id, "checkout.session.completed", {
            "id": "cs_live",
            "customer": "cus_live",
            "subscription": "sub_stripe",
            "client_reference_id": self.user["id"],
            "payment_status": "paid",
            "metadata": {"user_id": self.user["id"], "tier": "basic"},
        }, created=created))

    def test_checkout_uses_server_side_price_and_metadata(self):
        response = self.service.create_checkout(self.user, "basic")
        self.assertEqual(response["checkout_url"], "https://checkout.stripe.com/c/pay/cs_live_example")
        call = self.client.checkout_calls[0]
        self.assertEqual(call["price_id"], PLUS_PRICE)
        self.assertEqual(call["tier"], "basic")
        self.assertEqual(call["user_id"], self.user["id"])
        with self.assertRaises(BillingError):
            self.service.create_checkout(self.user, "enterprise")

    def test_signed_checkout_activates_atomically_and_is_idempotent(self):
        result = self.activate_plus()
        self.assertEqual(result["tier"], "basic")
        updated = self.store.get_user_by_id(self.user["id"])
        self.assertEqual(updated["tier"], "basic")
        self.assertIsNone(updated["subscription_id"])
        billing = self.store.get_billing_status(self.user["id"])
        self.assertEqual(billing["customer_id"], "cus_live")
        self.assertEqual(billing["subscription"]["stripe_subscription_id"], "sub_stripe")
        grants = self.store._q(
            "SELECT source_key,original_milli,remaining_milli FROM credit_grants WHERE user_id=?",
            (self.user["id"],),
        )
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["source_key"], "stripe-period:sub_stripe:1900000000")
        self.assertEqual(grants[0]["original_milli"], 300_000)
        duplicate = self.activate_plus()
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(
            self.store._one("SELECT COUNT(*) AS n FROM credit_grants")["n"], 1
        )

    def test_midcycle_upgrade_grants_only_the_plan_delta(self):
        self.activate_plus(created=1_800_000_000)
        self.client.subscriptions["sub_stripe"] = {
            "id": "sub_stripe",
            "customer": "cus_live",
            "status": "active",
            "cancel_at_period_end": False,
            "current_period_end": 1_900_000_000,
            "metadata": {"user_id": self.user["id"], "tier": "pro"},
            "items": {"data": [{"price": {"id": PRO_PRICE}}]},
        }
        self.process(self.event(
            "evt_upgrade",
            "customer.subscription.updated",
            self.client.subscriptions["sub_stripe"],
            created=1_800_000_100,
        ))
        grant = self.store._one(
            "SELECT original_milli,remaining_milli FROM credit_grants WHERE source_key=?",
            ("stripe-period:sub_stripe:1900000000",),
        )
        self.assertEqual(grant["original_milli"], 1_000_000)
        self.assertEqual(grant["remaining_milli"], 1_000_000)
        ledger = self.store._q(
            "SELECT amount_milli FROM credit_ledger WHERE grant_id=(SELECT id FROM credit_grants WHERE source_key=?)",
            ("stripe-period:sub_stripe:1900000000",),
        )
        self.assertEqual(sorted(row["amount_milli"] for row in ledger), [300_000, 700_000])

    def test_past_due_keeps_access_but_terminal_status_revokes_it(self):
        self.activate_plus()
        self.process(self.event("evt_failed", "invoice.payment_failed", {
            "id": "in_live", "customer": "cus_live", "subscription": "sub_stripe"
        }))
        self.assertEqual(self.store.get_user_by_id(self.user["id"])["tier"], "basic")
        self.process(self.event("evt_deleted", "customer.subscription.deleted", {
            "id": "sub_stripe",
            "customer": "cus_live",
            "status": "canceled",
            "metadata": {"user_id": self.user["id"], "tier": "basic"},
            "items": {"data": [{"price": {"id": PLUS_PRICE}}]},
        }))
        updated = self.store.get_user_by_id(self.user["id"])
        self.assertEqual(updated["tier"], "free")
        self.assertIsNone(updated["subscription_id"])

    def test_out_of_order_paid_invoice_cannot_restore_canceled_access(self):
        self.activate_plus(created=1_800_000_100)
        self.process(self.event(
            "evt_deleted_newer",
            "customer.subscription.deleted",
            {
                "id": "sub_stripe",
                "customer": "cus_live",
                "status": "canceled",
                "metadata": {"user_id": self.user["id"], "tier": "basic"},
                "items": {"data": [{"price": {"id": PLUS_PRICE}}]},
            },
            created=1_800_000_300,
        ))
        self.client.subscriptions["sub_stripe"] = {
            "id": "sub_stripe",
            "customer": "cus_live",
            "status": "canceled",
            "cancel_at_period_end": False,
            "current_period_end": 1_800_000_250,
            "metadata": {"user_id": self.user["id"], "tier": "basic"},
            "items": {"data": [{"price": {"id": PLUS_PRICE}}]},
        }

        result = self.process(self.event(
            "evt_invoice_paid_older",
            "invoice.paid",
            {"id": "in_old", "customer": "cus_live", "subscription": "sub_stripe"},
            created=1_800_000_200,
        ))

        self.assertTrue(result["stale"])
        self.assertEqual(self.client.retrieve_calls[-1], "sub_stripe")
        self.assertEqual(self.store.get_user_by_id(self.user["id"])["tier"], "free")
        billing = self.store.get_billing_status(self.user["id"])["subscription"]
        self.assertEqual(billing["status"], "canceled")
        self.assertEqual(billing["last_stripe_event_created"], 1_800_000_300)
        recorded = self.store._one(
            "SELECT stripe_event_created FROM stripe_events WHERE event_id=?",
            ("evt_invoice_paid_older",),
        )
        self.assertEqual(recorded["stripe_event_created"], 1_800_000_200)

    def test_current_stripe_state_blocks_activation_even_for_newer_paid_invoice(self):
        self.activate_plus(created=1_800_000_100)
        self.client.subscriptions["sub_stripe"] = {
            "id": "sub_stripe",
            "customer": "cus_live",
            "status": "canceled",
            "metadata": {"user_id": self.user["id"], "tier": "basic"},
            "items": {"data": [{"price": {"id": PLUS_PRICE}}]},
        }

        self.process(self.event(
            "evt_invoice_paid_newer",
            "invoice.paid",
            {"id": "in_new", "customer": "cus_live", "subscription": "sub_stripe"},
            created=1_800_000_400,
        ))

        self.assertEqual(self.store.get_user_by_id(self.user["id"])["tier"], "free")
        billing = self.store.get_billing_status(self.user["id"])["subscription"]
        self.assertEqual(billing["status"], "canceled")
        self.assertEqual(billing["last_stripe_event_created"], 1_800_000_400)

    def test_current_subscription_with_unknown_price_cannot_activate(self):
        self.client.subscriptions["sub_stripe"] = {
            "id": "sub_stripe",
            "customer": "cus_live",
            "status": "active",
            "metadata": {"user_id": self.user["id"], "tier": "basic"},
            "items": {"data": [{"price": {"id": "price_not_configured"}}]},
        }
        event = self.event(
            "evt_unknown_price",
            "checkout.session.completed",
            {
                "id": "cs_live",
                "customer": "cus_live",
                "subscription": "sub_stripe",
                "client_reference_id": self.user["id"],
                "payment_status": "paid",
                "metadata": {"user_id": self.user["id"], "tier": "basic"},
            },
        )

        with self.assertRaises(BillingError) as caught:
            self.process(event)

        self.assertEqual(caught.exception.code, "stripe_price_not_configured")
        self.assertEqual(self.store.get_user_by_id(self.user["id"])["tier"], "free")

    def test_portal_requires_a_bound_customer(self):
        with self.assertRaises(BillingError):
            self.service.create_portal(self.user)
        self.activate_plus()
        response = self.service.create_portal(self.user)
        self.assertEqual(response["portal_url"], "https://billing.stripe.com/p/session/example")
        self.assertEqual(self.client.portal_calls[0]["customer_id"], "cus_live")

    def test_account_deletion_cancels_active_stripe_subscription(self):
        self.activate_plus()
        current_user = self.store.get_user_by_id(self.user["id"])

        self.assertTrue(self.service.cancel_for_account_deletion(current_user))
        self.assertEqual(self.client.cancel_calls[0][0], "sub_stripe")
        self.assertTrue(
            self.client.cancel_calls[0][1].startswith("agentgenia-delete-")
        )

        self.process(self.event(
            "evt_canceled",
            "customer.subscription.deleted",
            {
                "id": "sub_stripe",
                "customer": "cus_live",
                "status": "canceled",
                "metadata": {"user_id": self.user["id"], "tier": "basic"},
                "items": {"data": [{"price": {"id": PLUS_PRICE}}]},
            },
            created=1_800_000_100,
        ))
        self.assertFalse(
            self.service.cancel_for_account_deletion(
                self.store.get_user_by_id(self.user["id"])
            )
        )

    def test_account_deletion_retry_accepts_subscription_already_canceled_remotely(self):
        self.activate_plus()
        self.client.subscriptions["sub_stripe"]["status"] = "canceled"

        self.assertTrue(
            self.service.cancel_for_account_deletion(
                self.store.get_user_by_id(self.user["id"])
            )
        )
        self.assertEqual(self.client.retrieve_calls[-1], "sub_stripe")
        self.assertEqual(self.client.cancel_calls, [])

    def test_invalid_signature_and_wrong_mode_are_rejected(self):
        payload = b'{"id":"evt"}'
        with self.assertRaises(BillingError):
            verify_webhook_signature(payload, "t=1,v1=nope", "whsec_example", now=1)
        missing_created = self.event("evt_missing_created", "invoice.paid", {})
        missing_created.pop("created")
        body, signature = signed(missing_created)
        with self.assertRaises(BillingError) as caught:
            self.service.process_webhook(body, signature)
        self.assertEqual(caught.exception.code, "invalid_stripe_event")
        test_event = self.event("evt_test", "checkout.session.completed", {})
        test_event["livemode"] = False
        body, signature = signed(test_event)
        with self.assertRaises(BillingError) as caught:
            self.service.process_webhook(body, signature)
        self.assertEqual(caught.exception.code, "stripe_mode_mismatch")

    def test_live_mode_rejects_test_secret_and_duplicate_prices(self):
        with self.assertRaises(ValueError):
            BillingConfig.from_values(
                enabled=True,
                live_mode=True,
                secret_key="sk_test_wrong",
                webhook_secret="whsec_example",
                basic_price_id=PLUS_PRICE,
                pro_price_id=PRO_PRICE,
                business_price_id=BUSINESS_PRICE,
                success_url="https://example.com/success",
                cancel_url="https://example.com/cancel",
                portal_return_url="https://example.com/account",
            )
        with self.assertRaises(ValueError):
            BillingConfig.from_values(
                enabled=True,
                live_mode=True,
                secret_key="sk_live_example",
                webhook_secret="whsec_example",
                basic_price_id=PLUS_PRICE,
                pro_price_id=PLUS_PRICE,
                business_price_id=BUSINESS_PRICE,
                success_url="https://example.com/success",
                cancel_url="https://example.com/cancel",
                portal_return_url="https://example.com/account",
            )

    def test_existing_sqlite_billing_tables_gain_event_ordering_columns(self):
        path = Path(self.tmp.name) / "legacy-billing.sqlite"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            CREATE TABLE billing_subscriptions (
              stripe_subscription_id TEXT PRIMARY KEY,
              user_id TEXT NOT NULL,
              tier TEXT NOT NULL,
              stripe_price_id TEXT,
              status TEXT NOT NULL,
              cancel_at_period_end INTEGER NOT NULL DEFAULT 0,
              current_period_end INTEGER,
              updated_at REAL NOT NULL
            );
            CREATE TABLE stripe_events (
              event_id TEXT PRIMARY KEY,
              event_type TEXT NOT NULL,
              processed_at REAL NOT NULL
            );
            """
        )
        connection.close()

        migrated = Store(path)

        billing_columns = {
            row["name"] for row in migrated._q("PRAGMA table_info(billing_subscriptions)")
        }
        event_columns = {
            row["name"] for row in migrated._q("PRAGMA table_info(stripe_events)")
        }
        self.assertIn("last_stripe_event_created", billing_columns)
        self.assertIn("stripe_event_created", event_columns)


if __name__ == "__main__":
    unittest.main()
