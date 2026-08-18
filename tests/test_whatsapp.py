"""Reliability and transport contract tests for the official WhatsApp channel."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
import sys

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from go_backend.connectors import CONNECTOR_CATALOG
from go_backend.store import Store
from go_backend.whatsapp import WhatsAppCloudAPI, WhatsAppConfig, WhatsAppError
from go_backend.whatsapp_agent import (
    approval_decision,
    connector_command,
    likely_connector_action,
    parse_agent_answer,
)


def config() -> WhatsAppConfig:
    return WhatsAppConfig(
        enabled=True,
        verify_token="verify-token-for-agentgenia-tests",
        app_secret="app-secret-for-agentgenia-tests",
        access_token="access-token-for-agentgenia-tests",
        phone_number_id="123456789012345",
        public_number="15551234567",
        graph_version="v25.0",
    )


class WhatsAppTransportTests(unittest.TestCase):
    def test_cloud_api_chunks_unicode_and_preserves_reply_context_once(self):
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={"messages": [{"id": f"wamid.out.{len(requests)}"}]},
            )

        api = WhatsAppCloudAPI(
            config(),
            api_base_url="https://meta.test",
            client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        outbound_id = api.send_text(
            to="15557654321",
            text=("línea con emoji 🚀 y acentos\n" * 300),
            reply_to_message_id="wamid.inbound",
        )

        self.assertGreater(len(requests), 1)
        self.assertEqual(outbound_id, f"wamid.out.{len(requests)}")
        self.assertEqual(requests[0].url.path, "/v25.0/123456789012345/messages")
        self.assertEqual(
            requests[0].headers["authorization"],
            "Bearer access-token-for-agentgenia-tests",
        )
        payloads = [json.loads(item.content) for item in requests]
        self.assertEqual(payloads[0]["context"], {"message_id": "wamid.inbound"})
        self.assertTrue(all("context" not in item for item in payloads[1:]))
        self.assertTrue(all(len(item["text"]["body"]) <= 3500 for item in payloads))

    def test_explicit_rate_limit_is_retryable_and_honors_retry_after(self):
        api = WhatsAppCloudAPI(
            config(),
            client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda _request: httpx.Response(
                        429,
                        headers={"Retry-After": "3"},
                        json={"error": {"code": 4, "message": "rate limit"}},
                    )
                )
            ),
        )
        with self.assertRaises(WhatsAppError) as raised:
            api.send_text(to="15557654321", text="hola")
        self.assertTrue(raised.exception.retryable)
        self.assertFalse(raised.exception.delivery_uncertain)
        self.assertEqual(raised.exception.retry_after_seconds, 3.0)

    def test_timeout_and_partial_multi_chunk_delivery_are_never_retried(self):
        def timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("socket closed", request=request)

        api = WhatsAppCloudAPI(
            config(),
            client=httpx.Client(transport=httpx.MockTransport(timeout)),
        )
        with self.assertRaises(WhatsAppError) as raised:
            api.send_text(to="15557654321", text="hola")
        self.assertFalse(raised.exception.retryable)
        self.assertTrue(raised.exception.delivery_uncertain)

        calls = 0

        def partial(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, json={"messages": [{"id": "wamid.first"}]})
            return httpx.Response(429, json={"error": {"code": 4}})

        api = WhatsAppCloudAPI(
            config(),
            client=httpx.Client(transport=httpx.MockTransport(partial)),
        )
        with self.assertRaises(WhatsAppError) as raised:
            api.send_text(to="15557654321", text="x " * 4000)
        self.assertTrue(raised.exception.delivery_uncertain)
        self.assertFalse(raised.exception.retryable)

    def test_every_catalog_connector_is_addressable_from_whatsapp(self):
        for connector_id, item in CONNECTOR_CATALOG.items():
            with self.subTest(connector_id=connector_id):
                self.assertEqual(
                    connector_command(f"Conecta {item['name']}"),
                    ("connect", connector_id),
                )
                self.assertEqual(
                    connector_command(f"Desconecta {item['name']}"),
                    ("disconnect", connector_id),
                )

    def test_text_approval_is_explicit_and_keeps_the_provider_summary(self):
        self.assertEqual(approval_decision("AUTORIZAR"), "approve")
        self.assertEqual(approval_decision("Sí, autoriza"), "approve")
        self.assertEqual(approval_decision("CANCELAR"), "reject")
        self.assertIsNone(approval_decision("sí"))
        self.assertIsNone(approval_decision("autoriza correos siempre"))
        self.assertTrue(likely_connector_action("Haz una presentación"))
        answer = parse_agent_answer(json.dumps({
            "text": "Confirma esta acción antes de que la ejecute.",
            "widget": {
                "type": "approval",
                "prompt": "Enviar un correo a ana@example.com con asunto «Hola»",
            },
        }))
        self.assertIn("ana@example.com", answer)
        self.assertIn("AUTORIZAR", answer)
        self.assertIn("CANCELAR", answer)


class WhatsAppQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="whatsapp-queue-")
        self.store = Store(Path(self.tmp.name) / "store.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def enqueue(self, message_id: str, sender: str) -> None:
        self.assertTrue(self.store.enqueue_whatsapp_message(
            message_id=message_id,
            phone_number_id="123456789012345",
            wa_user_id=sender,
            message_type="text",
            text=message_id,
            payload={"id": message_id, "type": "text"},
        ))
        # Make ordering deterministic even on a very fast filesystem clock.
        time.sleep(0.002)

    def test_same_chat_is_serial_but_different_chats_run_in_parallel(self):
        self.enqueue("wamid.a1", "sender-a")
        self.enqueue("wamid.a2", "sender-a")
        self.enqueue("wamid.b1", "sender-b")

        first = self.store.claim_whatsapp_message()
        second = self.store.claim_whatsapp_message()
        self.assertEqual(first["message_id"], "wamid.a1")
        self.assertEqual(second["message_id"], "wamid.b1")
        self.assertIsNone(self.store.claim_whatsapp_message())

        self.store.complete_whatsapp_message(
            message_id="wamid.a1", status="ignored"
        )
        third = self.store.claim_whatsapp_message()
        self.assertEqual(third["message_id"], "wamid.a2")

    def test_known_rate_limit_retries_but_uncertain_delivery_fails_closed(self):
        self.enqueue("wamid.retry", "sender-a")
        self.store.claim_whatsapp_message()
        self.store.prepare_whatsapp_outbound(
            message_id="wamid.retry", result_text="respuesta"
        )
        delay = self.store.retry_whatsapp_message(
            message_id="wamid.retry",
            error="HTTP 429",
            retryable=True,
            delivery_uncertain=False,
            retry_after_seconds=2,
        )
        self.assertEqual(delay, 2.0)
        row = self.store._one(
            "SELECT status FROM whatsapp_messages WHERE message_id=?",
            ("wamid.retry",),
        )
        self.assertEqual(row["status"], "pending")

        self.enqueue("wamid.uncertain", "sender-b")
        self.store.claim_whatsapp_message()
        self.store.prepare_whatsapp_outbound(
            message_id="wamid.uncertain", result_text="respuesta"
        )
        delay = self.store.retry_whatsapp_message(
            message_id="wamid.uncertain",
            error="read timeout",
            retryable=False,
            delivery_uncertain=True,
        )
        self.assertIsNone(delay)
        row = self.store._one(
            "SELECT status,last_error FROM whatsapp_messages WHERE message_id=?",
            ("wamid.uncertain",),
        )
        self.assertEqual(row["status"], "failed")
        self.assertIn("outbound_delivery_uncertain", row["last_error"])


if __name__ == "__main__":
    unittest.main()
