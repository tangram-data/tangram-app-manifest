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
