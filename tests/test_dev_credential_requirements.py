"""Offline checks for development and test credential requirements.

Run directly to avoid the database-backed pytest fixtures:
    python tests/test_dev_credential_requirements.py
"""

from __future__ import annotations

import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "dev_db.py"

spec = importlib.util.spec_from_file_location("dev_db_under_test", TOOL)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

ROLE_CREDENTIALS = {
    "BLOCKS_DB_MIGRATOR_PASSWORD": "local-migrator-only",
    "BLOCKS_DB_APP_PASSWORD": "local-app-only",
    "BLOCKS_DB_DISPATCHER_PASSWORD": "local-dispatcher-only",
    "BLOCKS_DB_BACKUP_PASSWORD": "local-backup-only",
    "BLOCKS_TEST_ADMIN_PASSWORD": "local-test-admin-only",
}
ADMIN_DSN = "host=invalid.local dbname=blocks user=local_admin"


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __enter__(self) -> "FakeConnection":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


class DevDbCredentialTests(unittest.TestCase):
    def call_tool(self, args: list[str], env: dict[str, str]) -> tuple[int, str, object, object]:
        stderr = io.StringIO()
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, "argv", ["dev_db.py", *args]), \
             patch("sys.stderr", stderr), \
             patch.object(module.psycopg, "connect") as connect, \
             patch.object(module.subprocess, "call") as migrate:
            migrate.return_value = 0
            result = module.main()
        return result, stderr.getvalue(), connect, migrate

    def test_help_needs_no_credentials_or_database(self) -> None:
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "argv", ["dev_db.py", "--help"]), \
             patch("sys.stdout", new_callable=io.StringIO) as stdout, \
             patch.object(module.psycopg, "connect") as connect:
            with self.assertRaises(SystemExit) as stopped:
                module.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertIn("--roles-only", stdout.getvalue())
        self.assertIn("--migrations-only", stdout.getvalue())
        connect.assert_not_called()

    def test_missing_or_blank_admin_dsn_fails_before_db(self) -> None:
        for value in (None, "", " \t "):
            with self.subTest(value_present=value is not None):
                env = dict(ROLE_CREDENTIALS)
                if value is not None:
                    env["BLOCKS_ADMIN_DSN"] = value
                result, stderr, connect, migrate = self.call_tool([], env)
                self.assertEqual(result, 2)
                self.assertIn("BLOCKS_ADMIN_DSN", stderr)
                connect.assert_not_called()
                migrate.assert_not_called()

    def test_migrations_only_rejects_missing_or_blank_migrator_before_db(self) -> None:
        for value in (None, "", "  \t  "):
            with self.subTest(value_present=value is not None):
                env = {"BLOCKS_ADMIN_DSN": ADMIN_DSN}
                if value is not None:
                    env["BLOCKS_DB_MIGRATOR_PASSWORD"] = value
                result, stderr, connect, migrate = self.call_tool(["--migrations-only"], env)
                self.assertEqual(result, 2)
                self.assertIn("BLOCKS_DB_MIGRATOR_PASSWORD", stderr)
                connect.assert_not_called()
                migrate.assert_not_called()

    def test_migrations_only_accepts_explicit_migrator_without_other_role_passwords(self) -> None:
        env = {"BLOCKS_ADMIN_DSN": ADMIN_DSN,
               "BLOCKS_DB_MIGRATOR_PASSWORD": ROLE_CREDENTIALS["BLOCKS_DB_MIGRATOR_PASSWORD"]}
        result, stderr, connect, migrate = self.call_tool(["--migrations-only"], env)
        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        connect.assert_not_called()
        self.assertEqual(migrate.call_count, 1)
        self.assertEqual(migrate.call_args.kwargs["env"]["BLOCKS_DB_MIGRATOR_PASSWORD"],
                         ROLE_CREDENTIALS["BLOCKS_DB_MIGRATOR_PASSWORD"])

    def test_roles_only_rejects_each_missing_or_blank_password_before_db(self) -> None:
        for key in ROLE_CREDENTIALS:
            for value in (None, "", " \t "):
                with self.subTest(key=key, value_present=value is not None):
                    env = {"BLOCKS_ADMIN_DSN": ADMIN_DSN, **ROLE_CREDENTIALS}
                    if value is None:
                        del env[key]
                    else:
                        env[key] = value
                    result, stderr, connect, migrate = self.call_tool(["--roles-only"], env)
                    self.assertEqual(result, 2)
                    self.assertIn(key, stderr)
                    self.assertNotIn("local-", stderr)
                    connect.assert_not_called()
                    migrate.assert_not_called()

    def test_roles_only_accepts_explicit_cli_passwords_and_does_not_migrate(self) -> None:
        arguments = ["--roles-only", "--admin-dsn", ADMIN_DSN]
        for flag, key in (
            ("--migrator-password", "BLOCKS_DB_MIGRATOR_PASSWORD"),
            ("--app-password", "BLOCKS_DB_APP_PASSWORD"),
            ("--dispatcher-password", "BLOCKS_DB_DISPATCHER_PASSWORD"),
            ("--backup-password", "BLOCKS_DB_BACKUP_PASSWORD"),
            ("--test-admin-password", "BLOCKS_TEST_ADMIN_PASSWORD"),
        ):
            arguments.extend((flag, ROLE_CREDENTIALS[key]))
        connection = FakeConnection()
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(sys, "argv", ["dev_db.py", *arguments]), \
             patch.object(module.psycopg, "connect", return_value=connection) as connect, \
             patch.object(module.subprocess, "call") as migrate, \
             patch("sys.stdout", new_callable=io.StringIO):
            result = module.main()
        self.assertEqual(result, 0)
        connect.assert_called_once()
        migrate.assert_not_called()
        self.assertEqual(len(connection.statements), 1)
        self.assertNotIn(":'", connection.statements[0])

    def test_default_mode_bootstraps_then_migrates_with_explicit_credentials(self) -> None:
        connection = FakeConnection()
        env = {"BLOCKS_ADMIN_DSN": ADMIN_DSN, **ROLE_CREDENTIALS}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, "argv", ["dev_db.py"]), \
             patch.object(module.psycopg, "connect", return_value=connection) as connect, \
             patch.object(module.subprocess, "call", return_value=0) as migrate, \
             patch("sys.stdout", new_callable=io.StringIO):
            result = module.main()
        self.assertEqual(result, 0)
        connect.assert_called_once()
        self.assertEqual(len(connection.statements), 1)
        migrate.assert_called_once()


