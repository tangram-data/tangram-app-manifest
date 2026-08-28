"""Behavioral conformance of the staged `tangram` backend module.

The artifact `conformance/backend-sdk-contract-1.json` is the single
contract both implementations must satisfy (this staged module, and the
platform runtime module in the tangram repo, which pins an identical copy).
The sha256 pin below makes silent artifact drift a test failure; changing
the contract is a deliberate two-repo update.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import unittest
from pathlib import Path

ARTIFACT = Path(__file__).resolve().parents[1] / "conformance" / "backend-sdk-contract-1.json"
EXPECTED_SHA256 = "db65a6ce4cf5c630f80dc09dcdab1968e4a92bb44f873c7e8e693df76f6a6da9"


def load_module():
    source = Path(__file__).resolve().parents[1] / "src" / "tangram_app" / "backend_runtime_sdk.py"
    spec = importlib.util.spec_from_file_location("conformance_tangram", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_conformance(case: unittest.TestCase, module) -> None:
    """Assert `module` satisfies the contract artifact. Shared logic — the
    platform repo carries an identical copy of this function."""
    contract = json.loads(ARTIFACT.read_text())
    descriptor = contract["descriptor"]

    for constant in descriptor["module_constants"]:
        case.assertTrue(hasattr(module, constant), f"missing module constant {constant}")

    for name, spec in descriptor["functions"].items():
        fn = getattr(module, name, None)
        case.assertTrue(callable(fn), f"missing function {name}")
        params = inspect.signature(fn).parameters
        for param in spec["params"]:
            case.assertIn(param, params, f"{name} lost param {param!r}")

    for facade, methods in descriptor["facades"].items():
        obj = getattr(module, facade, None)
        case.assertIsNotNone(obj, f"missing facade {facade}")
        for method, params in methods.items():
            fn = getattr(obj, method, None)
            case.assertTrue(callable(fn), f"{facade}.{method} is not callable")
            try:
                accepted = inspect.signature(fn).parameters
            except (TypeError, ValueError):
                continue  # uninspectable — presence suffices
            if any(
                p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                for p in accepted.values()
            ):
                continue  # dynamic facade (e.g. unsupported stub) accepts anything
            for param in params:
                case.assertIn(param, accepted, f"{facade}.{method} lost param {param!r}")

    for vector in contract["vectors"]:
        kind = vector["kind"]
        if kind == "attr_equals":
            case.assertEqual(getattr(module, vector["attr"]), vector["equals"], vector["id"])
        elif kind == "context_env":
            saved = {k: os.environ.pop(k, None) for k in ("TANGRAM_WORKSPACE", "TANGRAM_APP")}
            try:
                os.environ.update(vector["env"])
                ctx = dict(module.context())
                for key, value in vector["expect_subset"].items():
                    case.assertEqual(ctx.get(key), value, f"{vector['id']}: {key}")
            finally:
                for k in ("TANGRAM_WORKSPACE", "TANGRAM_APP"):
                    os.environ.pop(k, None)
                os.environ.update({k: v for k, v in saved.items() if v is not None})
        elif kind == "action_error":
            err = module.ActionError(*vector["args"])
            case.assertEqual(err.code, vector["expect"]["code"], vector["id"])
            case.assertEqual(err.retryable, vector["expect"]["retryable"], vector["id"])
            case.assertIn(vector["expect"]["message_contains"], str(err), vector["id"])
        elif kind == "error_envelope":
            got = module._normalize_error_envelope(vector["input"])
            case.assertEqual(list(got), vector["expect"], vector["id"])
        else:
            case.fail(f"unknown vector kind {kind!r} — update the conformance runner")


class BackendSdkConformanceTest(unittest.TestCase):
    def test_artifact_is_pinned(self):
        digest = hashlib.sha256(ARTIFACT.read_bytes()).hexdigest()
        self.assertEqual(
            digest,
            EXPECTED_SHA256,
            "conformance artifact changed — update BOTH repos' pins deliberately",
        )

    def test_staged_module_satisfies_the_contract(self):
        run_conformance(self, load_module())


if __name__ == "__main__":
    unittest.main()
