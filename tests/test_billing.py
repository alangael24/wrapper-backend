from __future__ import annotations

import hashlib
import hmac
import json
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


def config(*, live_mode: bool = True) -> BillingConfig:
    return BillingConfig.from_values(
        enabled=True,
        live_mode=live_mode,
        secret_key="sk_live_example" if live_mode else "sk_test_example",
        webhook_secret="whsec_example",
        basic_price_id=PLUS_PRICE,
        pro_price_id=PRO_PRICE,
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

    def create_checkout_session(self, **kwargs):
        self.checkout_calls.append(kwargs)
        return {"url": "https://checkout.stripe.com/c/pay/cs_live_example"}

    def create_portal_session(self, **kwargs):
        self.portal_calls.append(kwargs)
        return {"url": "https://billing.stripe.com/p/session/example"}


class TestBilling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "billing.sqlite")
        self.user = self.store.create_user("wrapper-key", "Alan", "alan@example.com")
        self.store.add_subscription(b"encrypted", "go-key", "pool", sub_id="sub_pool")
        self.client = FakeStripeClient()
        self.service = BillingService(self.store, config(), client=self.client)

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, event_id: str, event_type: str, obj: dict) -> dict:
        return {
            "id": event_id,
            "type": event_type,
            "livemode": True,
            "data": {"object": obj},
        }

    def process(self, event: dict) -> dict:
        payload, signature = signed(event)
        return self.service.process_webhook(payload, signature)

    def activate_plus(self, event_id: str = "evt_checkout") -> dict:
        return self.process(self.event(event_id, "checkout.session.completed", {
            "id": "cs_live",
            "customer": "cus_live",
            "subscription": "sub_stripe",
            "client_reference_id": self.user["id"],
            "payment_status": "paid",
            "metadata": {"user_id": self.user["id"], "tier": "basic"},
        }))

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
        self.assertEqual(updated["subscription_id"], "sub_pool")
        self.assertEqual(self.store.get_subscription("sub_pool")["assigned_user_id"], self.user["id"])
        billing = self.store.get_billing_status(self.user["id"])
        self.assertEqual(billing["customer_id"], "cus_live")
        self.assertEqual(billing["subscription"]["stripe_subscription_id"], "sub_stripe")
        duplicate = self.activate_plus()
        self.assertTrue(duplicate["duplicate"])

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
        self.assertEqual(self.store.get_subscription("sub_pool")["status"], "available")

    def test_portal_requires_a_bound_customer(self):
        with self.assertRaises(BillingError):
            self.service.create_portal(self.user)
        self.activate_plus()
        response = self.service.create_portal(self.user)
        self.assertEqual(response["portal_url"], "https://billing.stripe.com/p/session/example")
        self.assertEqual(self.client.portal_calls[0]["customer_id"], "cus_live")

    def test_invalid_signature_and_wrong_mode_are_rejected(self):
        payload = b'{"id":"evt"}'
        with self.assertRaises(BillingError):
            verify_webhook_signature(payload, "t=1,v1=nope", "whsec_example", now=1)
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
                success_url="https://example.com/success",
                cancel_url="https://example.com/cancel",
                portal_return_url="https://example.com/account",
            )


if __name__ == "__main__":
    unittest.main()
