"""The pip-side `tangram` shim: routing, native forwarding, self-exclusion."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tangram_app.frontdoor import find_native, main, route


class RouteTest(unittest.TestCase):
    def test_capability_commands_use_public_names(self):
        from tangram_app.cli_parser import build_parser
        parser = build_parser()
        for args in (["context", "resolve", "--workspace", "prod/demo"],
                     ["knowledge", "search", "query", "--target-ref", "ref"],
                     ["knowledge", "read", "id", "--target-ref", "ref"],
                     ["app", "search", "query", "--target-ref", "ref"],
                     ["app", "inspect", "app#Todo.List", "--contract", "v1", "--target-ref", "ref"],
                     ["app", "call", "app#Todo.List", "--contract", "v1", "--target-ref", "ref", "--invocation-id", "id"],
                     ["app", "invocation", "id", "--target-ref", "ref"]):
            self.assertEqual(route(args), args)
            self.assertTrue(parser.parse_args(args).capability_command)
        self.assertNotIn("hub", parser.format_help())
        self.assertIsNone(route(["hub", "call"]))

    def test_global_install_routing(self):
        args = ["app", "install", "./connector", "--global", "--instance", "prod"]
        self.assertEqual(route(args), args)
        for version in ("--version", "-v"):
            self.assertIsNone(route(["app", "install", "com.example", "connector", version, "1.0", "--global"]))
    def test_agent_lane_routes_in_process(self):
        self.assertEqual(route(["doctor", "--fix"]), ["doctor", "--fix"])
        self.assertEqual(route(["app", "list"]), ["app", "list"])
        self.assertEqual(route(["app", "ls"]), ["app", "list"])
        self.assertEqual(route(["app", "open", "todo"]), ["open", "todo"])
        self.assertEqual(route(["call", "todo", "Todo.List", "--local"]),
                         ["call", "todo", "Todo.List", "--local"])

    def test_platform_lane_stays_native(self):
        for args in (
            ["app", "ls", "--workspace", "demo"],
            ["app", "ls", "-w", "demo"],
            ["app", "install", "x", "--workspace=prod/ws"],
            ["use", "dev"],
            ["login"],
            ["workspace", "ls"],
            ["app", "pkg", "install"],
            [],
        ):
            self.assertIsNone(route(args), args)


class FindNativeTest(unittest.TestCase):
    def test_prefers_real_binary_and_skips_shim_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            shim_dir, native_dir = base / "shim", base / "native"
            shim_dir.mkdir(); native_dir.mkdir()
            shim = shim_dir / "tangram"
            shim.write_text("#!python\nfrom tangram_app.frontdoor import main\n")
            shim.chmod(0o755)
            native = native_dir / "tangram"
            native.write_bytes(b"\x7fELF native binary")
            native.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{shim_dir}:{native_dir}"}):
                found = find_native(argv0="/somewhere/else/tangram")
            self.assertEqual(found, str(native))

    def test_excludes_own_entrypoint_and_reports_none(self):
        with tempfile.TemporaryDirectory() as directory:
            own_dir = Path(directory)
            own = own_dir / "tangram"
            own.write_bytes(b"\x7fELF self")
            own.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": str(own_dir)}):
                self.assertIsNone(find_native(argv0=str(own)))


class MainTest(unittest.TestCase):
    def test_agent_command_runs_sdk_in_process(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["doctor"])
        self.assertEqual(code, 0)
        self.assertIn("checks", json.loads(out.getvalue())["data"])

    def test_platform_command_without_native_hints_install(self):
        err = io.StringIO()
        with mock.patch("tangram_app.frontdoor.find_native", return_value=None):
            with contextlib.redirect_stderr(err):
                code = main(["workspace", "ls"])
        self.assertEqual(code, 1)
        self.assertIn("native Tangram CLI", err.getvalue())

    def test_platform_command_execs_native(self):
        with mock.patch("tangram_app.frontdoor.find_native", return_value="/fake/tangram"):
            with mock.patch("tangram_app.frontdoor.os.execv") as execv:
                main(["use", "dev"])
        execv.assert_called_once_with("/fake/tangram", ["/fake/tangram", "use", "dev"])


if __name__ == "__main__":
    unittest.main()
