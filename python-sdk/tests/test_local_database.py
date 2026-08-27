from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tangram_app import LocalRuntimeError
from tangram_app.local_database import (
    PostgresBinaries,
    apply_migrations,
    load_migrations,
)


class LocalDatabaseTests(unittest.TestCase):
    def test_discovers_explicit_postgres_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("initdb", "postgres", "pg_ctl", "psql"):
                path = root / name
                path.write_text("", encoding="utf-8")
                path.chmod(0o700)

            binaries = PostgresBinaries.discover(
                {"TANGRAM_LOCAL_PG_BIN_DIR": str(root), "PATH": ""}
            )

            self.assertEqual(binaries.postgres, root / "postgres")

    def test_loads_versioned_migrations_in_numeric_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            migrations = root / "manifests/deployment/migrations/main"
            migrations.mkdir(parents=True)
            (migrations / "V2__second.sql").write_text("select 2", encoding="utf-8")
            (migrations / "V1__first.sql").write_text("select 1", encoding="utf-8")

            loaded = load_migrations(root)

            self.assertEqual(loaded, ((1, "select 1"), (2, "select 2")))

    def test_rejects_changed_applied_migration(self) -> None:
        sql = "create table example(id int)"
        responses = iter(("", "1:different-checksum"))
        with patch(
            "tangram_app.local_database._psql",
            side_effect=lambda *_args, **_kwargs: next(responses),
        ):
            with self.assertRaisesRegex(LocalRuntimeError, "changed after"):
                apply_migrations(object(), ((1, sql),))

    def test_records_checksum_in_same_transaction_as_migration(self) -> None:
        sql = "create table example(id int)"
        calls: list[str] = []

        def psql(_database, _name, statement, **_kwargs):
            calls.append(statement)
            return ""

        with patch("tangram_app.local_database._psql", side_effect=psql):
            apply_migrations(object(), ((1, sql),))

        expected = hashlib.sha256(sql.encode()).hexdigest()
        self.assertIn("begin;", calls[-1])
        self.assertIn(sql, calls[-1])
        self.assertIn(expected, calls[-1])
        self.assertIn("commit;", calls[-1])


if __name__ == "__main__":
    unittest.main()
