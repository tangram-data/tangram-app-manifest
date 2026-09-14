import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from tangram_app.hub_target import HubError
from tangram_app import desktop_invocation, supervisor_worker

class DesktopInvocationTest(unittest.TestCase):
    def test_missing_desktop_is_an_explicit_error(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"TANGRAM_HOME": home}):
            with self.assertRaisesRegex(HubError, "Open Tangram Desktop"):
                desktop_invocation.call(types.SimpleNamespace(), {})

    def test_transport_pins_generation_and_omits_caller_approval(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"TANGRAM_HOME": home}):
            Path(home, "runtime.json").write_text(json.dumps({"port": 1234, "token": "fixture", "generation": "g"}))
            args = types.SimpleNamespace(invocation_id="i", action="g/a#Thing.Do", contract="digest", timeout=300, approve=True)
            response = io.BytesIO(json.dumps({"invocationId": "i", "state": "succeeded", "result": 42}).encode())
            with patch("urllib.request.OpenerDirector.open", return_value=response) as send:
                self.assertEqual(desktop_invocation.call(args, {})["result"], 42)
                request = send.call_args.args[0]
                body = json.loads(request.data)
                self.assertEqual(body["generation"], "g")
                self.assertNotIn("approve", body)
                self.assertNotIn("confirmed", body)
                self.assertEqual(request.full_url, "http://127.0.0.1:1234/api/local/v1/app-invocations")

    def test_worker_revalidates_and_requires_the_exact_decision(self):
        request = {"invocationId": "i", "action": "g/a#Thing.Do", "contract": "digest", "input": {}, "timeoutSeconds": 30}
        app = types.SimpleNamespace(graph=types.SimpleNamespace(package=types.SimpleNamespace(digest="digest"), runtime_requirements={"backend": "connector"}))
        action = types.SimpleNamespace(effect=types.SimpleNamespace(value="Irreversible"), requires_confirmation=True, resource_type="Thing", name="Do")
        binding = types.SimpleNamespace(id="g/a#Thing.Do@doThing", input_schema={}, operation_id="doThing")
        metadata = ({"root": "/fixture"}, app, action, binding)
        for decision, trusted in [('', False), ('{"execute":"other"}', False), ('{"execute":"i"}', False), ('{"execute":"i"}', True)]:
            approved = decision == '{"execute":"i"}' and trusted
            with self.subTest(decision=decision):
                wire = json.dumps(request) + "\n" + decision + "\n"
                output = io.StringIO()
                with patch("sys.stdin", io.StringIO(wire)), contextlib.redirect_stdout(output), \
                     patch("tangram_app.cli_hub._local_inspect", return_value=metadata) as inspect, \
                     patch("tangram_app.schema.validate"), patch("tangram_app.supervisor_identity.authenticated_supervisor", return_value=trusted), patch("tangram_app.cli._run", return_value={"value": 1}) as run:
                    supervisor_worker.main()
                messages = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual(messages[0]["state"], "prepared")
                self.assertEqual(inspect.call_count, 2 if decision == '{"execute":"i"}' else 1)
                self.assertEqual(run.call_count, 1 if approved else 0)
                self.assertEqual(messages[-1]["state"], "succeeded" if approved else "failed")

    def test_source_worker_holds_both_locks_until_scala_finishes(self):
        from tangram_app.local_runtime import _ProjectLock, project_lock_path, LocalRuntimeError
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"TANGRAM_HOME": home}):
            root = Path(home, "app")
            (root / "manifests").mkdir(parents=True)
            request = {"invocationId": "i", "action": "g/a#Thing.Do", "contract": "digest", "input": {}, "timeoutSeconds": 30}
            app = types.SimpleNamespace(graph=types.SimpleNamespace(package=types.SimpleNamespace(digest="digest"), runtime_requirements={}))
            action = types.SimpleNamespace(effect=types.SimpleNamespace(value="Irreversible"), requires_confirmation=True, resource_type="Thing", name="Do")
            binding = types.SimpleNamespace(id="g/a#Thing.Do@doThing", input_schema={}, operation_id="doThing")
            testcase = self
            class Input(io.StringIO):
                def readline(self, *args):
                    if self.tell() > 0:
                        for path in (project_lock_path(root), root / ".preview/runtime.lock"):
                            with testcase.assertRaises(LocalRuntimeError):
                                _ProjectLock.acquire(path)
                    return super().readline(*args)
            wire = json.dumps(request) + '\n{"execute":"i"}\n{"finished":"i"}\n'
            output = io.StringIO()
            with patch("sys.stdin", Input(wire)), contextlib.redirect_stdout(output), \
                 patch("tangram_app.cli_hub._local_inspect", return_value=({"root": str(root)}, app, action, binding)), \
                 patch("tangram_app.schema.validate"), patch("tangram_app.cli._run") as run:
                supervisor_worker.main()
                run.assert_not_called()
            self.assertEqual([json.loads(line)["state"] for line in output.getvalue().splitlines()], ["prepared", "validated"])
            for path in (project_lock_path(root), root / ".preview/runtime.lock"):
                lock = _ProjectLock.acquire(path)
                lock.close()

    def test_invalid_discovery_is_a_pre_dispatch_failure(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"TANGRAM_HOME": home}):
            Path(home, "runtime.json").write_text('{')
            with patch('urllib.request.OpenerDirector.open') as send:
                with self.assertRaises(HubError) as caught:
                    desktop_invocation.call(types.SimpleNamespace(), {})
            self.assertEqual(caught.exception.code, 'approver_unavailable')
            send.assert_not_called()

    def test_persistent_worker_reuses_locks_and_revalidates_each_invocation(self):
        from tangram_app.local_runtime import _ProjectLock, project_lock_path, LocalRuntimeError
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"TANGRAM_HOME": home}):
            root = Path(home, "app")
            (root / "manifests").mkdir(parents=True)
            app = types.SimpleNamespace(graph=types.SimpleNamespace(package=types.SimpleNamespace(digest="digest"), runtime_requirements={}))
            action = types.SimpleNamespace(effect=types.SimpleNamespace(value="Irreversible"), requires_confirmation=True, resource_type="Thing", name="Do")
            binding = types.SimpleNamespace(id="g/a#Thing.Do@doThing", input_schema={}, operation_id="doThing")
            wire = ""
            for key in ("first", "second"):
                wire += json.dumps({"invocationId": key, "action": binding.id, "contract": "digest",
                                    "input": {}, "timeoutSeconds": 30, "persistent": True}) + "\n"
                wire += json.dumps({"execute": key}) + "\n" + json.dumps({"finished": key}) + "\n"
            output = io.StringIO()
            testcase = self
            class Input(io.StringIO):
                def readline(self, *args):
                    if self.tell() > 0:
                        with testcase.assertRaises(LocalRuntimeError):
                            _ProjectLock.acquire(project_lock_path(root))
                    return super().readline(*args)
            with patch("sys.stdin", Input(wire)), contextlib.redirect_stdout(output), \
                 patch("tangram_app.cli_hub._local_inspect", return_value=({"root": str(root)}, app, action, binding)) as inspect:
                supervisor_worker.main()
            self.assertEqual(inspect.call_count, 4)
            self.assertEqual([json.loads(line)["state"] for line in output.getvalue().splitlines()],
                             ["prepared", "validated", "idle"] * 2)
            lock = _ProjectLock.acquire(project_lock_path(root))
            lock.close()

    def test_reads_use_desktop_runtime_when_desktop_is_present(self):
        from tangram_app.cli_hub import _local_call
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"TANGRAM_HOME": home}):
            Path(home, "runtime.json").write_text("{}")
            args = types.SimpleNamespace(action="g/a#Thing.List", contract="digest", input_json="{}", invocation_id="read-fixture")
            action = types.SimpleNamespace(effect=types.SimpleNamespace(value="Stateless"), requires_confirmation=False)
            binding = types.SimpleNamespace(id="g/a#Thing.List@listThings", input_schema={})
            with patch("tangram_app.cli_hub._local_inspect", return_value=({}, None, action, binding)), \
                 patch("tangram_app.desktop_invocation.call", return_value={"state": "succeeded"}) as desktop, \
                 patch("tangram_app.cli._run") as standalone:
                self.assertEqual(_local_call(args, {"lane": "local"})["state"], "succeeded")
                self.assertEqual(desktop.call_args.args[0].action, binding.id)
                standalone.assert_not_called()

    def test_connection_refused_is_known_not_sent(self):
        import errno
        import uuid
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"TANGRAM_HOME": home}), \
             patch("socket.create_connection", side_effect=ConnectionRefusedError(errno.ECONNREFUSED, "refused")):
            Path(home, "runtime.json").write_text(json.dumps({
                "port": 1234, "token": "fixture", "generation": "g"}))
            args = types.SimpleNamespace(invocation_id=str(uuid.uuid4()), action="g/a#Thing.Do", contract="digest", timeout=1)
            with self.assertRaises(desktop_invocation.DesktopNotRunningError) as caught:
                desktop_invocation.call(args, {})
            self.assertEqual(caught.exception.code, "approver_unavailable")
            self.assertIn("this request was not sent", str(caught.exception))

    def test_response_loss_is_not_classified_as_not_sent(self):
        import errno
        import urllib.error
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"TANGRAM_HOME": home}):
            Path(home, "runtime.json").write_text(json.dumps({"port": 1234, "token": "fixture", "generation": "g"}))
            args = types.SimpleNamespace(invocation_id="i", action="g/a#Thing.Do", contract="digest", timeout=1)
            for failure in (TimeoutError("timeout"), ConnectionResetError(errno.ECONNRESET, "reset")):
                with self.subTest(failure=type(failure).__name__), \
                     patch("urllib.request.OpenerDirector.open", side_effect=urllib.error.URLError(failure)):
                    with self.assertRaises(HubError) as caught:
                        desktop_invocation.call(args, {})
                    self.assertNotIsInstance(caught.exception, desktop_invocation.DesktopNotRunningError)
                    self.assertEqual(caught.exception.code, "supervisor_unavailable")

    def test_stale_desktop_discovery_falls_back_only_for_ungated_reads(self):
        import errno
        import uuid
        from tangram_app.cli_hub import _local_call
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"TANGRAM_HOME": home}), \
             patch("socket.create_connection", side_effect=ConnectionRefusedError(errno.ECONNREFUSED, "refused")):
            Path(home, "runtime.json").write_text(json.dumps({
                "port": 1234, "token": "fixture", "generation": "g"}))
            app = types.SimpleNamespace(graph=types.SimpleNamespace(runtime_requirements={}))
            binding = types.SimpleNamespace(id="g/a#Thing.Do@doThing", input_schema={})
            for effect, confirmation in (("Stateless", False), ("Stateless", True), ("Reversible", False)):
                args = types.SimpleNamespace(action=binding.id, contract="digest", input_json="{}",
                    invocation_id=str(uuid.uuid4()), timeout=1, allow_mutation=False, approve=False)
                action = types.SimpleNamespace(effect=types.SimpleNamespace(value=effect), requires_confirmation=confirmation)
                with self.subTest(effect=effect, confirmation=confirmation), \
                     patch("tangram_app.cli_hub._local_inspect", return_value=({"root": home}, app, action, binding)), \
                     patch("tangram_app.cli._run", return_value={"value": 42}) as standalone:
                    if effect == "Stateless" and not confirmation:
                        result = _local_call(args, {"lane": "local"})
                        self.assertEqual(result["state"], "succeeded")
                        standalone.assert_called_once()
                    else:
                        with self.assertRaises(desktop_invocation.DesktopNotRunningError):
                            _local_call(args, {"lane": "local"})
                        standalone.assert_not_called()

    def test_read_does_not_fall_back_after_ambiguous_desktop_failure(self):
        from tangram_app.cli_hub import _local_call
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"TANGRAM_HOME": home}):
            Path(home, "runtime.json").write_text("{}")
            args = types.SimpleNamespace(action="g/a#Thing.List", contract="digest", input_json="{}", invocation_id="read-fixture")
            action = types.SimpleNamespace(effect=types.SimpleNamespace(value="Stateless"), requires_confirmation=False)
            binding = types.SimpleNamespace(id=args.action, input_schema={})
            with patch("tangram_app.cli_hub._local_inspect", return_value=({}, None, action, binding)), \
                 patch("tangram_app.desktop_invocation.call", side_effect=HubError("supervisor_unavailable", "response lost")), \
                 patch("tangram_app.cli._run") as standalone:
                with self.assertRaises(HubError):
                    _local_call(args, {"lane": "local"})
                standalone.assert_not_called()

    def test_standalone_read_replay_stays_on_its_journal_after_desktop_restart(self):
        from tangram_app import hub_journal
        from tangram_app.cli_hub import _local_call
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"TANGRAM_HOME": home}):
            Path(home, "runtime.json").write_text("{}")
            target = {"lane": "local"}
            args = types.SimpleNamespace(action="g/a#Thing.List", contract="digest", input_json="{}",
                invocation_id="read-fixture", timeout=1, allow_mutation=False, approve=False)
            action = types.SimpleNamespace(effect=types.SimpleNamespace(value="Stateless"), requires_confirmation=False)
            binding = types.SimpleNamespace(id=args.action, input_schema={})
            app = types.SimpleNamespace(graph=types.SimpleNamespace(runtime_requirements={}))
            record, _ = hub_journal.claim(args.invocation_id,
                {"target": target, "action": binding.id, "contract": args.contract, "input": {}},
                gated=False, approved=False)
            completed = hub_journal.complete(record, "succeeded", result={"value": 42})
            with patch("tangram_app.cli_hub._local_inspect", return_value=({"root": home}, app, action, binding)), \
                 patch("tangram_app.desktop_invocation.call") as desktop, \
                 patch("tangram_app.cli._run") as standalone:
                self.assertEqual(_local_call(args, target), completed)
                args.input_json = '{"changed": true}'
                with self.assertRaises(HubError) as caught:
                    _local_call(args, target)
                self.assertEqual(caught.exception.code, "idempotency_conflict")
                desktop.assert_not_called()
                standalone.assert_not_called()
                # A legacy standalone record must never authorize a gated call.
                action.requires_confirmation = True
                desktop.return_value = {"state": "pending_approval"}
                self.assertEqual(_local_call(args, target)["state"], "pending_approval")
                desktop.assert_called_once()
