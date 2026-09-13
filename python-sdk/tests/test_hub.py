from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from tangram_app import cli, hub_journal
from tangram_app.hub_target import HubError, load
from tangram_app import TangramApp, CapabilityGraph
from test_sdk import graph_dict


def run(*args):
    out = StringIO()
    with redirect_stdout(out):
        status = cli.main((["context", "resolve", *args[1:]] if args[0] == "resolve" else
                                   ["knowledge", args[0].removeprefix("knowledge-"), *args[1:]] if args[0].startswith("knowledge-") else
                                   ["app", *args]))
    return status, json.loads(out.getvalue())


class HubTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name)
        env = patch.dict(os.environ, {"TANGRAM_HOME": temp.name, "TANGRAM_INSTANCE": "local", "TANGRAM_WORKSPACE": ""})
        env.start()
        self.addCleanup(env.stop)
        self.pin = run("resolve", "--local")[1]["data"]

    def test_live_install_discovery_without_skill_sync(self):
        ref = self.pin["targetRef"]
        self.assertEqual(run("search", "reimbursements", "--target-ref", ref)[1]["data"]["candidates"], [])
        folder = self.home / "apps" / "example__expenses"
        folder.mkdir(parents=True)
        candidate = {"action": "example/expenses#Claim.List@pending", "description": "List pending reimbursements",
                     "contract": "sha256:abc", "effect": "Stateless", "requiresConfirmation": False}
        (folder / ".install.json").write_text(json.dumps({"id": "example/expenses", "capabilities": [candidate]}))
        data = run("search", "show my pending reimbursements", "--target-ref", ref)[1]["data"]
        self.assertEqual(data["candidates"][0]["action"], candidate["action"])
        self.assertEqual(data["target"], self.pin["target"])

    def test_pin_ignores_ambient_changes_but_detects_credential_change(self):
        (self.home / ".credentials").write_text(json.dumps([{"instance": "dev", "url": "localhost", "token": "one"}]))
        remote = run("resolve", "--workspace", "dev/demo")[1]["data"]
        with patch.dict(os.environ, {"TANGRAM_INSTANCE": "different", "TANGRAM_WORKSPACE": "prod"}):
            self.assertEqual(load(remote["targetRef"])[0]["workspace"], "demo")
            self.assertEqual(load(self.pin["targetRef"])[0]["lane"], "local")
        (self.home / ".credentials").write_text(json.dumps([{"instance": "dev", "url": "localhost", "token": "two"}]))
        with self.assertRaises(HubError) as caught:
            load(remote["targetRef"])
        self.assertEqual(caught.exception.code, "target_changed")

    def test_selectors_and_invalid_refs_fail_closed(self):
        for args in [("resolve", "--local", "--workspace", "dev/demo"),
                     ("resolve", "--workspace", ""),
                     ("search", "x", "--target-ref", "garbage")]:
            self.assertNotEqual(run(*args)[0], 0)

    def request(self):
        return {"target": self.pin["target"], "action": "example/app#Item.Create", "contract": "v1", "input": {"title": "x"}}

    def test_concurrent_claims_execute_once_and_replay_result(self):
        key = str(uuid.uuid4())
        request = self.request()
        with ThreadPoolExecutor(max_workers=4) as pool:
            claims = list(pool.map(lambda _: hub_journal.claim(key, request, gated=False, approved=False), range(4)))
        self.assertEqual(sum(execute for _, execute in claims), 1)
        record = next(record for record, execute in claims if execute)
        hub_journal.complete(record, "succeeded", result={"id": 42})
        replay, execute = hub_journal.claim(key, request, gated=False, approved=False)
        self.assertFalse(execute)
        self.assertEqual(replay["result"], {"id": 42})

    def test_approval_freezes_input_contract_and_target(self):
        key = str(uuid.uuid4())
        request = self.request()
        record, execute = hub_journal.claim(key, request, gated=True, approved=False)
        self.assertEqual(record["state"], "pending_approval")
        self.assertFalse(execute)
        for field, value in [("input", {}), ("contract", "v2"), ("target", {"lane": "elsewhere"})]:
            with self.assertRaises(HubError):
                hub_journal.claim(key, {**request, field: value}, gated=True, approved=True)
        record, execute = hub_journal.claim(key, request, gated=True, approved=True)
        self.assertTrue(execute)
        self.assertEqual(record["approval"]["kind"], "explicit_cli_consent")
        self.assertFalse(hub_journal.claim(key, request, gated=True, approved=True)[1])

    def test_unknown_never_reexecutes_and_cli_reports_nonzero(self):
        key = str(uuid.uuid4())
        record, _ = hub_journal.claim(key, self.request(), gated=False, approved=False)
        hub_journal.complete(record, "unknown")
        self.assertFalse(hub_journal.claim(key, self.request(), gated=False, approved=False)[1])
        status, envelope = run("invocation", key, "--target-ref", self.pin["targetRef"])
        self.assertEqual(status, 3)
        self.assertFalse(envelope["ok"])
        self.assertEqual(envelope["data"]["state"], "unknown")

    def test_local_knowledge_is_explicitly_unsupported(self):
        status, envelope = run("knowledge-search", "customer", "--target-ref", self.pin["targetRef"])
        self.assertEqual(status, 3)
        self.assertEqual(envelope["error"]["code"], "unsupported_capability")

    def test_broken_legacy_app_does_not_abort_search(self):
        with patch("tangram_app.cli_hub.list_installed", return_value=[{"id": "broken/app"}]), \
             patch("tangram_app.cli_hub._installed", side_effect=ValueError("broken")):
            status, result = run("search", "", "--target-ref", self.pin["targetRef"])
        self.assertEqual(status, 0)
        self.assertEqual(result["data"]["unavailableApps"], [{"app": "broken/app", "code": "catalog_unavailable"}])

    def test_workspace_wire_and_knowledge_paging(self):
        (self.home / ".credentials").write_text(json.dumps([{"instance": "dev", "url": "localhost", "token": "one"}]))
        pin = run("resolve", "--workspace", "dev/demo")[1]["data"]
        common = ["--target-ref", pin["targetRef"]]
        with patch("tangram_app.cli_hub.os_request", return_value={"state": "succeeded"}) as request:
            key = str(uuid.uuid4())
            status, result = run("call", "group/app#Item.Write", "--contract", "v1", "--invocation-id", key,
                                 "--input-json", '{"title":"hello"}', "--allow-mutation", "--approve", *common)
            self.assertEqual(status, 0, result)
            self.assertEqual(request.call_args.args[2], "/api/core/v1/workspaces/demo/hub/call")
            self.assertEqual(request.call_args.args[3], {"action": "group/app#Item.Write", "contract": "v1", "input": {"title": "hello"},
                                                      "invocationId": key, "allowMutation": True})
        with patch("tangram_app.cli_hub.os_request", return_value={"state": "running", "results": []}):
            self.assertEqual(run("knowledge-search", "hello", *common)[0], 0)
            self.assertNotEqual(run("knowledge-search", "hello", "--offset", "1", *common)[0], 0)
        source = str(uuid.uuid4())
        with patch("tangram_app.cli_hub.os_request", return_value={"text": "abcdefgh", "extractor": "test"}) as request:
            status, result = run("knowledge-read", source, "--offset", "2", "--limit", "3", *common)
            self.assertEqual(result["data"]["text"], "cde")
            self.assertEqual(result["data"]["nextOffset"], 5)
            self.assertEqual(request.call_args.args[2], f"/api/core/v1/knowledge/documents/{source}/text?workspace=demo")
        with patch("tangram_app.cli_hub.os_request", return_value={"text": None}):
            self.assertEqual(run("knowledge-read", source, *common)[1]["error"]["code"], "source_not_found")

    def test_cli_mutation_uses_desktop_even_with_caller_approval_flags(self):
        app = TangramApp.from_graph(CapabilityGraph.from_dict(graph_dict()))
        entry = {"id": "com.example/orders", "root": str(self.home)}
        with patch("tangram_app.cli_hub.resolve_installed", return_value=entry), \
             patch("tangram_app.cli_hub.TangramApp.from_package", return_value=app), \
             patch("tangram_app.desktop_invocation.call", return_value={"state": "succeeded", "result": 1}) as desktop, \
             patch.object(TangramApp, "bind") as bind:
            command = ["call", "com.example/orders#Order.Approve@approveOrder", "--contract", "sha256:fixture",
                       "--target-ref", self.pin["targetRef"], "--invocation-id", str(uuid.uuid4()), "--input-json", '{"orderId":1}']
            self.assertEqual(run(*command)[1]["data"]["state"], "succeeded")
            self.assertEqual(run(*command, "--allow-mutation", "--approve")[1]["data"]["state"], "succeeded")
            command[1] = "com.example/orders#Order.Approve"
            self.assertEqual(run(*command)[1]["data"]["state"], "succeeded")
            self.assertEqual(desktop.call_count, 3)
            for call in desktop.call_args_list:
                self.assertEqual(call.args[0].action, "com.example/orders#Order.Approve@approveOrder")
                self.assertEqual(call.args[1], {"orderId": 1})
            bind.assert_not_called()

    def test_inspect_and_call_use_frozen_contract_and_existing_host(self):
        app = TangramApp.from_graph(CapabilityGraph.from_dict(graph_dict()))
        entry = {"id": "com.example/orders", "root": str(self.home)}
        class Bound:
            graph = app.graph
            async def call(self, binding, arguments):
                return {"orders": [arguments]}
        with patch("tangram_app.cli_hub.resolve_installed", return_value=entry), \
             patch("tangram_app.cli_hub.TangramApp.from_package", return_value=app), \
             patch("tangram_app.cli._load_app", return_value=app), \
             patch("tangram_app.cli.attach_url", return_value="http://127.0.0.1:1234"), \
             patch.object(TangramApp, "bind", return_value=Bound()) as bind:
            common = ["com.example/orders#Order.List@listOrders", "--contract", "sha256:fixture", "--target-ref", self.pin["targetRef"]]
            status, result = run("inspect", *common)
            self.assertEqual(status, 0, result)
            self.assertIn("status", result["data"]["inputSchema"]["properties"])
            call = ["call", *common, "--invocation-id", str(uuid.uuid4()), "--input-json", '{"status":"open"}']
            first = run(*call)
            self.assertEqual(first[0], 0, first)
            self.assertEqual(run(*call), first)
            self.assertEqual(bind.call_count, 1)
            stale = common.copy()
            stale[2] = "sha256:changed"
            self.assertEqual(run("inspect", *stale)[1]["error"]["code"], "contract_changed")
