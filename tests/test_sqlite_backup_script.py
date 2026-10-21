import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from processwatch.backup import backup_database


class SQLiteBackupScriptTests(unittest.TestCase):
    def test_online_backup_is_independent_and_integrity_checked(self):
        temp_root = Path.cwd() / ".test-temp"
        temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=temp_root) as temp:
            root = Path(temp)
            database = root / "source.sqlite3"
            backup_dir = root / "backups"
            source = sqlite3.connect(database)
            source.execute("PRAGMA journal_mode=WAL")
            source.execute("CREATE TABLE items(value TEXT)")
            source.execute("INSERT INTO items VALUES ('committed-in-wal')")
            source.commit()
            try:
                self.assertTrue(Path(str(database) + "-wal").exists())
                backup = backup_database(database, backup_dir)
                self.assertTrue(backup.is_file())
                if os.name != "nt":
                    self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                check = sqlite3.connect(backup)
                try:
                    self.assertEqual(check.execute("SELECT value FROM items").fetchone()[0],
                                     "committed-in-wal")
                    self.assertEqual(check.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                finally:
                    check.close()
            finally:
                source.close()


if __name__ == "__main__":
    unittest.main()
