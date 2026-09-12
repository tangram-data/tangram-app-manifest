"""Shared-contract drift checks against the Tangram Desktop repo.

The desktop's `contracts/` directory is the single source for the canonical
policy decision table and the runtime base pins; its Scala runtime asserts
against those files in its own build. These tests are the Python side of the
same bargain, so a one-sided edit fails one of the two builds.

The canonical verdicts are allow / approval-required / deny. The standalone
policy has no approval channel, so its contract is: identical on `allow`,
and never MORE permissive anywhere else (approval-required surfaces here as
deny or confirmation-required — both fail closed). Missing/unrecognized
effects are rejected by the validated capability model before any policy
runs, which is where the canonical `deny` is enforced in this SDK.

Skipped when no desktop checkout is present (set TANGRAM_DESKTOP_REPO to
override the default sibling path).
"""

import asyncio
import json
import os
import pathlib
import unittest

from tangram_app.models import CapabilityEffect, ActionCapability, OperationBinding
from tangram_app.policy import (
    DecisionKind,
    InvocationRequest,
    LocalDevelopmentPolicy,
    Principal,
)

_DESKTOP = pathlib.Path(
    os.environ.get("TANGRAM_DESKTOP_REPO", pathlib.Path.home() / "github" / "tangram-desktop")
)
_CONTRACTS = _DESKTOP / "contracts"


def _load(name):
    return json.loads((_CONTRACTS / name).read_text(encoding="utf-8"))


def _action(effect, requires_confirmation):
    binding = OperationBinding(
        id="v1", operation_id="op", method="post", path="/op", input_schema={}
    )
    return ActionCapability(
        id="Thing.Do",
        resource_type="Thing",
        name="Do",
        description="fixture",
        effect=CapabilityEffect(effect),
        idempotent=False,
        requires_confirmation=requires_confirmation,
        required_privileges=(),
        bindings=(binding,),
    )


@unittest.skipUnless(_CONTRACTS.is_dir(), "tangram-desktop checkout not available")
class ContractFixturesTest(unittest.TestCase):
    def test_policy_matches_canonical_table(self):
        cases = _load("policy-decision-fixtures.json")["cases"]
        self.assertTrue(cases)
        policy = LocalDevelopmentPolicy()  # no grants: the bare standalone stance
        for case in cases:
            effect = case["effect"]
            flag = case["requiresConfirmation"]
            verdict = case["verdict"]
            with self.subTest(effect=effect, requiresConfirmation=flag):
                if effect is None or effect not in {e.value for e in CapabilityEffect}:
                    # Canonical deny: the model layer refuses the declaration.
                    self.assertEqual(verdict, "deny")
                    with self.assertRaises(Exception):
                        _action(effect, flag)
                    continue
                action = _action(effect, flag)
                request = InvocationRequest(
                    principal=Principal(id="tester"),
                    action=action,
                    binding=action.bindings[0],
                    arguments={},
                )
                decision = asyncio.run(policy.authorize(request))
                if verdict == "allow":
                    self.assertIs(decision.kind, DecisionKind.ALLOW)
                else:
                    # approval-required (or stricter): never an allow here.
                    self.assertIsNot(decision.kind, DecisionKind.ALLOW)

    def test_base_pins_match_contract(self):
        from tangram_app.local_runtime import _BASE_PINS

        contract = _load("runtime-base-pins.json")["basePins"]
        self.assertEqual(list(_BASE_PINS), contract)


if __name__ == "__main__":
    unittest.main()
