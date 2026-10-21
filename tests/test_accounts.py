import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.repository import SQLiteRepository, now


NUMBER_A = "00000003800004010000"
NUMBER_B = "10002365820254053305"


class NoopProvider:
    def fetch(self, number, court):
        return {"timed_out": False, "hits": {"total": {"value": 0}, "hits": []}}


class AccountIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "accounts.sqlite3")
        self.repo = SQLiteRepository(self.db)
        self.repo.create_office("office-a", "Escritório A")
        self.repo.create_office("office-b", "Escritório B")
        self.user_a = self.repo.create_user("a@example.test", "senha-super-segura-a-123", "office-a")
        self.user_b = self.repo.create_user("b@example.test", "senha-super-segura-b-123", "office-b", "lawyer")
        engine = Engine(self.repo, NoopProvider())
        engine.register(NUMBER_A, "office-a")
        engine.register(NUMBER_B, "office-b")
        self.app = create_app(self.repo, engine, config=Config(db=self.db, auth_mode="accounts",
                                                               access_token="legacy-token-must-not-authenticate"))

    def tearDown(self):
        if self.repo:
            self.repo.close()
        self.temp.cleanup()

    def test_accounts_login_isolates_process_queries_and_logout(self):
        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                self.assertEqual((await client.get("/api/processes")).status_code, 401)
                bearer = await client.get("/api/processes", headers={"Authorization": "Bearer legacy-token-must-not-authenticate"})
                self.assertEqual(bearer.status_code, 401)
                bad = await client.post("/api/auth/login", json={"email": "a@example.test", "password": "incorreta"})
                self.assertEqual(bad.status_code, 401)
                login = await client.post("/api/auth/login", json={"email": "a@example.test", "password": "senha-super-segura-a-123"})
                self.assertEqual(login.status_code, 200)
                self.assertIn("httponly", login.headers["set-cookie"].lower())
                self.assertNotIn("secure", login.headers["set-cookie"].lower())
                self.assertEqual([item["id"] for item in (await client.get("/api/auth/me")).json()["offices"]], ["office-a"])
                rows = await client.get("/api/processes")
                self.assertEqual([item["number"] for item in rows.json()], [NUMBER_A])
                csrf = await client.post("/api/processes", json={"number": NUMBER_B})
                self.assertEqual(csrf.status_code, 403)
                self.assertEqual((await client.get(f"/api/processos/{NUMBER_B}")).status_code, 404)
                self.assertEqual((await client.get(f"/api/processos/{NUMBER_A}")).status_code, 200)
                self.assertEqual((await client.post("/api/auth/logout", headers={"Origin": "http://test"})).status_code, 200)
                self.assertEqual((await client.get("/api/processes")).status_code, 401)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as other:
                await other.post("/api/auth/login", json={"email": "b@example.test", "password": "senha-super-segura-b-123"})
                self.assertEqual([item["number"] for item in (await other.get("/api/processes")).json()], [NUMBER_B])
                self.assertEqual((await other.get(f"/api/processos/{NUMBER_A}/movimentacoes")).status_code, 404)
                self.assertEqual((await other.get(f"/api/processos/{NUMBER_B}/movimentacoes")).status_code, 200)
            audit = self.repo.db.execute("SELECT DISTINCT org FROM access_log WHERE action='process_details'").fetchall()
            self.assertEqual({row[0] for row in audit}, {"office-a"})
        asyncio.run(scenario())

    def test_office_switch_requires_membership(self):
        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                await client.post("/api/auth/login", json={"email": "a@example.test", "password": "senha-super-segura-a-123"})
                denied = await client.post("/api/auth/office", json={"office_id": "office-b"}, headers={"Origin": "http://test"})
                self.assertEqual(denied.status_code, 403)
                self.assertEqual([item["number"] for item in (await client.get("/api/processes")).json()], [NUMBER_A])
        asyncio.run(scenario())

    def test_web_page_has_account_login_office_switch_and_logout_controls(self):
        from pathlib import Path
        web = Path(__file__).parents[1] / "src/processwatch/web"
        html = (web / "index.html").read_text(encoding="utf-8") + (web / "app.js").read_text(encoding="utf-8")
        for marker in ('id="account-login"', 'id="login-email"', 'id="login-password"',
                       'id="office-select"', 'id="logout"', "'/api/auth/me'", "'/api/auth/office'"):
            self.assertIn(marker, html)

    def test_worker_schedule_keeps_each_office_id(self):
        from datetime import datetime, timedelta, timezone
        due = datetime.now(timezone.utc) + timedelta(hours=24)
        self.repo.configure_monitor_schedule("office-a", NUMBER_A, enabled=True, interval_hours=6)
        self.repo.configure_monitor_schedule("office-b", NUMBER_B, enabled=True, interval_hours=12)
        scheduled = self.repo.enqueue_due_monitor_schedules(None, now_at=due, limit=10)
        self.assertEqual({item["number"] for item in scheduled}, {NUMBER_A, NUMBER_B})
        jobs = self.repo.db.execute("SELECT DISTINCT org FROM collection_jobs WHERE collection_type='scheduled_refresh'").fetchall()
        self.assertEqual({row[0] for row in jobs}, {"office-a", "office-b"})
        self.repo.db.execute("UPDATE collection_jobs SET scheduled_at=? WHERE status='queued'", (now(),))
        self.repo.db.commit()
        claimed = self.repo.claim_collection_job(None, "tenant-worker")
        self.assertIn(claimed["org"], {"office-a", "office-b"})
        self.assertEqual(self.repo.get(claimed["org"], claimed["number"])["number"], claimed["number"])

    def test_schema_v12_and_migration_preserve_legacy_processes(self):
        self.repo.close()
        self.repo = None
        conn = __import__("sqlite3").connect(self.db)
        try:
            before = conn.execute("SELECT count(*) FROM watches").fetchone()[0]
            conn.execute("DROP TABLE auth_sessions")
            conn.execute("DROP TABLE office_memberships")
            conn.execute("DROP TABLE users")
            conn.execute("DROP TABLE offices")
            conn.execute("PRAGMA user_version=10")
            conn.commit()
        finally:
            conn.close()
        migrated = SQLiteRepository(self.db)
        try:
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(migrated.db.execute("SELECT count(*) FROM watches").fetchone()[0], before)
            self.assertEqual(migrated.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(migrated.db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIn("office-a", {item["id"] for item in migrated.list_offices()})
        finally:
            migrated.close()
        backups = list(Path(self.temp.name).glob("*.pre-v18-*.backup.sqlite3"))
        self.assertEqual(len(backups), 1)


if __name__ == "__main__":
    unittest.main()

