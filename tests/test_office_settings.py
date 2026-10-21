import asyncio
import runpy
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.repository import SQLiteRepository


class NoopProvider:
    def fetch(self, number, court):
        return {"timed_out": False, "hits": {"total": {"value": 0}, "hits": []}}


class OfficeSettingsTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).parents[1] / ".test-temp"
        root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)
        self.path = str(Path(self.temp.name) / "offices.sqlite3")
        self.repo = SQLiteRepository(self.path)
        self.repo.create_office("office-a", "Escritório A")
        self.repo.create_office("office-b", "Escritório B")
        self.repo.create_user("admin-a@example.test", "safe-admin-password-123", "office-a", "office_admin")
        self.repo.create_user("admin-b@example.test", "safe-admin-password-456", "office-b", "office_admin")
        self.repo.create_user("lawyer@example.test", "safe-lawyer-password-123", "office-a", "lawyer")
        self.app = create_app(self.repo, Engine(self.repo, NoopProvider()),
                              config=Config(db=self.path, auth_mode="accounts"))

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def test_office_update_persists_and_is_scoped(self):
        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as admin_a, \
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as admin_b, \
                    httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as lawyer:
                for client, email, password in (
                    (admin_a, "admin-a@example.test", "safe-admin-password-123"),
                    (admin_b, "admin-b@example.test", "safe-admin-password-456"),
                    (lawyer, "lawyer@example.test", "safe-lawyer-password-123"),
                ):
                    self.assertEqual((await client.post("/api/auth/login", json={
                        "email": email, "password": password})).status_code, 200)
                payload = {"name": "Escritório A atualizado", "email": "contato@example.test",
                           "phone": "+55 (11) 3333-4444", "address": "Rua Exemplo, 123"}
                self.assertEqual((await admin_a.put("/api/office", json=payload)).status_code, 403)
                saved = await admin_a.put("/api/office", json=payload, headers={"Origin": "http://test"})
                self.assertEqual(saved.status_code, 200, saved.text)
                self.assertEqual(saved.json()["email"], payload["email"])
                self.assertEqual((await admin_a.get("/api/office")).json()["name"], payload["name"])
                self.assertEqual((await admin_b.get("/api/office")).json()["name"], "Escritório B")
                self.assertEqual((await lawyer.get("/api/office")).status_code, 200)
                self.assertEqual((await lawyer.put("/api/office", json=payload,
                    headers={"Origin": "http://test"})).status_code, 403)
                bad = await admin_a.put("/api/office", json={**payload, "email": "invalid"},
                                        headers={"Origin": "http://test"})
                self.assertEqual(bad.status_code, 400)
                self.assertEqual((await admin_a.get("/api/office")).json()["email"], payload["email"])
                self.assertEqual((await admin_a.get("/api/admin/users")).status_code, 200)
                self.assertEqual((await lawyer.get("/api/admin/users")).status_code, 403)
                self.assertEqual((await lawyer.get("/api/service-credentials")).status_code, 403)
                self.assertEqual((await admin_b.get("/api/service-credentials")).json()["items"], [])
        asyncio.run(scenario())
        self.repo.close()
        self.repo = SQLiteRepository(self.path)
        self.assertEqual(self.repo.office("office-a")["address"], "Rua Exemplo, 123")
        self.assertEqual(self.repo.office("office-b")["email"], "")

    def test_migration_adds_contact_columns_without_losing_office_or_users(self):
        self.repo.close()
        connection = sqlite3.connect(self.path)
        for column in ("email", "phone", "address"):
            connection.execute(f"ALTER TABLE offices DROP COLUMN {column}")
        connection.execute("PRAGMA user_version=15")
        connection.commit()
        connection.close()
        self.repo = SQLiteRepository(self.path)
        self.assertEqual(self.repo.db.execute("PRAGMA user_version").fetchone()[0], 18)
        self.assertEqual(self.repo.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(len(self.repo.office_users("office-a")), 2)
        self.assertEqual(self.repo.office("office-a")["name"], "Escritório A")
        self.assertEqual(self.repo.office("office-a")["email"], "")
        self.assertEqual(len(list(Path(self.temp.name).glob("*.pre-v18-*.backup.sqlite3"))), 1)

    def test_first_administrator_script_creates_login_without_default_password(self):
        script_db = str(Path(self.temp.name) / "first-admin.sqlite3")
        script = Path(__file__).parents[1] / "scripts" / "create_admin.py"
        argv = [str(script), "--db", script_db, "--office-id", "new-office",
                "--office-name", "Novo Escritório", "--email", "first@example.test"]
        with patch.object(sys, "argv", argv), patch("getpass.getpass", side_effect=[
                "private-admin-password-123", "private-admin-password-123"]):
            runpy.run_path(str(script), run_name="__main__")
        created = SQLiteRepository(script_db)
        try:
            user = created.authenticate_user("first@example.test", "private-admin-password-123")
            self.assertEqual(user["offices"][0]["role"], "office_admin")
            self.assertEqual(user["offices"][0]["name"], "Novo Escritório")
            self.assertIsNone(created.authenticate_user("first@example.test", "wrong-password"))
        finally:
            created.close()

    def test_legacy_token_mode_can_update_only_its_configured_office(self):
        app = create_app(self.repo, Engine(self.repo, NoopProvider()), org="office-a",
                         config=Config(db=self.path, auth_mode="token", access_token="legacy-test-token"))

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                self.assertEqual((await client.get("/api/office")).status_code, 401)
                headers = {"Authorization": "Bearer legacy-test-token"}
                self.assertEqual((await client.get("/api/office", headers=headers)).json()["id"], "office-a")
                response = await client.put("/api/office", headers=headers, json={
                    "name": "A em modo local", "email": "", "phone": "", "address": ""})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.repo.office("office-b")["name"], "Escritório B")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
