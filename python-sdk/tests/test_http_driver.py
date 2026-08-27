from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from tangram_app import (
    CapabilityGraph,
    CapabilityGraphError,
    DriverError,
    HttpExecutionDriver,
    HttpResponseError,
    LocalHttpDriver,
    OpenApiRequestRenderer,
    TangramHost,
    compile_manifest,
)


FIXTURE = Path(__file__).parent / "fixtures/minimal-app"


def http_graph() -> CapabilityGraph:
    return CapabilityGraph.from_dict(
        {
            "formatVersion": "1",
            "manifestSpecVersion": "v1",
            "package": {
                "id": "com.example/http",
                "version": "0.1.0",
                "digest": "sha256:http-fixture",
            },
            "actions": [
                {
                    "id": "com.example/http#Thing.Get",
                    "resourceType": "Thing",
                    "name": "Get",
                    "description": "Get a thing",
                    "effect": "Stateless",
                    "idempotent": True,
                    "requiresConfirmation": False,
                    "requiredPrivileges": ["Thing:Get"],
                    "bindings": [
                        {
                            "id": "com.example/http#Thing.Get@getThing",
                            "operationId": "getThing",
                            "method": "POST",
                            "path": "/things/{thingId}",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "thingId": {"type": "string"},
                                    "tag": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "trace": {"type": "string"},
                                    "name": {"type": "string"},
                                },
                                "required": ["thingId", "name"],
                                "additionalProperties": False,
                            },
                            "inputBindings": {
                                "thingId": {"location": "path", "name": "thingId"},
                                "tag": {"location": "query", "name": "tag"},
                                "trace": {"location": "header", "name": "X-Trace"},
                                "name": {"location": "body", "name": "name"},
                            },
                            "outputSchema": {"type": "object"},
                        }
                    ],
                }
            ],
        }
    )


class _Handler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "http://example.com/")
            self.end_headers()
            return
        if parsed.path == "/error":
            self._json(503, {"message": "sensitive upstream detail"})
            return
        if parsed.path == "/large":
            payload = b"x" * 256
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.requests.append(
            {
                "method": "GET",
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "headers": dict(self.headers.items()),
            }
        )
        self._json(
            200,
            {"orders": [], "status": parse_qs(parsed.query).get("status", [None])[0]},
        )

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length)) if length else None
        self.requests.append(
            {
                "method": "POST",
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )
        self._json(200, {"ok": True})

    def _json(self, status: int, value) -> None:
        payload = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args) -> None:
        return None


class HttpDriverTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        _Handler.requests.clear()

    async def test_renderer_reconstructs_path_query_header_and_body(self) -> None:
        _, binding = http_graph().resolve("com.example/http#Thing.Get")
        rendered = OpenApiRequestRenderer(
            self.base_url + "/api",
            headers={"Authorization": "Bearer configured"},
        ).render(
            binding,
            {
                "thingId": "a b",
                "tag": ["one", "two"],
                "trace": "trace-1",
                "name": "demo",
            },
        )

        parsed = urlsplit(rendered.url)
        self.assertEqual(parsed.path, "/api/things/a%20b")
        self.assertEqual(parse_qs(parsed.query), {"tag": ["one", "two"]})
        self.assertEqual(rendered.headers["X-Trace"], "trace-1")
        self.assertEqual(rendered.headers["Authorization"], "Bearer configured")
        self.assertEqual(json.loads(rendered.body), {"name": "demo"})

        with self.assertRaisesRegex(DriverError, "path separator"):
            OpenApiRequestRenderer(self.base_url).render(
                binding,
                {"thingId": "../admin", "name": "demo"},
            )

        empty_body = replace(
            binding,
            path="/empty",
            input_bindings={},
            body_required=True,
        )
        required_body = OpenApiRequestRenderer(self.base_url).render(empty_body, {})
        self.assertEqual(required_body.body, b"{}")

    async def test_compiled_manifest_invokes_local_backend_through_host(self) -> None:
        graph = compile_manifest(FIXTURE).graph
        host = TangramHost(graph, driver=LocalHttpDriver(self.base_url))

        result = await host.call("com.example/orders#Order.List", {"status": "pending"})

        self.assertEqual(result, {"orders": [], "status": "pending"})
        self.assertEqual(_Handler.requests[0]["path"], "/orders")
        self.assertEqual(_Handler.requests[0]["query"], {"status": ["pending"]})

    async def test_host_executes_rendered_post_binding(self) -> None:
        host = TangramHost(http_graph(), driver=LocalHttpDriver(self.base_url))

        result = await host.call(
            "com.example/http#Thing.Get",
            {
                "thingId": "thing 1",
                "tag": ["a", "b"],
                "trace": "trace-2",
                "name": "demo",
            },
        )

        self.assertEqual(result, {"ok": True})
        request = _Handler.requests[0]
        self.assertEqual(request["path"], "/things/thing%201")
        self.assertEqual(request["query"], {"tag": ["a", "b"]})
        self.assertEqual(request["headers"]["X-Trace"], "trace-2")
        self.assertEqual(request["body"], {"name": "demo"})

    async def test_remote_network_requires_explicit_opt_in(self) -> None:
        with self.assertRaisesRegex(DriverError, "remote HTTP backends are disabled"):
            HttpExecutionDriver("https://api.example.com")
        driver = HttpExecutionDriver("https://api.example.com", allow_remote=True)
        self.assertEqual(driver.renderer.hostname, "api.example.com")

    async def test_redirects_are_not_followed_and_error_body_is_redacted(self) -> None:
        action, binding = http_graph().resolve("com.example/http#Thing.Get")
        driver = LocalHttpDriver(self.base_url)
        with self.assertRaises(HttpResponseError) as redirected:
            await driver.invoke(
                action, replace(binding, method="GET", path="/redirect"), {}
            )
        self.assertEqual(redirected.exception.status, 302)

        with self.assertRaises(HttpResponseError) as failed:
            await driver.invoke(
                action, replace(binding, method="GET", path="/error"), {}
            )
        self.assertEqual(failed.exception.status, 503)
        self.assertTrue(failed.exception.retryable)
        self.assertNotIn("sensitive", str(failed.exception))

    async def test_transport_errors_do_not_disclose_request_urls(self) -> None:
        action, binding = http_graph().resolve("com.example/http#Thing.Get")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            unused_port = probe.getsockname()[1]
        driver = LocalHttpDriver(f"http://127.0.0.1:{unused_port}")
        with self.assertRaises(DriverError) as failed:
            await driver.invoke(
                action, replace(binding, method="GET", path="/private?token=secret"), {}
            )
        self.assertEqual(str(failed.exception), "HTTP transport error")

    async def test_response_size_limit_is_enforced(self) -> None:
        action, binding = http_graph().resolve("com.example/http#Thing.Get")
        driver = LocalHttpDriver(self.base_url, max_response_bytes=32)
        with self.assertRaisesRegex(DriverError, "response exceeds"):
            await driver.invoke(
                action, replace(binding, method="GET", path="/large"), {}
            )

    async def test_agent_cannot_supply_authorization_or_malformed_headers(self) -> None:
        _, binding = http_graph().resolve("com.example/http#Thing.Get")
        authorization_binding = replace(
            binding,
            input_bindings={
                "trace": replace(binding.input_bindings["trace"], name="Authorization")
            },
        )
        renderer = OpenApiRequestRenderer(
            self.base_url,
            headers={"Authorization": "Bearer configured"},
        )

        with self.assertRaisesRegex(DriverError, "reserved"):
            renderer.render(authorization_binding, {"trace": "Bearer attacker"})
        with self.assertRaisesRegex(DriverError, "contains a newline"):
            OpenApiRequestRenderer(self.base_url).render(
                binding, {"trace": "safe\r\nX-Injected: true"}
            )

    async def test_renderer_enforces_request_url_and_header_limits(self) -> None:
        _, binding = http_graph().resolve("com.example/http#Thing.Get")
        arguments = {"thingId": "one", "name": "too large"}

        with self.assertRaisesRegex(DriverError, "request body exceeds"):
            OpenApiRequestRenderer(self.base_url, max_request_bytes=4).render(
                binding, arguments
            )
        with self.assertRaisesRegex(DriverError, "URL exceeds"):
            OpenApiRequestRenderer(self.base_url, max_url_length=16).render(
                binding, arguments
            )
        with self.assertRaisesRegex(DriverError, "headers exceed"):
            OpenApiRequestRenderer(self.base_url, max_header_bytes=8).render(
                binding, arguments
            )

    async def test_query_objects_fail_closed(self) -> None:
        _, binding = http_graph().resolve("com.example/http#Thing.Get")
        with self.assertRaisesRegex(DriverError, "cannot be an object"):
            OpenApiRequestRenderer(self.base_url).render(binding, {"tag": {"a": 1}})

    async def test_static_operation_paths_reject_traversal(self) -> None:
        for path in (
            "/../admin",
            "/%2e%2e/admin",
            "/%252e%252e/admin",
            "/%25252e%25252e/admin",
            "/..\\admin",
        ):
            with self.subTest(path=path):
                graph = http_graph().to_dict()
                graph["actions"][0]["bindings"][0]["path"] = path
                with self.assertRaisesRegex(CapabilityGraphError, "traversal"):
                    CapabilityGraph.from_dict(graph)


if __name__ == "__main__":
    unittest.main()
