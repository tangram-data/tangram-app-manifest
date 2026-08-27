from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from tangram_app import (
    ActionEffect,
    ApplicationType,
    ManifestValidationError,
    OpenApiMapping,
    PathParam,
    PklEvaluator,
    PklEvaluationError,
    PklManifestLoader,
    validate_manifest,
)


FIXTURE = Path(__file__).parent / "fixtures/minimal-app"
PROJECT_PACKAGE_FIXTURE = Path(__file__).parent / "fixtures/project-package"


class PklManifestLoaderTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_evaluates_real_pkl_into_manifest_dataclasses(self) -> None:
        package = PklManifestLoader(PklEvaluator(timeout_seconds=10)).load(FIXTURE)

        self.assertEqual(package.application.id, "com.example/orders")
        self.assertIs(package.application.app_type, ApplicationType.APP)
        self.assertEqual(package.application.tags, ("sdk", "fixture"))

        order = package.resource_type("Order")
        self.assertEqual(order.active.version, "v1")
        action = order.active.action("List")
        self.assertIs(action.effect, ActionEffect.STATELESS)
        self.assertEqual(action.effective_privilege, "Read")
        custom_action = replace(action, name="Archive", privilege=None)
        self.assertIsNone(custom_action.effective_privilege)
        self.assertEqual(action.all_open_api_mappings[0].operation_id, "listOrders")
        self.assertEqual(order.active.preset_roles[0].permissions, ("Read",))

        self.assertEqual(package.settings[0].example, "eu-west-2")
        self.assertEqual(package.api_spec["backend"]["serviceName"], "orders")

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_evaluator_denies_ambient_environment_reads(self) -> None:
        evaluator = PklEvaluator(timeout_seconds=10)
        module = Path(__file__).parent / "fixtures/blocked-env.pkl"
        with self.assertRaises(PklEvaluationError) as raised:
            evaluator.evaluate(
                module,
                expression=None,
                root_dir=module.parent,
                project_dir=None,
            )
        self.assertIn("resource allowlist", str(raised.exception))

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_evaluator_denies_direct_package_imports(self) -> None:
        evaluator = PklEvaluator(timeout_seconds=10)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            module = root / "direct-package.pkl"
            module.write_text(
                'import "package://pkg.pkl-lang.org/github.com/tangram-data/'
                'tangram-app-manifest/tangram-app-manifest@1.0.0#/manifest.pkl" as manifest\n'
                "value = manifest\n",
                encoding="utf-8",
            )
            with self.assertRaises(PklEvaluationError) as raised:
                evaluator.evaluate(
                    module,
                    expression=None,
                    root_dir=root,
                    project_dir=None,
                )
        self.assertIn("module allowlist", str(raised.exception))

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_evaluator_allows_project_dependency_aliases(self) -> None:
        evaluator = PklEvaluator(timeout_seconds=10)
        value = evaluator.evaluate(
            PROJECT_PACKAGE_FIXTURE / "main.pkl",
            expression="message",
            root_dir=PROJECT_PACKAGE_FIXTURE.parent,
            project_dir=PROJECT_PACKAGE_FIXTURE,
        )
        self.assertEqual(value, "loaded through projectpackage")

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_full_validation_accepts_valid_fixture(self) -> None:
        result = validate_manifest(FIXTURE)
        self.assertTrue(result.valid, [finding.message for finding in result.errors])
        self.assertEqual(result.require_valid().application.id, "com.example/orders")

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_validation_reports_cross_file_action_errors(self) -> None:
        package = PklManifestLoader().load(FIXTURE)
        resource_type = package.resource_type("Order")
        action = resource_type.active.action("List")
        invalid_action = replace(
            action,
            open_api_mapping=OpenApiMapping(
                operation_id="missingOperation",
                resource_name=(PathParam("missingPathParameter"),),
            ),
        )
        invalid_version = replace(
            resource_type.active,
            actions=(invalid_action, invalid_action),
        )
        invalid_type = replace(
            resource_type,
            active_version="missing-version",
            versions=(invalid_version,),
        )
        invalid_package = replace(
            package,
            resource_type_definitions=(invalid_type,),
        )

        class StaticLoader:
            def load(self, package_root):
                return invalid_package

        result = validate_manifest(FIXTURE, loader=StaticLoader())
        codes = {finding.code for finding in result.errors}
        self.assertIn("action.duplicate", codes)
        self.assertIn("openapi.mapping_missing", codes)
        self.assertIn("resource_type.active_version", codes)
        self.assertFalse(result.valid)
        with self.assertRaises(ManifestValidationError):
            result.require_valid()

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_validation_accepts_custom_role_permission(self) -> None:
        package = PklManifestLoader().load(FIXTURE)
        resource_type = package.resource_type("Order")
        custom_role = replace(
            resource_type.active.preset_roles[0],
            permissions=("ManageRetention",),
        )
        custom_version = replace(resource_type.active, preset_roles=(custom_role,))
        custom_package = replace(
            package,
            resource_type_definitions=(
                replace(resource_type, versions=(custom_version,)),
            ),
        )

        class StaticLoader:
            def load(self, package_root):
                return custom_package

        result = validate_manifest(FIXTURE, loader=StaticLoader())
        self.assertTrue(result.valid, [finding.message for finding in result.errors])
        self.assertNotIn(
            "resource_role.permission",
            {finding.code for finding in result.findings},
        )

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_validation_rejects_duplicate_role_names_in_one_version(self) -> None:
        package = PklManifestLoader().load(FIXTURE)
        resource_type = package.resource_type("Order")
        role = resource_type.active.preset_roles[0]
        duplicate_version = replace(resource_type.active, preset_roles=(role, role))
        duplicate_package = replace(
            package,
            resource_type_definitions=(
                replace(resource_type, versions=(duplicate_version,)),
            ),
        )

        class StaticLoader:
            def load(self, package_root):
                return duplicate_package

        result = validate_manifest(FIXTURE, loader=StaticLoader())
        self.assertIn(
            "resource_role.duplicate",
            {finding.code for finding in result.errors},
        )

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_connector_agent_and_ui_validation_paths(self) -> None:
        package = PklManifestLoader().load(FIXTURE)

        connector_api = dict(package.api_spec or {})
        connector_api.pop("backend", None)
        connector_api.update(
            {
                "auth": {},
                "endpoint": "https://api.example.com",
                "endpointHostAllowlist": ("*api.example.com",),
            }
        )
        connector = replace(
            package,
            application=replace(
                package.application, app_type=ApplicationType.CONNECTOR
            ),
            api_spec=connector_api,
        )

        agent = replace(
            package,
            application=replace(package.application, app_type=ApplicationType.AGENT),
            api_spec=None,
            agent_spec={
                "systemPrompt": "Test agent",
                "defaultLlm": {"provider": "", "model": "test"},
                "tools": ({"name": "Bad-Name", "description": "bad"},),
                "skills": (),
            },
        )

        ui = replace(
            package,
            ui_spec={
                "components": (
                    {"name": "panel", "kind": "declarative", "spec": {}},
                    {"name": "panel", "kind": "declarative", "spec": {}},
                ),
                "deployment": {"mode": "UIComponent", "rootComponent": "missing"},
            },
        )

        class StaticLoader:
            def __init__(self, value):
                self.value = value

            def load(self, package_root):
                return self.value

        connector_codes = {
            finding.code
            for finding in validate_manifest(
                FIXTURE, loader=StaticLoader(connector)
            ).errors
        }
        agent_codes = {
            finding.code
            for finding in validate_manifest(FIXTURE, loader=StaticLoader(agent)).errors
        }
        ui_codes = {
            finding.code
            for finding in validate_manifest(FIXTURE, loader=StaticLoader(ui)).errors
        }

        self.assertIn("connector.allowlist_pattern", connector_codes)
        self.assertIn("agent.default_llm", agent_codes)
        self.assertIn("agent.tool_name", agent_codes)
        self.assertIn("ui.component_duplicate", ui_codes)
        self.assertIn("ui.root_component", ui_codes)

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_app_deployment_and_integrations_report_coverage_warnings(self) -> None:
        package = PklManifestLoader().load(FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "package"
            shutil.copytree(FIXTURE, root)
            deployment = root / "manifests/deployment"
            integrations = root / "manifests/integrations"
            deployment.mkdir()
            integrations.mkdir()
            (deployment / "components.pkl").write_text(
                "component = 1\n", encoding="utf-8"
            )
            (integrations / "example.pkl").write_text("value = 1\n", encoding="utf-8")

            class StaticLoader:
                def load(self, package_root):
                    return package

            result = validate_manifest(root, loader=StaticLoader())

        warning_codes = {finding.code for finding in result.warnings}
        self.assertIn("deployment.not_validated", warning_codes)
        self.assertIn("integrations.not_validated", warning_codes)

    @unittest.skipUnless(shutil.which("pkl"), "Pkl CLI is not installed")
    def test_remote_dependency_lock_requires_sha256(self) -> None:
        package = PklManifestLoader().load(FIXTURE)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "package"
            shutil.copytree(FIXTURE, root)
            lock = {
                "schemaVersion": 1,
                "resolvedDependencies": {
                    "package://example.invalid/schema@1": {
                        "type": "remote",
                        "uri": "projectpackage://example.invalid/schema@1.0.0",
                    }
                },
            }
            (root / "manifests/PklProject.deps.json").write_text(
                json.dumps(lock), encoding="utf-8"
            )

            class StaticLoader:
                def load(self, package_root):
                    return package

            result = validate_manifest(root, loader=StaticLoader())

        self.assertIn(
            "layout.pkl_lock_remote_checksum",
            {finding.code for finding in result.errors},
        )

    def test_validation_reports_missing_layout_without_throwing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = validate_manifest(directory)
            self.assertFalse(result.valid)
        self.assertEqual(result.errors[0].code, "layout.manifests")


if __name__ == "__main__":
    unittest.main()
