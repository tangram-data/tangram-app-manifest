"""Loopback backend-actions surface (composable-app-sdk §5.3 parity)."""

from __future__ import annotations

import importlib.util
import json
import os
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from tangram_app.local_actions import LocalActionsServer


class _Effect:
    def __init__(self, value: str) -> None:
        self.value = value


def _action(resource_type, name, effect="Stateless", requires_confirmation=False):
    return SimpleNamespace(
        id=f"com.example/demo#{resource_type}.{name}@op{name}",
        resource_type=resource_type,
        name=name,
        effect=_Effect(effect),
        requires_confirmation=requires_confirmation,
    )


class _Bound:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def call(self, action_id, arguments):
        self.calls.append((action_id, arguments))
        return self._result


def _session(actions, result=None):
    bound = _Bound(result if result is not None else {"ok": True})
    app = SimpleNamespace(
        graph=SimpleNamespace(actions=actions),
        bind=lambda **kwargs: bound,
    )
    session = SimpleNamespace(
        app=app,
        backend_url="http://127.0.0.1:1",
        request_timeout_seconds=5.0,
    )
    return session, bound


def _post(url, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{url}/actions/invoke",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


class LocalActionsServerTest(unittest.TestCase):
    def setUp(self):
        self.server = LocalActionsServer.start()
        self.addCleanup(self.server.close)

    def test_refuses_calls_without_the_session_token(self):
        status, body = _post(self.server.url, {"resource_type": "Todo", "action": "List"})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthenticated")
        self.assertIn("token", body["error"]["message"])

    def test_answers_503_before_a_session_attaches(self):
        status, body = _post(self.server.url, {"resource_type": "Todo", "action": "List"}, self.server.token)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "host_starting")
        self.assertTrue(body["error"]["retryable"])
        self.assertIn("starting", body["error"]["message"])

    def test_invokes_an_unattended_safe_action_through_the_host(self):
        session, bound = _session([_action("Todo", "List")], result={"rows": []})
        self.server.attach(session)
        status, body = _post(
            self.server.url,
            {"resource_type": "Todo", "action": "List", "args": {"q": "x"}},
            self.server.token,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["result"], {"rows": []})
        self.assertEqual(bound.calls, [("com.example/demo#Todo.List@opList", {"q": "x"})])

    def test_refuses_confirmation_gated_and_irreversible_actions(self):
        session, bound = _session(
            [
                _action("Todo", "Purge", effect="Irreversible"),
                _action("Todo", "Close", effect="Reversible", requires_confirmation=True),
            ]
        )
        self.server.attach(session)
        for name in ("Purge", "Close"):
            status, body = _post(self.server.url, {"resource_type": "Todo", "action": name}, self.server.token)
            self.assertEqual(status, 403)
            self.assertEqual(body["error"]["code"], "confirmation_required_unattended")
            self.assertIn("cannot run from backend code", body["error"]["message"])
        self.assertEqual(bound.calls, [])

    def test_refuses_cross_app_and_unknown_actions(self):
        session, _ = _session([_action("Todo", "List")])
        self.server.attach(session)
        status, body = _post(
            self.server.url,
            {"resource_type": "Note", "action": "List", "app": "other/app"},
            self.server.token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "cross_app_unsupported")
        self.assertIn("requires Tangram OS", body["error"]["message"])
        status, body = _post(self.server.url, {"resource_type": "Ghost", "action": "List"}, self.server.token)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_request")
        self.assertIn("no unambiguous action", body["error"]["message"])


class StagedBackendSdkTest(unittest.TestCase):
    """The staged `tangram` module's actions/sql facades, driven over the wire."""

    def _load_staged_module(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "tangram_app"
            / "backend_runtime_sdk.py"
        )
        spec = importlib.util.spec_from_file_location("staged_tangram", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_invoke_posts_to_the_local_endpoint_and_unwraps_result(self):
        server = LocalActionsServer.start()
        self.addCleanup(server.close)
        session, _ = _session([_action("Todo", "List")], result=[1, 2])
        server.attach(session)
        module = self._load_staged_module()
        os.environ["TANGRAM_LOCAL_ACTIONS_URL"] = server.url
        os.environ["TANGRAM_LOCAL_ACTIONS_TOKEN"] = server.token
        self.addCleanup(os.environ.pop, "TANGRAM_LOCAL_ACTIONS_URL", None)
        self.addCleanup(os.environ.pop, "TANGRAM_LOCAL_ACTIONS_TOKEN", None)
        self.assertEqual(module.actions.invoke("Todo", "List"), [1, 2])
        with self.assertRaises(RuntimeError) as gated:
            module.actions.invoke("Ghost", "List")
        self.assertIn("no unambiguous action", str(gated.exception))

    def test_cross_app_and_sql_fail_with_platform_pointers(self):
        module = self._load_staged_module()
        with self.assertRaises(RuntimeError) as cross:
            module.actions.invoke("Note", "List", app="other/notes")
        self.assertIn("requires Tangram OS", str(cross.exception))
        with self.assertRaises(RuntimeError) as sql:
            module.sql.run("daily_orders")
        self.assertIn("require Tangram OS", str(sql.exception))

    def test_invoke_without_endpoint_env_fails_actionably(self):
        module = self._load_staged_module()
        os.environ.pop("TANGRAM_LOCAL_ACTIONS_URL", None)
        with self.assertRaises(RuntimeError) as missing:
            module.actions.invoke("Todo", "List")
        self.assertIn("did not expose an actions endpoint", str(missing.exception))


if __name__ == "__main__":
    unittest.main()
