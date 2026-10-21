import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.repository import SQLiteRepository


class NoopProvider:
    def fetch(self, number, court):
        return {"timed_out": False, "hits": {"total": {"value": 0}, "hits": []}}


class AccountManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "accounts.sqlite3")
        self.repo = SQLiteRepository(self.path)
        self.repo.create_office("office-a", "Escritório A")
        self.repo.create_office("office-b", "Escritório B")
        self.admin = self.repo.create_user("admin@example.test", "admin-password-123", "office-a", "office_admin", "Admin A")
        self.other_admin = self.repo.create_user("admin-b@example.test", "admin-password-456", "office-b", "office_admin", "Admin B")
        self.lawyer = self.repo.create_user("lawyer@example.test", "lawyer-password-123", "office-a", "lawyer", "Dra. Ana")
        self.app = create_app(self.repo, Engine(self.repo, NoopProvider()),
                              config=Config(db=self.path, auth_mode="accounts", access_token="legacy"))

    def tearDown(self):
        if self.repo:
            self.repo.close()
        self.tmp.cleanup()

    async def client_for(self, email, password):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        result = await client.post("/api/auth/login", json={"email": email, "password": password})
        self.assertEqual(result.status_code, 200)
        client.headers["Origin"] = "http://test"
        return client

    def test_admin_create_list_update_deactivate_and_remove_office_membership(self):
        async def run():
            admin = await self.client_for("admin@example.test", "admin-password-123")
            create = await admin.post("/api/admin/users", json={"email":"new@example.test", "full_name":"Joana Silva", "role":"lawyer", "temporary_password":"temporary-pass-123"})
            self.assertEqual(create.status_code, 201, create.text)
            uid = create.json()["id"]
            listing = await admin.get("/api/admin/users")
            self.assertEqual({u["email"] for u in listing.json()["items"]}, {"admin@example.test","lawyer@example.test","new@example.test"})
            updated = await admin.patch(f"/api/admin/users/{uid}", json={"role":"office_admin","active":False,"display_name":"Joana S."})
            self.assertEqual(updated.status_code, 200, updated.text)
            row = next(u for u in (await admin.get("/api/admin/users")).json()["items"] if u["id"] == uid)
            self.assertFalse(row["active"])
            self.assertEqual(row["role"], "office_admin")
            removed = await admin.delete(f"/api/admin/users/{uid}")
            self.assertEqual(removed.status_code, 200, removed.text)
            self.assertEqual((await admin.delete(f"/api/admin/users/{uid}")).status_code, 404)
            await admin.aclose()
        asyncio.run(run())

    def test_lawyer_cannot_administer_and_direct_ids_are_office_scoped(self):
        async def run():
            lawyer = await self.client_for("lawyer@example.test", "lawyer-password-123")
            self.assertEqual((await lawyer.get("/api/admin/users")).status_code, 403)
            self.assertEqual((await lawyer.patch(f"/api/admin/users/{self.other_admin['id']}", json={"role":"lawyer","active":False,"display_name":"x"})).status_code, 403)
            admin = await self.client_for("admin@example.test", "admin-password-123")
            self.assertEqual((await admin.patch(f"/api/admin/users/{self.other_admin['id']}", json={"role":"lawyer","active":False,"display_name":"x"})).status_code, 404)
            self.assertEqual((await admin.delete(f"/api/admin/users/{self.other_admin['id']}")).status_code, 404)
            await lawyer.aclose(); await admin.aclose()
        asyncio.run(run())

    def test_last_office_admin_cannot_remove_or_demote_self(self):
        async def run():
            admin = await self.client_for("admin@example.test", "admin-password-123")
            self.assertEqual((await admin.patch(f"/api/admin/users/{self.admin['id']}", json={"role":"lawyer","active":True,"display_name":"Admin A"})).status_code, 409)
            self.assertEqual((await admin.delete(f"/api/admin/users/{self.admin['id']}")).status_code, 409)
            await admin.aclose()
        asyncio.run(run())

    def test_change_password_requires_current_and_revokes_sessions(self):
        async def run():
            first = await self.client_for("lawyer@example.test", "lawyer-password-123")
            second = await self.client_for("lawyer@example.test", "lawyer-password-123")
            self.assertEqual((await first.post("/api/profile/password", json={"current_password":"wrong-password","new_password":"new-secure-password-123"})).status_code, 400)
            changed = await first.post("/api/profile/password", json={"current_password":"lawyer-password-123","new_password":"new-secure-password-123"})
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual((await second.get("/api/processes")).status_code, 401)
            self.assertIsNone(self.repo.authenticate_user("lawyer@example.test", "lawyer-password-123"))
            self.assertIsNotNone(self.repo.authenticate_user("lawyer@example.test", "new-secure-password-123"))
            await first.aclose(); await second.aclose()
        asyncio.run(run())

    def test_recovery_is_single_use_expiring_and_revokes_sessions(self):
        self.repo.add_user_to_office(self.lawyer["id"], "office-b", role="lawyer")
        shared_session = "shared-office-session-before-recovery"
        self.repo.create_auth_session(self.lawyer["id"], shared_session, "office-b",
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        async def run():
            active = await self.client_for("lawyer@example.test", "lawyer-password-123")
            req = await active.post("/api/auth/recovery/request", json={"email":"unknown@example.test"})
            self.assertEqual(req.status_code, 202)
            self.assertIn("administrador", req.json()["message"])
            known_req = await active.post("/api/auth/recovery/request", json={"email":"lawyer@example.test"})
            self.assertEqual(known_req.status_code, req.status_code)
            self.assertEqual(known_req.json(), req.json())
            self.assertEqual(self.repo.db.execute("SELECT count(*) FROM password_recovery_tokens").fetchone()[0], 0)
            token = self.repo.create_password_recovery_token(self.lawyer["id"], ttl_minutes=30)
            stored_hash = self.repo.db.execute("SELECT token_hash FROM password_recovery_tokens ORDER BY created_at DESC LIMIT 1").fetchone()[0]
            self.assertNotEqual(stored_hash, token)
            done = await active.post("/api/auth/recovery/complete", json={"email":"lawyer@example.test","token":token,"new_password":"recovered-password-123"})
            self.assertEqual(done.status_code, 200, done.text)
            self.assertEqual((await active.get("/api/processes")).status_code, 401)
            self.assertIsNone(self.repo.auth_session(shared_session))
            again = await active.post("/api/auth/recovery/complete", json={"email":"lawyer@example.test","token":token,"new_password":"another-password-123"})
            self.assertEqual(again.status_code, 400)
            expired = self.repo.create_password_recovery_token(self.lawyer["id"], ttl_minutes=30)
            self.repo.db.execute("UPDATE password_recovery_tokens SET expires_at=? WHERE token_hash=(SELECT token_hash FROM password_recovery_tokens ORDER BY created_at DESC LIMIT 1)", ((datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),))
            self.repo.db.commit()
            self.assertEqual((await active.post("/api/auth/recovery/complete", json={"email":"lawyer@example.test","token":expired,"new_password":"expired-password-123"})).status_code,400)
            await active.aclose()
        asyncio.run(run())

    def test_office_admin_can_issue_one_time_recovery_token_only_for_active_member(self):
        target = self.repo.create_user("target-recovery@example.test", "target-password-old-123",
            "office-a", role="lawyer", full_name="Target")
        async def run():
            admin = await self.client_for("admin@example.test", "admin-password-123")
            issued = await admin.post(f"/api/admin/users/{target['id']}/recovery")
            self.assertEqual(issued.status_code, 201, issued.text)
            token = issued.json()["token"]
            self.assertIn("canal privado", issued.json()["message"])
            stored = self.repo.db.execute("SELECT token_hash FROM password_recovery_tokens WHERE user_id=?",
                                          (target["id"],)).fetchone()[0]
            self.assertNotEqual(stored, token)
            other_office_member = self.other_admin["id"]
            denied = await admin.post(f"/api/admin/users/{other_office_member}/recovery")
            self.assertEqual(denied.status_code, 404)
            completed = await admin.post("/api/auth/recovery/complete", json={
                "email": "target-recovery@example.test", "token": token,
                "new_password": "target-password-new-456"})
            self.assertEqual(completed.status_code, 200, completed.text)
            self.assertIsNotNone(self.repo.authenticate_user(
                "target-recovery@example.test", "target-password-new-456"))
            await admin.aclose()
        asyncio.run(run())

    def test_login_attempts_are_rate_limited(self):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                for _ in range(10):
                    response = await client.post("/api/auth/login", json={"email":"missing@example.test","password":"wrong"})
                    self.assertEqual(response.status_code, 401)
                limited = await client.post("/api/auth/login", json={"email":"missing@example.test","password":"wrong"})
                self.assertEqual(limited.status_code, 429)
        asyncio.run(run())

    def test_existing_account_can_be_linked_without_cross_office_listing(self):
        async def run():
            admin_b = await self.client_for("admin-b@example.test", "admin-password-456")
            linked = await admin_b.post("/api/admin/users", json={"email":"lawyer@example.test","full_name":"Dra. Ana B","role":"lawyer"})
            self.assertEqual(linked.status_code, 201, linked.text)
            self.assertFalse(linked.json()["created"])
            self.assertEqual({u["email"] for u in (await admin_b.get("/api/admin/users")).json()["items"]},
                             {"admin-b@example.test","lawyer@example.test"})
            admin_a = await self.client_for("admin@example.test", "admin-password-123")
            users_a = (await admin_a.get("/api/admin/users")).json()["items"]
            self.assertEqual(next(u for u in users_a if u["email"]=="lawyer@example.test")["display_name"], "Dra. Ana")
            await admin_a.aclose(); await admin_b.aclose()
        asyncio.run(run())

    def test_csrf_and_inactivity_expiration(self):
        async def run():
            client = await self.client_for("lawyer@example.test", "lawyer-password-123")
            denied = await client.put("/api/profile", json={"display_name":"Ana"}, headers={"Origin":"https://attacker.test"})
            self.assertEqual(denied.status_code, 403)
            session_token = client.cookies.get("processwatch_session")
            digest = __import__('hashlib').sha256(session_token.encode()).hexdigest()
            old = (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
            self.repo.db.execute("UPDATE auth_sessions SET last_seen_at=? WHERE token_hash=?", (old,digest)); self.repo.db.commit()
            self.assertEqual((await client.get("/api/profile")).status_code,401)
            await client.aclose()
        asyncio.run(run())

    def test_production_accounts_require_secure_cookie_and_https_origin(self):
        with self.assertRaises(ValueError):
            Config(auth_mode="accounts", production=True, session_cookie_secure=False, public_origin="https://watch.example")
        with self.assertRaises(ValueError):
            Config(auth_mode="accounts", production=True, session_cookie_secure=True, public_origin="http://watch.example")
        Config(auth_mode="accounts", production=True, session_cookie_secure=True, public_origin="https://watch.example")

    def test_v11_migration_adds_management_and_recovery_columns_with_backup(self):
        self.repo.close()
        self.repo = None
        conn = __import__("sqlite3").connect(self.path)
        try:
            conn.executescript("""
                DROP TABLE password_recovery_tokens;
                DROP TABLE auth_sessions;
                DROP TABLE office_memberships;
                DROP TABLE users;
                CREATE TABLE users(id INTEGER PRIMARY KEY,email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,password_salt TEXT NOT NULL,password_iterations INTEGER NOT NULL,
                    created_at TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE office_memberships(office_id TEXT NOT NULL,user_id INTEGER NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('office_admin','lawyer')),created_at TEXT NOT NULL,
                    PRIMARY KEY(office_id,user_id),FOREIGN KEY(office_id) REFERENCES offices(id),
                    FOREIGN KEY(user_id) REFERENCES users(id));
                INSERT INTO users(id,email,password_hash,password_salt,password_iterations,created_at)
                    VALUES(501,'legacy-admin@example.test','hash','salt',310000,'2026-01-01T00:00:00+00:00');
                INSERT INTO office_memberships(office_id,user_id,role,created_at)
                    VALUES('office-a',501,'office_admin','2026-01-01T00:00:00+00:00');
                PRAGMA user_version=11;
            """)
            conn.commit()
        finally:
            conn.close()
        migrated = SQLiteRepository(self.path)
        try:
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(migrated.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(migrated.db.execute("PRAGMA foreign_key_check").fetchall(), [])
            users = migrated.office_users("office-a")
            self.assertEqual(len(users), 1)
            self.assertEqual(users[0]["email"], "legacy-admin@example.test")
            self.assertEqual(users[0]["role"], "office_admin")
            self.assertTrue(users[0]["active"])
        finally:
            migrated.close()
        backup = next(Path(self.tmp.name).glob("accounts.sqlite3.pre-v18-*.backup.sqlite3"))
        bc = __import__("sqlite3").connect(f"file:{backup}?mode=ro", uri=True)
        try:
            self.assertEqual(bc.execute("PRAGMA user_version").fetchone()[0], 11)
            self.assertEqual(bc.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(bc.execute("SELECT count(*) FROM users").fetchone()[0], 1)
        finally:
            bc.close()

    def test_account_management_controls_are_present_in_existing_ui(self):
        web = Path(__file__).parents[1] / "src/processwatch/web"
        html = (web / "index.html").read_text(encoding="utf-8") + (web / "app.js").read_text(encoding="utf-8")
        for marker in ('id="users-toggle"', 'id="users-panel"', 'id="user-add"',
                       'id="profile-panel"', 'id="password-save"', 'id="recovery-panel"',
                       '/api/admin/users', '/api/auth/recovery/complete'):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()
