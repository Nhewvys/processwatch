"""Account and office administration against isolated SQLite and the real ASGI API."""
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


class AccountLifecycleTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parents[1] / ".test-temp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.repo = SQLiteRepository(str(Path(self.temp.name) / "accounts.sqlite3"), environment="test")
        self.repo.create_office("office-a", "A")
        self.repo.create_office("office-b", "B")
        self.admin = self.repo.create_user("admin-a@example.test", "admin-password-123", "office-a", "office_admin", "Admin A")
        self.admin_b = self.repo.create_user("admin-b@example.test", "admin-password-456", "office-b", "office_admin", "Admin B")
        self.lawyer = self.repo.create_user("lawyer@example.test", "lawyer-password-123", "office-a", "lawyer", "Dra. Ana")
        self.app = create_app(self.repo, Engine(self.repo, NoopProvider()),
                              config=Config(db=str(Path(self.temp.name) / "accounts.sqlite3"),
                                            environment="test", auth_mode="accounts"))

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    async def client(self, email, password):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        response = await client.post("/api/auth/login", json={"email": email, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        client.headers["Origin"] = "http://test"
        return client

    def test_partial_patch_preserves_name_role_and_active_state(self):
        async def scenario():
            admin = await self.client("admin-a@example.test", "admin-password-123")
            path = f"/api/admin/users/{self.lawyer['id']}"
            self.assertEqual((await admin.patch(path, json={"active": False})).status_code, 200)
            row = (await admin.get(path)).json()
            self.assertEqual((row["display_name"], row["role"], row["active"]), ("Dra. Ana", "lawyer", 0))
            self.assertEqual((await admin.patch(path, json={"active": True})).status_code, 200)
            self.assertEqual((await admin.patch(path, json={"display_name": "Ana Silva"})).status_code, 200)
            self.assertEqual((await admin.patch(path, json={"role": "office_admin"})).status_code, 200)
            row = (await admin.get(path)).json()
            self.assertEqual((row["display_name"], row["role"], row["active"]), ("Ana Silva", "office_admin", 1))
            self.assertEqual((await admin.patch(path, json={"display_name": ""})).status_code, 422)
            await admin.aclose()
        asyncio.run(scenario())

    def test_legacy_empty_membership_name_uses_existing_account_name(self):
        self.repo.db.execute("UPDATE office_memberships SET display_name='' WHERE office_id=? AND user_id=?",
                             ("office-a", self.lawyer["id"]))
        self.repo.db.commit()
        async def scenario():
            admin = await self.client("admin-a@example.test", "admin-password-123")
            path = f"/api/admin/users/{self.lawyer['id']}"
            self.assertEqual((await admin.patch(path, json={"active": False})).status_code, 200)
            self.assertEqual((await admin.get(path)).json()["display_name"], "Dra. Ana")
            await admin.aclose()
        asyncio.run(scenario())

    def test_openapi_describes_optional_patch_fields(self):
        schema = self.app.openapi()
        patch = schema["paths"]["/api/admin/users/{user_id}"]["patch"]
        model_ref = patch["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        model = schema["components"]["schemas"][model_ref.rsplit("/", 1)[-1]]
        self.assertEqual(set(model["properties"]), {"role", "active", "display_name"})
        self.assertNotIn("required", model)

    def test_user_delete_revokes_session_and_removes_orphan_account(self):
        metadata, token = self.repo.create_service_credential("office-a", "exemplo", ["processes:read"],
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), created_by="lawyer@example.test")
        async def scenario():
            admin = await self.client("admin-a@example.test", "admin-password-123")
            lawyer = await self.client("lawyer@example.test", "lawyer-password-123")
            self.assertEqual((await admin.delete(f"/api/admin/users/{self.lawyer['id']}")).status_code, 200)
            self.assertEqual((await lawyer.get("/api/profile")).status_code, 401)
            self.assertIsNone(self.repo.find_user("lawyer@example.test"))
            self.assertIsNone(self.repo.authenticate_service_credential(token))
            await admin.aclose(); await lawyer.aclose()
        asyncio.run(scenario())

    def test_admin_demotion_revokes_related_service_credential(self):
        second = self.repo.create_user("second@example.test", "second-password-123", "office-a", "office_admin", "Segunda")
        _, token = self.repo.create_service_credential("office-a", "exemplo", ["processes:read"],
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), created_by="second@example.test")
        async def scenario():
            first = await self.client("admin-a@example.test", "admin-password-123")
            second_client = await self.client("second@example.test", "second-password-123")
            self.assertEqual((await first.patch(f"/api/admin/users/{second['id']}", json={"role": "lawyer"})).status_code, 200)
            self.assertEqual((await second_client.get("/api/profile")).status_code, 401)
            self.assertIsNone(self.repo.authenticate_service_credential(token))
            await first.aclose(); await second_client.aclose()
        asyncio.run(scenario())

    def test_user_delete_only_removes_current_office_when_shared(self):
        self.repo.add_user_to_office(self.lawyer["id"], "office-b")
        async def scenario():
            admin = await self.client("admin-a@example.test", "admin-password-123")
            self.assertEqual((await admin.get(f"/api/admin/users/{self.admin_b['id']}")).status_code, 404)
            self.assertEqual((await admin.delete(f"/api/admin/users/{self.admin_b['id']}")).status_code, 404)
            self.assertEqual((await admin.delete(f"/api/admin/users/{self.lawyer['id']}")).status_code, 200)
            self.assertIsNotNone(self.repo.find_user("lawyer@example.test"))
            self.assertIsNotNone(self.repo.office_user("office-b", self.lawyer["id"]))
            await admin.aclose()
        asyncio.run(scenario())

    def test_self_delete_requires_password_and_protects_last_admin(self):
        async def scenario():
            admin = await self.client("admin-a@example.test", "admin-password-123")
            self.assertEqual((await admin.request("DELETE", "/api/profile", json={"current_password": "admin-password-123"})).status_code, 409)
            lawyer = await self.client("lawyer@example.test", "lawyer-password-123")
            self.assertEqual((await lawyer.request("DELETE", "/api/profile", json={"current_password": "wrong"})).status_code, 400)
            self.assertEqual((await lawyer.request("DELETE", "/api/profile", json={"current_password": "lawyer-password-123"})).status_code, 200)
            self.assertEqual((await lawyer.get("/api/profile")).status_code, 401)
            self.assertIsNone(self.repo.find_user("lawyer@example.test"))
            await admin.aclose(); await lawyer.aclose()
        asyncio.run(scenario())

    def test_admin_may_delete_own_account_when_another_admin_remains(self):
        self.repo.create_user("second@example.test", "second-password-123", "office-a", "office_admin", "Segunda")
        async def scenario():
            admin = await self.client("admin-a@example.test", "admin-password-123")
            response = await admin.request("DELETE", "/api/profile", json={"current_password": "admin-password-123"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual((await admin.get("/api/profile")).status_code, 401)
            self.assertIsNone(self.repo.find_user("admin-a@example.test"))
            self.assertEqual(len([item for item in self.repo.office_users("office-a") if item["role"] == "office_admin"]), 1)
            await admin.aclose()
        asyncio.run(scenario())

    def test_office_deactivate_activate_and_delete_keep_other_office(self):
        metadata, service_token = self.repo.create_service_credential("office-a", "serviço", ["processes:read"],
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), created_by="admin-a@example.test")
        async def scenario():
            admin = await self.client("admin-a@example.test", "admin-password-123")
            other = await self.client("admin-b@example.test", "admin-password-456")
            self.assertEqual((await admin.post("/api/office/deactivate", json={"office_id": "office-b"})).status_code, 403)
            self.assertEqual((await admin.post("/api/office/deactivate", json={"office_id": "office-a"})).status_code, 200)
            self.assertEqual((await admin.get("/api/admin/users")).status_code, 401)
            self.assertEqual((await admin.get("/api/office")).status_code, 200)
            self.assertIsNone(self.repo.authenticate_service_credential(service_token))
            # Login remains possible for an office administrator solely to reactivate.
            renewed = await self.client("admin-a@example.test", "admin-password-123")
            self.assertEqual((await renewed.get("/api/admin/users")).status_code, 401)
            self.assertEqual((await admin.post("/api/office/activate", json={"office_id": "office-a"})).status_code, 200)
            self.assertIsNotNone(self.repo.authenticate_service_credential(service_token))
            self.assertEqual((await admin.get("/api/admin/users")).status_code, 200)
            self.assertEqual((await admin.request("DELETE", "/api/office", json={"office_id": "office-b"})).status_code, 403)
            self.repo.record_access("pre_delete_audit", None, "preservar histórico", actor="admin-a@example.test",
                                    org="office-a")
            office_a_audit_count = self.repo.db.execute(
                "SELECT count(*) FROM access_log WHERE org='office-a'").fetchone()[0]
            self.assertEqual((await admin.request("DELETE", "/api/office", json={"office_id": "office-a"})).status_code, 200)
            self.assertIsNone(self.repo.office("office-a"))
            self.assertEqual(self.repo.db.execute("SELECT count(*) FROM access_log WHERE org='office-a' ").fetchone()[0],
                             office_a_audit_count)
            deletion = self.repo.db.execute("SELECT actor,org,purpose FROM access_log WHERE action='office_delete'").fetchone()
            self.assertEqual(deletion["actor"], "admin-a@example.test")
            self.assertIsNone(deletion["org"])
            self.assertIn("office-a", deletion["purpose"])
            self.assertIsNotNone(self.repo.office("office-b"))
            self.assertEqual((await other.get("/api/admin/users")).status_code, 200)
            self.assertEqual(self.repo.db.execute("PRAGMA foreign_key_check").fetchall(), [])
            await admin.aclose(); await other.aclose(); await renewed.aclose()
        asyncio.run(scenario())

    def test_office_delete_cleans_scoped_process_and_communication_rows(self):
        engine = Engine(self.repo, NoopProvider())
        engine.register("00000003800004010000", "office-a")
        engine.register("10002365820254053305", "office-b")
        self.repo._upsert_inbox_communication("office-a", "00000003800004010000", "publication-a", {"text": "A"})
        self.repo.record_access("before_delete", None, "historical audit record", actor="admin-a@example.test",
                                org="office-a")
        self.repo.db.commit()
        self.repo.delete_office("office-a")
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM watches WHERE org='office-a'").fetchone()[0], 0)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM communication_inbox WHERE org='office-a'").fetchone()[0], 0)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM watches WHERE org='office-b'").fetchone()[0], 1)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM access_log WHERE org='office-a' ").fetchone()[0], 1)
        self.assertEqual(self.repo.db.execute("SELECT action FROM access_log WHERE org='office-a'").fetchone()[0],
                         "before_delete")
        self.assertEqual(self.repo.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_inactive_office_does_not_enqueue_oab_jobs(self):
        monitor = self.repo.create_oab_monitor("office-a", "12345", "SP")
        self.repo.set_office_active("office-a", False)
        self.assertEqual(self.repo.enqueue_due_oab_monitor_jobs("office-a"), [])
        self.assertIsNone(self.repo.claim_oab_monitor_job("office-a"))
        self.repo.set_office_active("office-a", True)
        self.assertEqual(len(self.repo.enqueue_due_oab_monitor_jobs("office-a")), 1)
        self.assertEqual(self.repo.claim_oab_monitor_job("office-a")["monitor_id"], monitor["id"])
        with self.assertRaises(ValueError):
            self.repo.set_office_active("office-a", False)
        with self.assertRaises(ValueError):
            self.repo.delete_office("office-a")


if __name__ == "__main__":
    unittest.main()
