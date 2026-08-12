from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from go_backend.computers import ComputerConfig, ComputerError, ComputerManager
from go_backend.connectors import ConnectorBroker, ConnectorBrokerError
from go_backend.store import Store


class FakeComputerProvider:
    name = "fake"

    def __init__(self):
        self.created: list[tuple[str, str, str]] = []
        self.states: dict[str, str] = {}
        self.operations: list[tuple[str, str, dict]] = []

    def create(self, *, user_id: str, bot_id: str, bot_name: str, include_viewer: bool = False):
        provider_ref = f"box-{len(self.created) + 1}"
        self.created.append((user_id, bot_id, bot_name))
        self.states[provider_ref] = "running"
        return self._snapshot(provider_ref, include_viewer)

    def inspect(self, provider_ref: str, *, include_viewer: bool = False):
        return self._snapshot(provider_ref, include_viewer)

    def ensure(self, provider_ref: str, *, include_viewer: bool = False):
        self.states[provider_ref] = "running"
        return self._snapshot(provider_ref, include_viewer)

    def stop(self, provider_ref: str):
        self.states[provider_ref] = "hibernated"
        return self._snapshot(provider_ref, False)

    def delete(self, provider_ref: str):
        self.states.pop(provider_ref, None)

    def delete_identity(self, *, user_id: str, bot_id: str):
        return None

    def execute(self, provider_ref: str, operation: str, arguments: dict):
        self.operations.append((provider_ref, operation, arguments))
        if operation == "screenshot":
            return {"image_base64": "aW1hZ2U=", "mime_type": "image/jpeg", "size_bytes": 5}
        return {"ok": True, "operation": operation}

    def _snapshot(self, provider_ref: str, include_viewer: bool):
        return {
            "provider_ref": provider_ref,
            "provider_state": self.states[provider_ref],
            "state": self.states[provider_ref],
            "viewer_url": f"https://viewer.invalid/{provider_ref}?token=secret" if include_viewer else "",
            "viewer_expires_at": 12345 if include_viewer else 0,
        }


class TestComputerManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wrapper-computers-")
        self.store = Store(Path(self.tmp.name) / "computers.sqlite")
        self.user_a = self.store.create_user("sk-user-a", "A", None)["id"]
        self.user_b = self.store.create_user("sk-user-b", "B", None)["id"]
        self.provider = FakeComputerProvider()
        self.manager = ComputerManager(
            store=self.store,
            config=ComputerConfig(enabled=False),
            provider=self.provider,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_computer_is_owned_by_user_and_bot_and_viewer_is_not_persisted(self):
        first = self.manager.ensure(user_id=self.user_a, bot_id="bot-1", bot_name="Research")
        self.assertEqual(first["state"], "running")
        self.assertIn("token=secret", first["viewer_url"])

        stored = self.store.get_bot_computer(self.user_a, "bot-1")
        self.assertEqual(stored["provider_ref"], "box-1")
        self.assertNotIn("viewer", stored)
        self.assertEqual(self.manager.status(user_id=self.user_b, bot_id="bot-1")["state"], "off")

        second = self.manager.ensure(user_id=self.user_a, bot_id="bot-1", bot_name="Renamed")
        self.assertEqual(second["provider"], "fake")
        self.assertEqual(len(self.provider.created), 1)

    def test_concurrent_ensure_creates_only_one_remote_computer(self):
        results: list[dict] = []
        errors: list[Exception] = []

        def ensure():
            try:
                results.append(self.manager.ensure(user_id=self.user_a, bot_id="bot-race", bot_name="Bot"))
            except Exception as exc:  # pragma: no cover - assertion reports the actual error
                errors.append(exc)

        threads = [threading.Thread(target=ensure) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(len(results), 8)
        self.assertEqual(len(self.provider.created), 1)
        self.assertEqual({item["state"] for item in results}, {"running"})

    def test_hibernate_execute_and_delete_lifecycle(self):
        self.manager.ensure(user_id=self.user_a, bot_id="bot-1", bot_name="Bot")
        stopped = self.manager.hand_back(user_id=self.user_a, bot_id="bot-1")
        self.assertEqual(stopped["state"], "hibernated")

        executed = self.manager.execute(
            user_id=self.user_a, bot_id="bot-1", operation="click", arguments={"x": 10, "y": 20}
        )
        self.assertTrue(executed["result"]["ok"])
        self.assertEqual(self.provider.operations[-1][1], "click")
        self.assertTrue(self.manager.delete(user_id=self.user_a, bot_id="bot-1")["deleted"])
        self.assertIsNone(self.store.get_bot_computer(self.user_a, "bot-1"))

    def test_invalid_arguments_are_rejected_before_provider(self):
        with self.assertRaises(ComputerError) as raised:
            self.manager.execute(
                user_id=self.user_a, bot_id="bot-1", operation="not-allowed", arguments={}
            )
        self.assertEqual(raised.exception.status, 400)

    def test_plan_limit_is_claimed_atomically(self):
        self.manager.ensure(user_id=self.user_a, bot_id="bot-1", bot_name="One")
        with self.assertRaises(ComputerError) as raised:
            self.manager.ensure(user_id=self.user_a, bot_id="bot-2", bot_name="Two")
        self.assertEqual(raised.exception.code, "computer_limit_reached")
        self.assertEqual(len(self.provider.created), 1)


class TestComputerGrant(unittest.TestCase):
    def test_grant_scopes_a_single_bot(self):
        broker = ConnectorBroker(default_ttl_seconds=30)
        token = broker.issue(user_id="user-a", connector_ids=(), computer_id="bot-1")
        self.assertEqual(broker.computer(token), ("user-a", "bot-1"))
        broker.revoke(token)
        with self.assertRaises(ConnectorBrokerError):
            broker.computer(token)


if __name__ == "__main__":
    unittest.main()
