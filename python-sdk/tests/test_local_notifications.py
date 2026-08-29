"""Behavior of the staged module's standalone `tangram.notifications`.

Desktop delivery is patched out; these tests pin the envelope shape,
channel semantics, dedupe contract, and the per-recipient record."""

from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest import mock


def load_module():
    source = Path(__file__).resolve().parents[1] / "src" / "tangram_app" / "backend_runtime_sdk.py"
    spec = importlib.util.spec_from_file_location("local_notifications_tangram", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LocalNotificationsTest(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.delivered = []
        self.patch = mock.patch.object(
            self.module, "_deliver_desktop", side_effect=lambda title, body: self.delivered.append((title, body))
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_auto_sends_one_desktop_notification_and_records_sent(self):
        with mock.patch.dict(os.environ, {"TANGRAM_APP": "notes"}):
            envelope = self.module.notifications.send(
                ["acc-1", "acc-2"], "Order shipped", "Order #7 left the warehouse", link="/orders/7"
            )
        self.assertEqual(envelope["queued"], ["acc-1", "acc-2"])
        self.assertEqual(envelope["skipped"], [])
        self.assertEqual(self.delivered, [("notes: Order shipped", "Order #7 left the warehouse\n/orders/7")])
        rows = self.module.notifications.list()
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["status"] for row in rows}, {"sent"})
        self.assertEqual({row["account_id"] for row in rows}, {"acc-1", "acc-2"})

    def test_explicit_channels_never_fall_back_and_skip_unreachable(self):
        for channel in ("email", "slack"):
            envelope = self.module.notifications.send(["acc-1"], "s", "b", channel=channel)
            self.assertEqual(envelope["queued"], [])
            self.assertEqual(envelope["skipped"], [{"id": "acc-1", "reason": "unreachable"}])
        self.assertEqual(self.delivered, [])
        self.assertEqual({row["status"] for row in self.module.notifications.list()}, {"skipped"})

    def test_dedupe_key_pins_the_exact_request(self):
        first = self.module.notifications.send(["acc-1"], "s", "b", dedupe_key="k1")
        again = self.module.notifications.send(["acc-1"], "s", "b", dedupe_key="k1")
        self.assertEqual(again["id"], first["id"])
        self.assertTrue(again["deduped"])
        self.assertEqual(len(self.delivered), 1)
        with self.assertRaises(self.module.ActionError) as caught:
            self.module.notifications.send(["acc-1"], "s", "DIFFERENT", dedupe_key="k1")
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_refuses_address_shaped_recipients_and_bad_input(self):
        for bad in (["a@example.com"], [], [""], None):
            with self.assertRaises(self.module.ActionError) as caught:
                self.module.notifications.send(bad, "s", "b")
            self.assertEqual(caught.exception.code, "invalid_request")
        with self.assertRaises(self.module.ActionError):
            self.module.notifications.send(["acc-1"], "", "b")
        with self.assertRaises(self.module.ActionError):
            self.module.notifications.send(["acc-1"], "s", "b", channel="sms")
        self.assertEqual(self.delivered, [])

    def test_delivery_failure_lands_terminal_failed_not_an_exception(self):
        self.patch.stop()
        with mock.patch.object(self.module, "_deliver_desktop", side_effect=RuntimeError("no notifier")):
            envelope = self.module.notifications.send(["acc-1"], "s", "b")
        self.patch.start()
        self.assertEqual(envelope["queued"], ["acc-1"])
        self.assertEqual(self.module.notifications.list()[0]["status"], "failed")

    def test_list_is_newest_first_and_bounded(self):
        for index in range(3):
            self.module.notifications.send(["acc-1"], f"s{index}", "b")
        rows = self.module.notifications.list(limit=2)
        self.assertEqual([row["subject"] for row in rows], ["s2", "s1"])


if __name__ == "__main__":
    unittest.main()