class PytestHarnessCredentialTests(unittest.TestCase):
    def load_harness(self, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        script = (
            "import psycopg, runpy; "
            "psycopg.connect = lambda *a, **k: (_ for _ in ()).throw("
            "AssertionError('database accessed during import')); "
            "runpy.run_path('tests/conftest.py')"
        )
        return subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, env=env,
            capture_output=True, text=True, check=False,
        )

    def test_harness_rejects_missing_or_blank_credentials_before_db(self) -> None:
        for key in ("BLOCKS_DB_MIGRATOR_PASSWORD", "BLOCKS_DB_APP_PASSWORD"):
            for value in (None, "", " \t "):
                with self.subTest(key=key, value_present=value is not None):
                    env = {"BLOCKS_TEST_ADMIN_DSN": ADMIN_DSN, **ROLE_CREDENTIALS}
                    if value is None:
                        del env[key]
                    else:
                        env[key] = value
                    result = self.load_harness(env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(key, result.stderr)
                    self.assertNotIn("database accessed during import", result.stderr)
                    self.assertNotIn("local-", result.stderr)

    def test_harness_accepts_explicit_credentials_without_db(self) -> None:
        result = self.load_harness({"BLOCKS_TEST_ADMIN_DSN": ADMIN_DSN, **ROLE_CREDENTIALS})
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
