import contextlib
import hashlib
import io
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from processwatch.api import create_app
from processwatch.cli import main
from processwatch.config import Config
from processwatch.database_paths import validate_database_path
from processwatch.repository import SQLiteRepository
from processwatch.worker import start_background_worker


class DatabaseEnvironmentSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.primary = self.root / "operational.sqlite3"
        connection = sqlite3.connect(self.primary)
        try:
            connection.execute("CREATE TABLE preserved(value TEXT)")
            connection.execute("INSERT INTO preserved VALUES ('keep')")
            connection.execute("PRAGMA user_version=13")
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        self.temp.cleanup()

    def digest(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def test_test_database_is_resolved_and_opened(self):
        relative = "isolated.sqlite3"
        original_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            resolved = validate_database_path(relative, environment="test", primary_path=self.primary)
            self.assertTrue(Path(resolved).is_absolute())
            repo = SQLiteRepository(resolved, environment="test", primary_path=self.primary)
            self.assertEqual(repo.db.execute("PRAGMA user_version").fetchone()[0], 18)
            repo.close()
        finally:
            os.chdir(original_cwd)

    def test_test_and_homologation_reject_primary_before_sqlite_writes(self):
        before = self.digest(self.primary)
        for environment in ("test", "homologation"):
            with self.subTest(environment=environment), self.assertRaisesRegex(ValueError, "Banco recusado"):
                SQLiteRepository(str(self.primary), environment=environment, primary_path=self.primary)
        self.assertEqual(self.digest(self.primary), before)
        with contextlib.closing(sqlite3.connect(self.primary)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 13)
        self.assertFalse(list(self.root.glob("*.pre-v16-*.backup.sqlite3")))

    def test_development_rejects_relative_alias_to_primary(self):
        alias = self.root / "alias.sqlite3"
        try:
            os.link(self.primary, alias)
        except OSError as exc:
            self.skipTest(f"hard link indisponível neste volume: {exc}")
        with self.assertRaisesRegex(ValueError, "Banco recusado"):
            validate_database_path(str(alias), environment="development", primary_path=self.primary)

    def test_symlink_alias_to_primary_is_rejected(self):
        alias = self.root / "alias-link.sqlite3"
        try:
            alias.symlink_to(self.primary)
        except OSError as exc:
            self.skipTest(f"symlink indisponível neste ambiente: {exc}")
        with self.assertRaisesRegex(ValueError, "Banco recusado"):
            validate_database_path(str(alias), environment="homologation", primary_path=self.primary)

    def test_principal_environment_keeps_operational_compatibility(self):
        repo = SQLiteRepository(str(self.primary), environment="principal", primary_path=self.primary)
        self.assertEqual(repo.db.execute("PRAGMA user_version").fetchone()[0], 18)
        repo.close()

    def test_api_rejects_injected_primary_for_test_before_serving(self):
        repo = SQLiteRepository(str(self.primary), environment="principal", primary_path=self.primary)
        try:
            config = Config(db=str(self.primary), environment="test", primary_db=str(self.primary))
            with self.assertRaisesRegex(ValueError, "Banco recusado"):
                create_app(repo, config=config)
        finally:
            repo.close()

    def test_worker_rejects_primary_before_starting_thread(self):
        config = Config(db=str(self.primary), environment="test", primary_db=str(self.primary))
        with self.assertRaisesRegex(ValueError, "Banco recusado"):
            start_background_worker(str(self.primary), config)

    def test_cli_rejects_primary_before_migration_or_worker_start(self):
        before = self.digest(self.primary)
        stderr = io.StringIO()
        with patch.dict(os.environ, {
            "PROCESSWATCH_ENV": "test",
            "PROCESSWATCH_DB": str(self.primary),
            "PROCESSWATCH_PRIMARY_DB": str(self.primary),
        }, clear=False), contextlib.redirect_stderr(stderr):
            result = main(["list"])
        self.assertEqual(result, 1)
        self.assertIn("Banco recusado", stderr.getvalue())
        self.assertEqual(self.digest(self.primary), before)
        with contextlib.closing(sqlite3.connect(self.primary)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 13)
        self.assertFalse(list(self.root.glob("*.pre-v16-*.backup.sqlite3")))


if __name__ == "__main__":
    unittest.main()
