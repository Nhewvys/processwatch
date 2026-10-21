import asyncio
import contextlib
import hashlib
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from unittest.mock import patch

from processwatch.api import create_app
from processwatch import api_cli
from processwatch.cli import main as cli_main
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.repository import SQLiteRepository
from processwatch.error_sanitizer import sanitize_error
from scripts.issue_password_recovery import issue_password_recovery_token


class NoopProvider:
    def fetch(self, number, court):
        return {"timed_out": False, "hits": {"total": {"value": 0}, "hits": []}}


class PlatformAdminTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".test-temp")
        self.db_path = str(Path(self.temp.name) / "platform.sqlite3")
        self.repo = SQLiteRepository(self.db_path, environment="test")
        self.repo.create_office("office-a", "Escritório A")
        self.repo.create_office("office-b", "Escritório B")
        self.repo.create_user("admin-a@example.test", "strong-office-password-123", "office-a",
                              role="office_admin", full_name="Admin A")
        self.repo.create_user("admin-b@example.test", "strong-office-password-456", "office-b",
                              role="office_admin", full_name="Admin B")
        self.repo.db.execute("INSERT INTO watches(org,number,court,interval,created_at,next_due) VALUES(?,?,?,?,?,?)",
                             ("local", "00000003800004010000", "trf1", 86400, "2026-01-01", "2026-01-02"))
        self.repo.db.commit()
        self.repo.bootstrap_platform_admin("platform@example.test", "strong-platform-password-123")
        self.app = create_app(repository=self.repo, engine=Engine(self.repo, NoopProvider()),
                              config=Config(db=self.db_path, environment="test", auth_mode="accounts"))

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def client(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")

    def test_global_routes_reject_anonymous_office_users_and_office_service_keys(self):
        async def scenario():
            async with self.client() as client:
                self.assertEqual((await client.get("/api/platform/offices")).status_code, 401)
                self.assertEqual((await client.get("/api/platform/processes")).status_code, 401)
                self.assertEqual((await client.get("/api/platform/jobs")).status_code, 401)
                self.assertEqual((await client.get("/api/platform/jobs/collection/1")).status_code, 401)
                self.assertEqual((await client.post("/api/platform/jobs/collection/1/retry",
                    json={"purpose": "tentativa sem sessão global"},
                    headers={"Origin": "http://test"})).status_code, 401)
                self.assertEqual((await client.get("/api/platform/processes/local/00000003800004010000")).status_code, 401)
                self.assertEqual((await client.get("/api/platform/offices/office-a/users")).status_code, 401)
                self.assertEqual((await client.post("/api/platform/offices/office-a/users", json={})).status_code, 401)
                self.assertEqual((await client.patch("/api/platform/offices/office-a", json={"name": "Sem acesso"},
                    headers={"Origin": "http://test"})).status_code, 401)
                self.assertEqual((await client.patch("/api/platform/offices/office-a/users/1",
                    json={"active": False}, headers={"Origin": "http://test"})).status_code, 401)
                self.assertEqual((await client.delete("/api/platform/offices/office-a/users/1",
                    headers={"Origin": "http://test"})).status_code, 401)
                self.assertEqual((await client.patch("/api/platform/offices/office-a", json={"name": "Sem acesso"},
                    headers={"Origin": "http://test"})).status_code, 401)
                await client.post("/api/auth/login", json={"email": "admin-a@example.test",
                                   "password": "strong-office-password-123"})
                self.assertEqual((await client.get("/api/platform/offices")).status_code, 401)
                self.assertEqual((await client.get("/api/platform/processes")).status_code, 401)
                self.assertEqual((await client.get("/api/platform/processes/local/00000003800004010000")).status_code, 401)
                self.assertEqual((await client.get("/api/platform/offices/office-a/users")).status_code, 401)
                self.assertEqual((await client.patch("/api/platform/offices/office-a/users/1",
                    json={"active": False}, headers={"Origin": "http://test"})).status_code, 401)
                self.assertEqual((await client.delete("/api/platform/offices/office-a/users/1",
                    headers={"Origin": "http://test"})).status_code, 401)
                metadata, secret = self.repo.create_service_credential("office-a", "reader", ["processes:read"],
                    (datetime.now(timezone.utc) + timedelta(days=10)).isoformat())
                denied = await client.get("/api/platform/offices",
                    headers={"Authorization": f"Bearer {secret}"})
                self.assertIn(denied.status_code, (401, 403))
                self.assertNotIn(secret, denied.text)
                self.assertEqual((await client.get("/api/platform/processes",
                    headers={"Authorization": f"Bearer {secret}"})).status_code, 401)
                self.assertEqual((await client.get("/api/platform/processes/local/00000003800004010000",
                    headers={"Authorization": "Bearer legacy-office-token"})).status_code, 401)
                self.assertEqual((await client.get("/api/platform/offices/office-a/users",
                    headers={"Authorization": f"Bearer {secret}"})).status_code, 401)
                self.assertEqual((await client.patch("/api/platform/offices/office-a", json={"name": "Sem acesso"},
                    headers={"Authorization": f"Bearer {secret}", "Origin": "http://test"})).status_code, 401)
        asyncio.run(scenario())

    def test_platform_admin_can_list_global_data_and_audits_access(self):
        async def scenario():
            async with self.client() as client:
                login = await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                self.assertEqual(login.status_code, 200, login.text)
                self.assertIn("processwatch_platform_session", client.cookies)
                offices = await client.get("/api/platform/offices")
                self.assertEqual(offices.status_code, 200, offices.text)
                self.assertEqual({row["id"] for row in offices.json()["items"]}, {"local", "office-a", "office-b"})
                processes = await client.get("/api/platform/processes", params={
                    "q": "00000003800004010000"})
                self.assertEqual(processes.status_code, 200, processes.text)
                self.assertEqual(processes.json()["total"], 1)
                details = await client.get("/api/platform/processes/local/00000003800004010000")
                self.assertEqual(details.status_code, 200, details.text)
                self.assertEqual(details.json()["process"]["number"], "00000003800004010000")
                self.assertIn("movements", details.json())
                rows = self.repo.db.execute("SELECT action,actor,org,number,purpose FROM access_log "
                    "WHERE action IN ('platform_processes_search','platform_process_detail') ORDER BY id").fetchall()
                self.assertEqual([row["action"] for row in rows],
                                 ["platform_processes_search", "platform_process_detail"])
                self.assertEqual(rows[0]["actor"], "platform_admin:platform@example.test")
                self.assertIsNone(rows[0]["org"])
                self.assertIn("consulta global", rows[0]["purpose"])
                self.assertEqual((rows[1]["actor"], rows[1]["org"], rows[1]["number"]),
                                 ("platform_admin:platform@example.test", "local", "00000003800004010000"))
                self.assertIn("histórico", rows[1]["purpose"])
        asyncio.run(scenario())

    def test_expired_platform_session_cannot_read_global_processes(self):
        self.repo.create_platform_admin_session(1, "expired-platform-session",
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        async def scenario():
            async with self.client() as client:
                client.cookies.set("processwatch_platform_session", "expired-platform-session")
                self.assertEqual((await client.get("/api/platform/processes")).status_code, 401)
                self.assertEqual((await client.get("/api/platform/processes/local/00000003800004010000")).status_code, 401)
        asyncio.run(scenario())

    def test_platform_cli_process_search_works_without_purpose(self):
        calls = []
        def fake_request(url, method, path, **kwargs):
            calls.append((url, method, path, kwargs))
            return ({"items": [], "total": 0}, {})
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
             patch.object(api_cli, "read_url", return_value="http://127.0.0.1:8000"), \
             patch.object(api_cli, "_session", return_value="session-token"), \
             patch.object(api_cli, "request", side_effect=fake_request):
            self.assertEqual(cli_main(["platform-admin", "processes", "--query", "12345"]), 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], "/api/platform/processes?q=12345")
        self.assertEqual(calls[0][3]["cookie_name"], "processwatch_platform_session")
        self.assertNotIn("purpose", calls[0][2])

    def test_platform_cli_user_link_management_uses_scoped_api_contracts(self):
        calls = []
        def fake_request(url, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return ({"items": []} if method == "GET" else {"status": "updated"}, {})
        with patch.object(api_cli, "read_url", return_value="http://127.0.0.1:8000"), \
             patch.object(api_cli, "_session", return_value="session-token"), \
             patch.object(api_cli, "request", side_effect=fake_request), \
             patch("builtins.input", side_effect=["9", "9"]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["platform-admin", "users", "--office-id", "office-a"]), 0)
            self.assertEqual(cli_main(["platform-admin", "users", "update", "office-a", "9",
                "--name", "Nome", "--membership-status", "inactive", "--account-status", "active"]), 0)
            self.assertEqual(cli_main(["platform-admin", "users", "deactivate", "office-a", "9"]), 0)
            self.assertEqual(cli_main(["platform-admin", "users", "unlink", "office-a", "9"]), 0)
        self.assertEqual(calls[0][1], "/api/platform/offices/office-a/users")
        self.assertEqual(calls[1][0:2], ("PATCH", "/api/platform/offices/office-a/users/9"))
        self.assertEqual(calls[1][2]["payload"], {
            "display_name": "Nome", "active": False, "account_active": True})
        self.assertEqual(calls[2][2]["payload"], {"account_active": False})
        self.assertEqual(calls[3][0:2], ("DELETE", "/api/platform/offices/office-a/users/9"))

    def test_platform_cli_office_update_sends_only_selected_fields(self):
        calls = []
        def fake_request(url, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return ({"id": "office-a", "name": "A atualizado"}, {})
        with patch.object(api_cli, "read_url", return_value="http://127.0.0.1:8000"), \
             patch.object(api_cli, "_session", return_value="session-token"), \
             patch.object(api_cli, "request", side_effect=fake_request), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["platform-admin", "offices", "update", "office-a",
                                       "--name", "A atualizado"]), 0)
        self.assertEqual(calls[0][0:2], ("PATCH", "/api/platform/offices/office-a"))
        self.assertEqual(calls[0][2]["payload"], {"name": "A atualizado"})
        self.assertEqual(calls[0][2]["cookie_name"], "processwatch_platform_session")

    def test_platform_cli_admin_commands_use_global_api_and_confirmation(self):
        calls = []
        def fake_request(url, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return ({"items": []} if method == "GET" else {"status": "ok"}, {})
        with patch.object(api_cli, "read_url", return_value="http://127.0.0.1:8000"), \
             patch.object(api_cli, "_session", return_value="session-token"), \
             patch.object(api_cli, "request", side_effect=fake_request), \
             patch("builtins.input", side_effect=["delegated@example.test", "2", "2"]), \
             patch.object(api_cli.getpass, "getpass", side_effect=["delegated-password-123", "delegated-password-123"]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["platform-admin", "admins", "list"]), 0)
            self.assertEqual(cli_main(["platform-admin", "admins", "create"]), 0)
            self.assertEqual(cli_main(["platform-admin", "admins", "update", "2", "--email", "new@example.test"]), 0)
            self.assertEqual(cli_main(["platform-admin", "admins", "deactivate", "2"]), 0)
            self.assertEqual(cli_main(["platform-admin", "admins", "revoke-sessions", "2"]), 0)
        self.assertEqual(calls[0][0:2], ("GET", "/api/platform/admins"))
        self.assertEqual(calls[1][0:2], ("POST", "/api/platform/admins"))
        self.assertEqual(calls[1][2]["payload"], {
            "email": "delegated@example.test", "password": "delegated-password-123"})
        self.assertEqual(calls[2][0:2], ("PATCH", "/api/platform/admins/2"))
        self.assertEqual(calls[3][0:2], ("PATCH", "/api/platform/admins/2/status"))
        self.assertEqual(calls[4][0:2], ("POST", "/api/platform/admins/2/sessions/revoke"))
        self.assertTrue(all(call[2]["cookie_name"] == "processwatch_platform_session" for call in calls))

    def test_global_admin_lifecycle_keeps_bootstrap_owner_distinct_and_protected(self):
        initial_office_users = self.repo.db.execute("SELECT count(*) FROM users").fetchone()[0]
        initial_memberships = self.repo.db.execute("SELECT count(*) FROM office_memberships").fetchone()[0]
        async def scenario():
            async with self.client() as owner_client:
                owner_login = await owner_client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                self.assertEqual(owner_login.status_code, 200)
                self.assertTrue(owner_login.json()["administrator"]["is_owner"])
                created = await owner_client.post("/api/platform/admins", json={
                    "email": "delegated@example.test", "password": "delegated-password-123"},
                    headers={"Origin": "http://test"})
                self.assertEqual(created.status_code, 201, created.text)
                second = created.json()
                self.assertFalse(second["is_owner"])
                self.assertNotIn("password", created.text)
                self.assertNotIn("password_hash", created.text)
                self.assertNotIn("delegated-password-123", created.text)
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM users").fetchone()[0], initial_office_users)
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM office_memberships").fetchone()[0], initial_memberships)
                escalation = await owner_client.post("/api/platform/admins", json={
                    "email": "escalation@example.test", "password": "escalation-password-123", "is_owner": True},
                    headers={"Origin": "http://test"})
                self.assertEqual(escalation.status_code, 422)
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM platform_admins").fetchone()[0], 2)

                listed = await owner_client.get("/api/platform/admins")
                self.assertEqual(listed.status_code, 200, listed.text)
                self.assertEqual(len(listed.json()["items"]), 2)
                self.assertEqual(sum(row["is_owner"] for row in listed.json()["items"]), 1)
                self.assertNotIn("password_hash", listed.text)
                detail = await owner_client.get(f"/api/platform/admins/{second['id']}")
                self.assertEqual(detail.status_code, 200, detail.text)

                async with self.client() as delegated_client:
                    delegated_login = await delegated_client.post("/api/platform/auth/login", json={
                        "email": "delegated@example.test", "password": "delegated-password-123"})
                    self.assertEqual(delegated_login.status_code, 200, delegated_login.text)
                    self.assertFalse(delegated_login.json()["administrator"]["is_owner"])
                    owner_id = self.repo.db.execute("SELECT id FROM platform_admins WHERE is_owner=1").fetchone()[0]
                    denied_status = await delegated_client.patch(
                        f"/api/platform/admins/{owner_id}/status", json={"active": False},
                        headers={"Origin": "http://test"})
                    self.assertEqual(denied_status.status_code, 403)
                    denied_update = await delegated_client.patch(f"/api/platform/admins/{owner_id}",
                        json={"email": "takeover@example.test"}, headers={"Origin": "http://test"})
                    self.assertEqual(denied_update.status_code, 403)
                    denied_sessions = await delegated_client.post(
                        f"/api/platform/admins/{owner_id}/sessions/revoke", headers={"Origin": "http://test"})
                    self.assertEqual(denied_sessions.status_code, 403)
                    escalation_update = await delegated_client.patch(
                        f"/api/platform/admins/{second['id']}",
                        json={"email": "self-elevated@example.test", "is_owner": True},
                        headers={"Origin": "http://test"})
                    self.assertEqual(escalation_update.status_code, 422)
                    self.assertIsNotNone(self.repo.authenticate_platform_admin(
                        "platform@example.test", "strong-platform-password-123"))

                email_change_session = "delegated-session-before-email-change"
                self.repo.create_platform_admin_session(second["id"], email_change_session,
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
                changed = await owner_client.patch(f"/api/platform/admins/{second['id']}",
                    json={"email": "delegated-updated@example.test"}, headers={"Origin": "http://test"})
                self.assertEqual(changed.status_code, 200, changed.text)
                self.assertEqual(changed.json()["email"], "delegated-updated@example.test")
                self.assertIsNone(self.repo.platform_admin_session(email_change_session))
                second_session = "delegated-session-before-deactivate"
                self.repo.create_platform_admin_session(second["id"], second_session,
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
                revoked = await owner_client.post(
                    f"/api/platform/admins/{second['id']}/sessions/revoke", headers={"Origin": "http://test"})
                self.assertEqual(revoked.status_code, 200, revoked.text)
                self.assertEqual(revoked.json()["revoked_sessions"], 1)
                self.assertIsNone(self.repo.platform_admin_session(second_session))
                second_session = "delegated-session-before-deactivate-2"
                self.repo.create_platform_admin_session(second["id"], second_session,
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
                disabled = await owner_client.patch(f"/api/platform/admins/{second['id']}/status",
                    json={"active": False}, headers={"Origin": "http://test"})
                self.assertEqual(disabled.status_code, 200, disabled.text)
                self.assertFalse(disabled.json()["active"])
                self.assertIsNone(self.repo.platform_admin_session(second_session))
                async with self.client() as disabled_client:
                    self.assertEqual((await disabled_client.post("/api/platform/auth/login", json={
                        "email": "delegated-updated@example.test", "password": "delegated-password-123"})).status_code, 401)
                enabled = await owner_client.patch(f"/api/platform/admins/{second['id']}/status",
                    json={"active": True}, headers={"Origin": "http://test"})
                self.assertEqual(enabled.status_code, 200, enabled.text)
                self.assertIsNone(self.repo.platform_admin_session(second_session))

                owner_id = self.repo.db.execute("SELECT id FROM platform_admins WHERE is_owner=1").fetchone()[0]
                owner_disable = await owner_client.patch(f"/api/platform/admins/{owner_id}/status",
                    json={"active": False}, headers={"Origin": "http://test"})
                self.assertEqual(owner_disable.status_code, 409)
                owner_row = self.repo.platform_admin(owner_id)
                self.assertTrue(owner_row["active"])
                self.assertTrue(owner_row["is_owner"])
                logs = self.repo.db.execute("SELECT action,actor,purpose FROM access_log WHERE action LIKE 'platform_admin_%'").fetchall()
                self.assertTrue(any(row["action"] == "platform_admin_create" for row in logs))
                self.assertTrue(any(row["action"] == "platform_admin_deactivate" for row in logs))
                self.assertTrue(any(row["action"] == "platform_admin_status_denied" for row in logs))
                self.assertTrue(all("delegated-password-123" not in row["purpose"] for row in logs))
        asyncio.run(scenario())

    def test_platform_admin_password_change_requires_current_and_revokes_all_sessions(self):
        async def scenario():
            first, second = self.client(), self.client()
            for client in (first, second):
                login = await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                self.assertEqual(login.status_code, 200)
            wrong = await first.post("/api/platform/auth/password", json={
                "current_password": "wrong-password", "new_password": "new-platform-password-123"},
                headers={"Origin": "http://test"})
            self.assertEqual(wrong.status_code, 400)
            changed = await first.post("/api/platform/auth/password", json={
                "current_password": "strong-platform-password-123",
                "new_password": "new-platform-password-123"}, headers={"Origin": "http://test"})
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertNotIn("new-platform-password-123", changed.text)
            self.assertEqual((await second.get("/api/platform/auth/me")).status_code, 401)
            self.assertIsNone(self.repo.authenticate_platform_admin(
                "platform@example.test", "strong-platform-password-123"))
            self.assertIsNotNone(self.repo.authenticate_platform_admin(
                "platform@example.test", "new-platform-password-123"))
            audit = self.repo.db.execute("SELECT action,purpose FROM access_log "
                "WHERE action LIKE 'platform_admin_password_%'").fetchall()
            self.assertIn("platform_admin_password_change", {row["action"] for row in audit})
            self.assertTrue(all("password" not in row["purpose"] for row in audit))
            await first.aclose(); await second.aclose()
        asyncio.run(scenario())

    def test_owner_can_reset_delegated_password_but_delegates_cannot_reset_owner(self):
        delegated = self.repo.create_platform_admin("reset-me@example.test", "delegated-old-password-123")
        async def scenario():
            owner, delegate = self.client(), self.client()
            await owner.post("/api/platform/auth/login", json={
                "email": "platform@example.test", "password": "strong-platform-password-123"})
            await delegate.post("/api/platform/auth/login", json={
                "email": "reset-me@example.test", "password": "delegated-old-password-123"})
            owner_id = self.repo.db.execute("SELECT id FROM platform_admins WHERE is_owner=1").fetchone()[0]
            denied = await delegate.post(f"/api/platform/admins/{owner_id}/password/reset", json={
                "new_password": "owner-reset-attempt-123"}, headers={"Origin": "http://test"})
            self.assertEqual(denied.status_code, 403)
            owner_reset = await owner.post(f"/api/platform/admins/{owner_id}/password/reset", json={
                "new_password": "owner-reset-attempt-123"}, headers={"Origin": "http://test"})
            self.assertEqual(owner_reset.status_code, 409)
            reset = await owner.post(f"/api/platform/admins/{delegated['id']}/password/reset", json={
                "new_password": "delegated-new-password-456"}, headers={"Origin": "http://test"})
            self.assertEqual(reset.status_code, 200, reset.text)
            self.assertNotIn("delegated-new-password-456", reset.text)
            self.assertEqual((await delegate.get("/api/platform/auth/me")).status_code, 401)
            self.assertIsNone(self.repo.authenticate_platform_admin(
                "reset-me@example.test", "delegated-old-password-123"))
            self.assertIsNotNone(self.repo.authenticate_platform_admin(
                "reset-me@example.test", "delegated-new-password-456"))
            audit = self.repo.db.execute("SELECT action,purpose FROM access_log "
                "WHERE action LIKE 'platform_admin_password_%'").fetchall()
            self.assertIn("platform_admin_password_reset", {row["action"] for row in audit})
            self.assertTrue(all("delegated-new-password-456" not in row["purpose"] for row in audit))
            await owner.aclose(); await delegate.aclose()
        asyncio.run(scenario())

    def test_local_owner_recovery_restores_access_and_preserves_office_records(self):
        owner_id = self.repo.db.execute("SELECT id FROM platform_admins WHERE is_owner=1").fetchone()[0]
        raw_session = "owner-session-before-local-recovery"
        self.repo.create_platform_admin_session(owner_id, raw_session,
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        self.repo.db.execute("UPDATE platform_admins SET active=0 WHERE id=?", (owner_id,))
        self.repo.db.commit()
        original_hash = self.repo.db.execute(
            "SELECT password_hash FROM platform_admins WHERE id=?", (owner_id,)).fetchone()[0]
        self.repo.db.execute("""CREATE TRIGGER fail_local_owner_recovery
            BEFORE UPDATE OF password_hash ON platform_admins WHEN OLD.id=%d
            BEGIN SELECT RAISE(ABORT,'forced owner reset failure'); END""" % owner_id)
        self.repo.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.recover_platform_owner_password_locally(
                "platform@example.test", "failed-owner-password-123", actor="test-operator")
        owner_before_retry = self.repo.db.execute(
            "SELECT password_hash,active FROM platform_admins WHERE id=?", (owner_id,)).fetchone()
        self.assertEqual((owner_before_retry["password_hash"], owner_before_retry["active"]),
                         (original_hash, 0))
        self.assertIsNone(self.repo.db.execute("SELECT revoked_at FROM platform_admin_sessions WHERE token_hash=?",
            (hashlib.sha256(raw_session.encode("ascii")).hexdigest(),)).fetchone()[0])
        self.repo.db.execute("DROP TRIGGER fail_local_owner_recovery")
        self.repo.db.commit()
        processes_before = self.repo.db.execute("SELECT count(*) FROM watches").fetchone()[0]
        users_before = self.repo.db.execute("SELECT count(*) FROM users").fetchone()[0]
        with patch("builtins.input", return_value="platform@example.test"), \
             patch.object(api_cli.getpass, "getpass", side_effect=[
                 "restored-owner-password-123", "restored-owner-password-123"]), \
             patch("getpass.getuser", return_value="test-operator"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["platform-admin", "reset-owner-password", "--db", self.db_path,
                                       "--email", "platform@example.test"]), 0)
        self.assertIsNotNone(self.repo.authenticate_platform_admin(
            "platform@example.test", "restored-owner-password-123"))
        self.assertIsNone(self.repo.platform_admin_session(raw_session))
        owner = self.repo.platform_admin(owner_id)
        self.assertTrue(owner["active"])
        self.assertTrue(owner["is_owner"])
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM watches").fetchone()[0], processes_before)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM users").fetchone()[0], users_before)
        log = self.repo.db.execute("SELECT actor,org,purpose FROM access_log "
            "WHERE action='platform_owner_password_recovery'").fetchone()
        self.assertEqual(log["actor"], "local_operator:test-operator")
        self.assertIsNone(log["org"])
        self.assertNotIn("restored-owner-password-123", log["purpose"])

    def test_office_password_recovery_issuer_is_audited_and_token_stays_single_use(self):
        target = self.repo.create_user("recover-target@example.test", "target-old-password-123",
            "office-a", role="lawyer", full_name="Target")
        token = issue_password_recovery_token(self.repo, "admin-a@example.test",
            "strong-office-password-123", "office-a", "recover-target@example.test")
        self.assertNotEqual(self.repo.db.execute(
            "SELECT token_hash FROM password_recovery_tokens WHERE user_id=?", (target["id"],)
        ).fetchone()[0], token)
        audit = self.repo.db.execute("SELECT actor,org,purpose FROM access_log "
            "WHERE action='password_recovery_token_issued'").fetchone()
        self.assertEqual((audit["actor"], audit["org"]), ("office_admin:admin-a@example.test", "office-a"))
        self.assertNotIn(token, audit["purpose"])

    def test_platform_admin_can_issue_scoped_office_recovery_without_email(self):
        target = self.repo.create_user("platform-help@example.test", "target-old-password-123",
            "office-a", role="lawyer", full_name="Target")
        async def scenario():
            async with self.client() as client:
                login = await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                self.assertEqual(login.status_code, 200)
                issued = await client.post(
                    f"/api/platform/offices/office-a/users/{target['id']}/recovery",
                    headers={"Origin": "http://test"})
                self.assertEqual(issued.status_code, 201, issued.text)
                token = issued.json()["token"]
                self.assertIn("não há envio por e-mail", issued.json()["message"])
                stored = self.repo.db.execute("SELECT token_hash FROM password_recovery_tokens WHERE user_id=?",
                                              (target["id"],)).fetchone()[0]
                self.assertNotEqual(stored, token)
                self.assertNotIn(token, self.repo.db.execute(
                    "SELECT purpose FROM access_log WHERE action='password_recovery_token_issued'").fetchone()[0])
                complete = await client.post("/api/auth/recovery/complete", json={
                    "email": "platform-help@example.test", "token": token,
                    "new_password": "recovered-office-password-456"})
                self.assertEqual(complete.status_code, 200, complete.text)
                self.assertIsNotNone(self.repo.authenticate_user(
                    "platform-help@example.test", "recovered-office-password-456"))
                self.assertIn("password_recovery_complete", {
                    row[0] for row in self.repo.db.execute("SELECT action FROM access_log")})
                wrong_scope = await client.post(
                    f"/api/platform/offices/office-b/users/{target['id']}/recovery",
                    headers={"Origin": "http://test"})
                self.assertEqual(wrong_scope.status_code, 404)
        asyncio.run(scenario())

    def test_delegated_password_reset_rolls_back_password_and_sessions_on_database_failure(self):
        delegated = self.repo.create_platform_admin("rollback-reset@example.test", "rollback-old-password-123")
        raw_session = "rollback-reset-session"
        self.repo.create_platform_admin_session(delegated["id"], raw_session,
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        self.repo.db.execute("""CREATE TRIGGER fail_delegated_password_reset
            BEFORE UPDATE OF password_hash ON platform_admins WHEN OLD.id=%d
            BEGIN SELECT RAISE(ABORT,'forced reset failure'); END""" % delegated["id"])
        self.repo.db.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.reset_delegated_platform_admin_password(
                delegated["id"], "rollback-new-password-123")
        self.assertIsNotNone(self.repo.authenticate_platform_admin(
            "rollback-reset@example.test", "rollback-old-password-123"))
        self.assertIsNone(self.repo.authenticate_platform_admin(
            "rollback-reset@example.test", "rollback-new-password-123"))
        self.assertIsNotNone(self.repo.platform_admin_session(raw_session))

    def test_platform_password_cli_uses_authenticated_routes_and_clears_local_session(self):
        calls = []
        def fake_request(url, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return ({"status": "ok"}, {})
        with patch.object(api_cli, "read_url", return_value="http://127.0.0.1:8000"), \
             patch.object(api_cli, "_session", return_value="platform-session"), \
             patch.object(api_cli, "request", side_effect=fake_request), \
             patch.object(api_cli, "delete_session") as delete_session, \
             patch("builtins.input", side_effect=["5"]), \
             patch.object(api_cli.getpass, "getpass", side_effect=[
                 "old-platform-password", "new-platform-password-123", "new-platform-password-123",
                 "delegated-reset-password-123", "delegated-reset-password-123"]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["platform-admin", "change-password"]), 0)
            self.assertEqual(cli_main(["platform-admin", "admins", "reset-password", "5"]), 0)
        self.assertEqual(calls[0][0:2], ("POST", "/api/platform/auth/password"))
        self.assertEqual(calls[0][2]["payload"], {
            "current_password": "old-platform-password",
            "new_password": "new-platform-password-123"})
        self.assertEqual(calls[1][0:2], ("POST", "/api/platform/admins/5/password/reset"))
        self.assertEqual(calls[1][2]["payload"], {"new_password": "delegated-reset-password-123"})
        self.assertTrue(all(call[2]["cookie_name"] == "processwatch_platform_session" for call in calls))
        delete_session.assert_called_once_with("http://127.0.0.1:8000", platform=True)

    def test_office_and_platform_cli_recovery_commands_use_scoped_routes(self):
        calls = []
        def fake_request(url, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return ({"status": "issued", "token": "one-time-secret"}, {})
        with patch.object(api_cli, "read_url", return_value="http://127.0.0.1:8000"), \
             patch.object(api_cli, "_session", side_effect=["office-session", "platform-session"]), \
             patch.object(api_cli, "request", side_effect=fake_request), \
             patch("builtins.input", side_effect=["42", "42"]), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["users", "issue-recovery", "42"]), 0)
            self.assertEqual(cli_main(["platform-admin", "users", "issue-recovery", "office-a", "42"]), 0)
        self.assertEqual(calls[0][0:2], ("POST", "/api/admin/users/42/recovery"))
        self.assertEqual(calls[1][0:2], (
            "POST", "/api/platform/offices/office-a/users/42/recovery"))
        self.assertEqual(calls[0][2]["session"], "office-session")
        self.assertEqual(calls[1][2]["session"], "platform-session")
        self.assertEqual(calls[1][2]["cookie_name"], "processwatch_platform_session")

    def test_global_admin_routes_reject_office_credentials_and_failed_create_rolls_back(self):
        async def scenario():
            async with self.client() as client:
                self.assertEqual((await client.get("/api/platform/admins")).status_code, 401)
                self.assertEqual((await client.post("/api/platform/auth/password", json={
                    "current_password": "x", "new_password": "new-platform-password-123"},
                    headers={"Origin": "http://test"})).status_code, 401)
                await client.post("/api/auth/login", json={"email": "admin-a@example.test",
                                 "password": "strong-office-password-123"})
                self.assertEqual((await client.get("/api/platform/admins")).status_code, 401)
                self.assertEqual((await client.post("/api/platform/admins/1/password/reset", json={
                    "new_password": "new-platform-password-123"},
                    headers={"Origin": "http://test"})).status_code, 401)
                _, secret = self.repo.create_service_credential("office-a", "writer", ["processes:read"],
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
                self.assertEqual((await client.get("/api/platform/admins",
                    headers={"Authorization": f"Bearer {secret}"})).status_code, 401)
                self.assertEqual((await client.post("/api/platform/admins", json={"email": "x@y.test",
                    "password": "another-password-123"}, headers={"Origin": "http://test"})).status_code, 401)
        asyncio.run(scenario())

        self.repo.db.execute("""CREATE TRIGGER fail_platform_admin_create BEFORE INSERT ON platform_admins
            WHEN NEW.email='rollback-global@example.test' BEGIN SELECT RAISE(ABORT,'forced failure'); END""")
        self.repo.db.commit()
        async def rollback_scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={"email": "platform@example.test",
                    "password": "strong-platform-password-123"})
                failed = await client.post("/api/platform/admins", json={
                    "email": "rollback-global@example.test", "password": "rollback-password-123"},
                    headers={"Origin": "http://test"})
                self.assertEqual(failed.status_code, 409)
                self.assertIsNone(self.repo.db.execute("SELECT 1 FROM platform_admins WHERE email=?",
                    ("rollback-global@example.test",)).fetchone())
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM access_log WHERE action='platform_admin_create_failed'").fetchone()[0], 1)
        asyncio.run(rollback_scenario())

    def test_v18_migration_preserves_existing_global_admin_as_owner_and_is_idempotent(self):
        legacy_path = str(Path(self.temp.name) / "legacy-platform-admin.sqlite3")
        connection = sqlite3.connect(legacy_path)
        connection.executescript("""CREATE TABLE platform_admins (
            id INTEGER PRIMARY KEY,email TEXT NOT NULL COLLATE NOCASE UNIQUE,password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,password_iterations INTEGER NOT NULL,created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),last_login_at TEXT);
            INSERT INTO platform_admins(id,email,password_hash,password_salt,password_iterations,created_at,active)
            VALUES(7,'legacy-owner@example.test','preserved-hash','preserved-salt',310000,'2026-01-01',1);
            PRAGMA user_version=17;""")
        connection.commit()
        connection.close()
        migrated = SQLiteRepository(legacy_path, environment="test")
        try:
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], 18)
            admin = migrated.platform_admin(7)
            self.assertTrue(admin["is_owner"])
            self.assertEqual(admin["email"], "legacy-owner@example.test")
            self.assertEqual(tuple(migrated.db.execute(
                "SELECT password_hash,password_salt FROM platform_admins WHERE id=7").fetchone()),
                ("preserved-hash", "preserved-salt"))
            self.assertEqual(migrated.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(migrated.db.execute("PRAGMA foreign_key_check").fetchall(), [])
            backups = list(Path(self.temp.name).glob("legacy-platform-admin.sqlite3.pre-v18-*.backup.sqlite3"))
            self.assertEqual(len(backups), 1)
            backup = sqlite3.connect(str(backups[0]))
            try:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 17)
                self.assertEqual(backup.execute("SELECT email FROM platform_admins WHERE id=7").fetchone()[0],
                                 "legacy-owner@example.test")
            finally:
                backup.close()
        finally:
            migrated.close()
        repeated = SQLiteRepository(legacy_path, environment="test")
        try:
            self.assertEqual(repeated.db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(repeated.db.execute("SELECT count(*) FROM platform_admins WHERE is_owner=1").fetchone()[0], 1)
            self.assertEqual(len(list(Path(self.temp.name).glob("legacy-platform-admin.sqlite3.pre-v18-*.backup.sqlite3"))), 1)
        finally:
            repeated.close()

    def test_platform_admin_partially_edits_active_and_inactive_offices_safely(self):
        self.repo.db.execute("INSERT INTO watches(org,number,court,interval,created_at,next_due) VALUES(?,?,?,?,?,?)",
                             ("office-a", "00000003800004010000", "trf1", 86400, "2026-01-01", "2026-01-02"))
        self.repo.db.commit()
        original_b = self.repo.office("office-b")
        original_users = self.repo.db.execute(
            "SELECT count(*) FROM office_memberships WHERE office_id='office-a'").fetchone()[0]
        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                response = await client.patch("/api/platform/offices/office-a", json={"name": "Nome ajustado"},
                                              headers={"Origin": "http://test"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["name"], "Nome ajustado")
                self.assertEqual(response.json()["email"], "")
                self.assertTrue(response.json()["active"])
                self.assertEqual(self.repo.office("office-b"), original_b)
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM office_memberships WHERE office_id='office-a'").fetchone()[0], original_users)
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM watches WHERE org='office-a'").fetchone()[0], 1)

                self.repo.set_office_active("office-a", False)
                inactive_response = await client.patch("/api/platform/offices/office-a", json={
                    "email": "contato@example.test", "phone": "(11) 3333-4444"},
                    headers={"Origin": "http://test"})
                self.assertEqual(inactive_response.status_code, 200, inactive_response.text)
                self.assertEqual(inactive_response.json()["active"], 0)
                self.assertEqual(inactive_response.json()["name"], "Nome ajustado")
                self.assertEqual(inactive_response.json()["email"], "contato@example.test")
                self.assertEqual(inactive_response.json()["address"], "")

                invalid = await client.patch("/api/platform/offices/office-a", json={"email": "not-an-email"},
                                             headers={"Origin": "http://test"})
                self.assertEqual(invalid.status_code, 400)
                identifier = await client.patch("/api/platform/offices/office-a", json={"id": "renamed"},
                                                headers={"Origin": "http://test"})
                self.assertEqual(identifier.status_code, 422)
                self.assertEqual(self.repo.office("office-a")["id"], "office-a")
                self.assertIsNone(self.repo.office("renamed"))
                empty = await client.patch("/api/platform/offices/office-a", json={},
                                           headers={"Origin": "http://test"})
                self.assertEqual(empty.status_code, 422)
                audits = self.repo.db.execute("SELECT action,purpose,actor FROM access_log WHERE org='office-a' "
                                              "AND action LIKE 'platform_office_update%' ORDER BY id").fetchall()
                self.assertEqual(audits[0]["action"], "platform_office_update")
                self.assertIn("name", audits[0]["purpose"])
                self.assertEqual(audits[0]["actor"], "platform_admin:platform@example.test")
                self.assertTrue(any(row["action"] == "platform_office_update_failed" for row in audits))
        asyncio.run(scenario())

    def test_platform_office_patch_rolls_back_on_sqlite_failure(self):
        original = self.repo.office("office-a")
        self.repo.db.execute("""CREATE TRIGGER fail_office_patch BEFORE UPDATE OF email ON offices
            WHEN NEW.email='trigger@example.test' BEGIN SELECT RAISE(ABORT,'forced failure'); END""")
        self.repo.db.commit()
        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                response = await client.patch("/api/platform/offices/office-a", json={
                    "name": "Não salvar", "email": "trigger@example.test"},
                    headers={"Origin": "http://test"})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.repo.office("office-a"), original)
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM access_log WHERE org='office-a' "
                    "AND action='platform_office_update_failed'").fetchone()[0], 1)
        asyncio.run(scenario())

    def test_platform_admin_can_manage_users_for_an_inactive_office_without_reactivating_it(self):
        self.repo.create_office("inactive-office", "Escritório Inativo")
        self.repo.db.execute("UPDATE offices SET active=0 WHERE id='inactive-office'")
        self.repo.db.commit()
        initial_users = self.repo.db.execute("SELECT count(*) FROM users").fetchone()[0]
        initial_watches = self.repo.db.execute("SELECT count(*) FROM watches WHERE org='inactive-office'").fetchone()[0]
        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={"email": "platform@example.test",
                    "password": "strong-platform-password-123"})
                created = await client.post("/api/platform/offices/inactive-office/users", json={
                    "email": "inactive-admin@example.test", "full_name": "Admin Preparado",
                    "role": "office_admin", "temporary_password": "initial-password-123"},
                    headers={"Origin": "http://test"})
                self.assertEqual(created.status_code, 201, created.text)
                self.assertTrue(created.json()["created"])
                user_id = created.json()["id"]
                self.assertEqual(self.repo.office("inactive-office")["active"], 0)
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM users").fetchone()[0], initial_users + 1)
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM watches WHERE org='inactive-office'").fetchone()[0], initial_watches)
                self.assertNotIn("initial-password-123", created.text)
                self.assertTrue(self.repo.verify_user_password(user_id, "initial-password-123"))

                duplicate = await client.post("/api/platform/offices/inactive-office/users", json={
                    "email": "inactive-admin@example.test", "full_name": "Nome Não Aplicado",
                    "role": "lawyer", "temporary_password": "replacement-password-123"},
                    headers={"Origin": "http://test"})
                self.assertEqual(duplicate.status_code, 409)
                self.assertTrue(self.repo.verify_user_password(user_id, "initial-password-123"))
                self.assertFalse(self.repo.verify_user_password(user_id, "replacement-password-123"))

                listing = await client.get("/api/platform/offices/inactive-office/users")
                self.assertEqual(listing.status_code, 200, listing.text)
                self.assertEqual(listing.json()["items"][0]["role"], "office_admin")
                detail = await client.get(f"/api/platform/offices/inactive-office/users/{user_id}")
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json()["account"]["email"], "inactive-admin@example.test")
                self.assertEqual(len(detail.json()["memberships"]), 1)
                actions = self.repo.db.execute("SELECT action,actor,org,purpose FROM access_log "
                    "WHERE org='inactive-office' ORDER BY id").fetchall()
                self.assertIn("platform_office_user_create", [row["action"] for row in actions])
                self.assertIn("platform_office_users_list", [row["action"] for row in actions])
                self.assertIn("platform_office_user_detail", [row["action"] for row in actions])
                self.assertTrue(all(row["actor"] == "platform_admin:platform@example.test" for row in actions))
                self.assertTrue(all(row["org"] == "inactive-office" for row in actions))
                self.assertTrue(all("initial-password-123" not in row["purpose"] for row in actions))
        asyncio.run(scenario())

    def test_global_account_state_and_office_membership_state_are_independent(self):
        shared = self.repo.create_user("shared@example.test", "shared-password-123", "office-a",
                                       role="lawyer", full_name="Shared User")
        self.repo.ensure_office_membership("office-b", "shared@example.test", "lawyer", full_name="Shared B")
        session_a = "shared-session-office-a"
        session_b = "shared-session-office-b"
        expiry = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.repo.create_auth_session(shared["id"], session_a, "office-a", expiry)
        self.repo.create_auth_session(shared["id"], session_b, "office-b", expiry)
        credential_a, secret_a = self.repo.create_service_credential("office-a", "shared-a", ["processes:read"], expiry,
                                                                      created_by="shared@example.test")
        credential_b, secret_b = self.repo.create_service_credential("office-b", "shared-b", ["processes:read"], expiry,
                                                                      created_by="shared@example.test")
        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={"email": "platform@example.test",
                    "password": "strong-platform-password-123"})
                scoped = await client.patch(f"/api/platform/offices/office-a/users/{shared['id']}", json={
                    "display_name": "Nome no A", "active": False, "role": "office_admin"},
                    headers={"Origin": "http://test"})
                self.assertEqual(scoped.status_code, 200, scoped.text)
                account = self.repo.db.execute("SELECT active FROM users WHERE id=?", (shared["id"],)).fetchone()[0]
                memberships = {row["office_id"]: dict(row) for row in self.repo.db.execute(
                    "SELECT office_id,active,role,display_name FROM office_memberships WHERE user_id=?",
                    (shared["id"],)).fetchall()}
                self.assertEqual(account, 1)
                self.assertEqual(memberships["office-a"]["active"], 0)
                self.assertEqual(memberships["office-a"]["role"], "office_admin")
                self.assertEqual(memberships["office-a"]["display_name"], "Nome no A")
                self.assertEqual(memberships["office-b"]["active"], 1)
                self.assertEqual(memberships["office-b"]["role"], "lawyer")
                self.assertEqual(memberships["office-b"]["display_name"], "Shared B")
                self.assertIsNone(self.repo.auth_session(session_a))
                self.assertIsNotNone(self.repo.auth_session(session_b))
                cred_states = {row["id"]: row["revoked_at"] for row in self.repo.db.execute(
                    "SELECT id,revoked_at FROM service_credentials WHERE id IN (?,?)",
                    (credential_a["id"], credential_b["id"])).fetchall()}
                self.assertIsNotNone(cred_states[credential_a["id"]])
                self.assertIsNone(cred_states[credential_b["id"]])

                account_off = await client.patch(f"/api/platform/offices/office-a/users/{shared['id']}",
                    json={"account_active": False}, headers={"Origin": "http://test"})
                self.assertEqual(account_off.status_code, 200, account_off.text)
                self.assertEqual(self.repo.db.execute("SELECT active FROM users WHERE id=?", (shared["id"],)).fetchone()[0], 0)
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM office_memberships WHERE user_id=? AND active=1",
                                                     (shared["id"],)).fetchone()[0], 1)
                self.assertIsNone(self.repo.auth_session(session_b))
                self.assertIsNotNone(self.repo.db.execute("SELECT revoked_at FROM service_credentials WHERE id=? AND revoked_at IS NOT NULL",
                                                          (credential_b["id"],)).fetchone())
                account_on = await client.patch(f"/api/platform/offices/office-a/users/{shared['id']}",
                    json={"account_active": True}, headers={"Origin": "http://test"})
                self.assertEqual(account_on.status_code, 200, account_on.text)
                self.assertEqual(account_on.json()["user"]["account"]["active"], 1)
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM office_memberships WHERE user_id=? AND active=1",
                                                     (shared["id"],)).fetchone()[0], 1)

                removed = await client.delete(f"/api/platform/offices/office-a/users/{shared['id']}",
                                              headers={"Origin": "http://test"})
                self.assertEqual(removed.status_code, 200, removed.text)
                self.assertEqual(removed.json()["remaining_memberships"], 1)
                self.assertIsNotNone(self.repo.find_user("shared@example.test"))
                self.assertIsNotNone(self.repo.platform_office_user("office-b", shared["id"]))
                self.assertIsNone(self.repo.platform_office_user("office-a", shared["id"]))
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM watches WHERE org='local'").fetchone()[0], 1)
                logged = self.repo.db.execute("SELECT count(*) FROM access_log WHERE actor='platform_admin:platform@example.test' "
                    "AND org='office-a' AND action LIKE 'platform_office_user_%'").fetchone()[0]
                self.assertGreaterEqual(logged, 4)
                self.assertNotIn(secret_a, " ".join(row[0] or "" for row in self.repo.db.execute(
                    "SELECT purpose FROM access_log WHERE org='office-a'")))
                self.assertNotIn(secret_b, " ".join(row[0] or "" for row in self.repo.db.execute(
                    "SELECT purpose FROM access_log WHERE org='office-a'")))
        asyncio.run(scenario())

    def test_global_user_operations_keep_last_admin_protection_and_can_link_inactive_account(self):
        inactive = self.repo.create_user("inactive-shared@example.test", "inactive-password-123", "office-b",
                                         role="lawyer", full_name="Inactive Shared")
        self.repo.db.execute("UPDATE users SET active=0 WHERE id=?", (inactive["id"],))
        self.repo.db.commit()
        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={"email": "platform@example.test",
                    "password": "strong-platform-password-123"})
                last_admin = await client.patch(f"/api/platform/offices/office-a/users/{self.repo.find_user('admin-a@example.test')['id']}",
                    json={"account_active": False}, headers={"Origin": "http://test"})
                self.assertEqual(last_admin.status_code, 409)
                last_admin_membership = await client.patch(
                    f"/api/platform/offices/office-a/users/{self.repo.find_user('admin-a@example.test')['id']}",
                    json={"active": False}, headers={"Origin": "http://test"})
                self.assertEqual(last_admin_membership.status_code, 409)
                last_admin_role = await client.patch(
                    f"/api/platform/offices/office-a/users/{self.repo.find_user('admin-a@example.test')['id']}",
                    json={"role": "lawyer"}, headers={"Origin": "http://test"})
                self.assertEqual(last_admin_role.status_code, 409)
                last_admin_remove = await client.delete(
                    f"/api/platform/offices/office-a/users/{self.repo.find_user('admin-a@example.test')['id']}",
                    headers={"Origin": "http://test"})
                self.assertEqual(last_admin_remove.status_code, 409)
                self.assertEqual(self.repo.find_user("admin-a@example.test")["active"], 1)
                link = await client.post("/api/platform/offices/office-a/users", json={
                    "email": "inactive-shared@example.test", "role": "lawyer"},
                    headers={"Origin": "http://test"})
                self.assertEqual(link.status_code, 201, link.text)
                self.assertFalse(link.json()["created"])
                self.assertEqual(self.repo.find_user("inactive-shared@example.test")["active"], 0)
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM users WHERE email='inactive-shared@example.test'").fetchone()[0], 1)
                activated = await client.patch(f"/api/platform/offices/office-a/users/{inactive['id']}",
                    json={"account_active": True}, headers={"Origin": "http://test"})
                self.assertEqual(activated.status_code, 200, activated.text)
                self.assertEqual(activated.json()["user"]["account"]["active"], 1)
        asyncio.run(scenario())

    def test_global_user_create_rolls_back_account_and_membership_together(self):
        original = self.repo._create_user_in_transaction

        def create_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.IntegrityError("forced failure after account and membership insert")

        with patch.object(self.repo, "_create_user_in_transaction", side_effect=create_then_fail):
            with self.assertRaises(sqlite3.IntegrityError):
                self.repo.ensure_platform_office_membership(
                    "office-a", "rollback@example.test", "lawyer",
                    password="rollback-password-123", full_name="Rollback User")
        self.assertIsNone(self.repo.find_user("rollback@example.test"))
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM office_memberships WHERE office_id='office-a' "
                                               "AND display_name='Rollback User'").fetchone()[0], 0)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM watches WHERE org='local'").fetchone()[0], 1)

    def test_office_provisioning_is_transactional_and_creates_first_admin(self):
        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                payload = {"office_id": "new-office", "name": "Novo Escritório", "email": "office@example.test",
                    "phone": "(11) 1234-5678", "address": "Rua Teste", "administrator_email": "first@example.test",
                    "administrator_name": "Primeiro Admin", "administrator_password": "strong-first-password-123"}
                created = await client.post("/api/platform/offices", json=payload, headers={"Origin": "http://test"})
                self.assertEqual(created.status_code, 201, created.text)
                self.assertEqual(created.json()["office"]["email"], "office@example.test")
                self.assertEqual(created.json()["administrator"]["email"], "first@example.test")
                self.assertTrue(self.repo.authenticate_user("first@example.test", payload["administrator_password"]))
                collision = dict(payload, office_id="rollback-office", administrator_email="admin-a@example.test")
                rejected = await client.post("/api/platform/offices", json=collision,
                                             headers={"Origin": "http://test"})
                self.assertIn(rejected.status_code, (409, 422))
                self.assertIsNone(self.repo.office("rollback-office"))
        asyncio.run(scenario())

    def test_office_detail_api_returns_cadastral_stats_and_members_for_drawer(self):
        self.repo.provision_office("drawer-office", "Drawer Office", "drawer-admin@example.test",
            "strong-drawer-password-123", full_name="Drawer Admin", office_email="contact@example.test",
            phone="(11) 1234-5678", address="Rua de Teste, 10")
        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                response = await client.get("/api/platform/offices/drawer-office")
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["office"]["phone"], "(11) 1234-5678")
                self.assertEqual(body["office"]["address"], "Rua de Teste, 10")
                self.assertEqual(body["stats"]["users"], 1)
                self.assertEqual(body["stats"]["processes"], 0)
                self.assertEqual(len(body["users"]), 1)
                self.assertEqual(body["users"][0]["role"], "office_admin")
        asyncio.run(scenario())

    def _make_inactive_adminless_office(self, office_id="legacy"):
        self.repo.create_office(office_id, "Escritório legado")
        self.repo.db.execute("UPDATE offices SET active=0 WHERE id=?", (office_id,))
        self.repo.db.commit()

    def test_platform_status_toggles_adminless_office_without_changing_users_or_processes(self):
        self._make_inactive_adminless_office("local-legacy")
        for index in range(7):
            number = f"100023669202540{index:04d}"
            self.repo.db.execute("INSERT INTO watches(org,number,court,interval,created_at,next_due) VALUES(?,?,?,?,?,?)",
                ("local-legacy", number, "trf1", 86400, "2026-01-01", "2026-01-02"))
        self.repo.db.commit()
        user_counts = tuple(self.repo.db.execute("SELECT (SELECT count(*) FROM users), "
            "(SELECT count(*) FROM office_memberships)").fetchone())
        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                response = await client.patch("/api/platform/offices/local-legacy/status", json={"active": True},
                                              headers={"Origin": "http://test"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()["active"])
                self.assertEqual(set(response.json()), {"id", "name", "email", "phone", "address", "created_at", "active"})
                self.assertEqual((await client.patch("/api/platform/offices/local-legacy/status",
                    json={"active": False}, headers={"Origin": "http://test"})).status_code, 200)
                self.assertFalse(self.repo.office("local-legacy")["active"])
                self.assertEqual(tuple(self.repo.db.execute("SELECT (SELECT count(*) FROM users), "
                    "(SELECT count(*) FROM office_memberships)").fetchone()), user_counts)
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM watches WHERE org='local-legacy'").fetchone()[0], 7)
                self.assertTrue(self.repo.office("office-a")["active"])
                audit = self.repo.db.execute("SELECT action,actor,org,purpose FROM access_log "
                    "WHERE org='local-legacy' AND action IN ('platform_office_activate','platform_office_deactivate') "
                    "ORDER BY id").fetchall()
                self.assertEqual([row["action"] for row in audit],
                                 ["platform_office_activate", "platform_office_deactivate"])
                self.assertTrue(all(row["actor"] == "platform_admin:platform@example.test" and
                                    row["org"] == "local-legacy" for row in audit))
        asyncio.run(scenario())

    def test_platform_can_toggle_office_with_admin_and_status_requires_platform_admin(self):
        async def scenario():
            async with self.client() as anonymous:
                denied = await anonymous.patch("/api/platform/offices/office-a/status", json={"active": False},
                                               headers={"Origin": "http://test"})
                self.assertEqual(denied.status_code, 401)
            async with self.client() as office_admin:
                await office_admin.post("/api/auth/login", json={"email": "admin-a@example.test",
                    "password": "strong-office-password-123", "office_id": "office-a"})
                denied = await office_admin.patch("/api/platform/offices/office-a/status", json={"active": False},
                                                  headers={"Origin": "http://test"})
                self.assertEqual(denied.status_code, 401)
                self.assertTrue(self.repo.office("office-a")["active"])
            async with self.client() as platform:
                await platform.post("/api/platform/auth/login", json={"email": "platform@example.test",
                    "password": "strong-platform-password-123"})
                off = await platform.patch("/api/platform/offices/office-a/status", json={"active": False},
                                           headers={"Origin": "http://test"})
                self.assertEqual(off.status_code, 200, off.text)
                self.assertFalse(off.json()["active"])
                on = await platform.patch("/api/platform/offices/office-a/status", json={"active": True},
                                          headers={"Origin": "http://test"})
                self.assertEqual(on.status_code, 200, on.text)
                self.assertTrue(on.json()["active"])
        asyncio.run(scenario())

    def test_running_jobs_still_block_deactivation_without_requiring_office_admin(self):
        self._make_inactive_adminless_office("job-guard")
        self.repo.db.execute("UPDATE offices SET active=1 WHERE id='job-guard'")
        number = "10002366920254010001"
        self.repo.db.execute("INSERT INTO watches(org,number,court,interval,created_at,next_due) VALUES(?,?,?,?,?,?)",
            ("job-guard", number, "trf1", 86400, "2026-01-01", "2026-01-02"))
        self.repo.db.execute("""INSERT INTO collection_jobs
            (org,number,group_id,source,collection_type,stage_order,status,scheduled_at,created_at,updated_at,params)
            VALUES(?,?,?,?,?,?,'running',?,?,?,'{}')""",
            ("job-guard", number, "running-group", "datajud", "scheduled", 1,
             "2026-01-01", "2026-01-01", "2026-01-01"))
        monitor_id = self.repo.db.execute("""INSERT INTO oab_monitors
            (org,oab,uf,enabled,interval_hours,created_at,status)
            VALUES('job-guard','12345','SP',1,24,'2026-01-01','active')""").lastrowid
        self.repo.db.execute("""INSERT INTO oab_monitor_jobs
            (monitor_id,org,window_start,window_end,run_kind,status,scheduled_at,created_at,updated_at)
            VALUES(?,'job-guard','2026-01-01','2026-01-02','manual','running',
                   '2026-01-01','2026-01-01','2026-01-01')""", (monitor_id,))
        self.repo.db.commit()
        with self.assertRaisesRegex(ValueError, "jobs em andamento"):
            self.repo.set_office_active("job-guard", False)
        self.assertTrue(self.repo.office("job-guard")["active"])
        self.repo.db.execute("UPDATE collection_jobs SET status='completed' WHERE org='job-guard'")
        self.repo.db.commit()
        with self.assertRaisesRegex(ValueError, "jobs em andamento"):
            self.repo.set_office_active("job-guard", False)
        self.repo.db.execute("UPDATE oab_monitor_jobs SET status='completed' WHERE org='job-guard'")
        self.repo.db.commit()
        self.repo.set_office_active("job-guard", False)
        self.assertFalse(self.repo.office("job-guard")["active"])

    def test_workspace_deletion_does_not_delete_platform_admin_identity_or_session(self):
        token = "persistent-platform-session-token"
        session_id = self.repo.create_platform_admin_session(1, token,
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        self.repo.delete_office("office-a")
        self.assertIsNotNone(self.repo.platform_admin_session(token))
        self.assertIsNotNone(self.repo.db.execute("SELECT 1 FROM platform_admins WHERE id=1").fetchone())
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM watches WHERE org='local'").fetchone()[0], 1)
        self.assertTrue(session_id)

    def test_bootstrap_is_single_use_and_platform_password_is_not_an_office_user(self):
        with self.assertRaisesRegex(ValueError, "já foi inicializada"):
            self.repo.bootstrap_platform_admin("second@example.test", "another-platform-password-123")
        self.assertIsNone(self.repo.authenticate_user("platform@example.test", "strong-platform-password-123"))
        self.assertIsNotNone(self.repo.authenticate_platform_admin("platform@example.test", "strong-platform-password-123"))

    def test_repository_provisioning_rolls_back_office_when_first_admin_fails(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.repo.provision_office("rolled-back", "Rollback", "admin-a@example.test",
                                       "strong-new-password-123", full_name="Duplicate")
        self.assertIsNone(self.repo.office("rolled-back"))
        provisioned = self.repo.provision_office("provisioned", "Provisioned", "new@example.test",
            "strong-new-password-123", full_name="New Admin", office_email="contact@example.test")
        self.assertEqual(provisioned["office"]["email"], "contact@example.test")
        self.assertEqual(provisioned["administrator"]["role"], "office_admin")

    def test_platform_session_expires_and_logout_revokes_it(self):
        token = "short-lived-platform-session-token"
        self.repo.create_platform_admin_session(1, token,
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        self.assertIsNone(self.repo.platform_admin_session(token))

        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                response = await client.post("/api/platform/auth/logout", headers={"Origin": "http://test"})
                self.assertEqual(response.status_code, 200)
                row = self.repo.db.execute("SELECT actor,action FROM access_log WHERE action='platform_admin_logout'").fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["actor"], "platform_admin:platform@example.test")
        asyncio.run(scenario())

    def test_operational_error_sanitizer_redacts_credentials_and_authenticated_urls(self):
        samples = [
            "Authorization: Bearer highly-sensitive-token",
            "api_key=secret-value and password: plaintext",
            "request failed https://user:pass@example.test/path?token=abc&show=ok",
            '{"code":"failed","message":"pwsvc_secret-token"}',
            '{"context":{"Authorization":"Bearer nested-token","password":"strong-pass",'
            '"password_salt":"salt-value","helpful":"upstream returned 503"}}',
        ]
        for sample in samples:
            cleaned = str(sanitize_error(sample))
            for secret in ("highly-sensitive-token", "secret-value", "plaintext", "user:pass",
                           "token=abc", "pwsvc_secret-token"):
                self.assertNotIn(secret, cleaned)
        self.assertIn("show=ok", str(sanitize_error(samples[2])))
        nested = sanitize_error(samples[4])
        self.assertIn("upstream returned 503", nested)
        self.assertNotIn("nested-token", nested)
        self.assertNotIn("strong-pass", nested)
        self.assertNotIn("salt-value", nested)

    def test_failed_global_operations_are_audited(self):
        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                headers = {"Origin": "http://test"}
                status = await client.patch("/api/platform/offices/missing-office/status",
                                            json={"active": True}, headers=headers)
                retry = await client.post("/api/platform/jobs/collection/99999/retry",
                                          json={"purpose": "auditar tentativa operacional recusada"}, headers=headers)
                refresh = await client.post("/api/platform/offices/office-a/oab-monitors/99999/refresh",
                                            json={"purpose": "auditar atualização recusada"}, headers=headers)
                self.assertEqual(status.status_code, 404)
                self.assertEqual(retry.status_code, 404)
                self.assertEqual(refresh.status_code, 404)
                actions = {row[0] for row in self.repo.db.execute(
                    "SELECT action FROM access_log WHERE action LIKE 'platform_%_failed'")}
                self.assertIn("platform_office_status_failed", actions)
                self.assertIn("platform_collection_retry_failed", actions)
                self.assertIn("platform_oab_refresh_failed", actions)
        asyncio.run(scenario())

    def test_global_routes_are_registered_and_dashboard_uses_local_scripts(self):
        paths = self.app.openapi()["paths"]
        for path in ("/api/platform/auth/login", "/api/platform/auth/password",
                     "/api/platform/overview", "/api/platform/offices",
                     "/api/platform/users", "/api/platform/admins", "/api/platform/jobs", "/api/platform/integrations",
                     "/api/platform/jobs/{job_kind}/{job_id}",
                     "/api/platform/jobs/collection/{job_id}/retry",
                     "/api/platform/jobs/oab/{job_id}/retry",
                     "/api/platform/offices/{office_id}/oab-monitors/{monitor_id}/refresh",
                     "/api/platform/processes", "/api/platform/audit",
                     "/api/platform/admins/{admin_id}/password/reset",
                     "/api/platform/offices/{office_id}/users/{user_id}/recovery",
                     "/api/admin/users/{user_id}/recovery"):
            self.assertIn(path, paths)
        self.assertIn("post", paths["/api/platform/auth/password"])
        self.assertIn("post", paths["/api/platform/admins"])
        self.assertIn("get", paths["/api/platform/admins/{admin_id}"])
        self.assertIn("patch", paths["/api/platform/admins/{admin_id}"])
        self.assertIn("patch", paths["/api/platform/admins/{admin_id}/status"])
        self.assertIn("post", paths["/api/platform/admins/{admin_id}/sessions/revoke"])
        self.assertIn("post", paths["/api/platform/admins/{admin_id}/password/reset"])
        self.assertIn("post", paths["/api/platform/offices/{office_id}/users/{user_id}/recovery"])
        self.assertIn("post", paths["/api/admin/users/{user_id}/recovery"])
        self.assertIn("patch", paths["/api/platform/offices/{office_id}"])
        office_patch_ref = paths["/api/platform/offices/{office_id}"]["patch"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        self.assertEqual(office_patch_ref, "#/components/schemas/PlatformOfficeUpdateRequest")
        self.assertEqual(set(self.app.openapi()["components"]["schemas"]["PlatformOfficeUpdateRequest"]["properties"]),
                         {"name", "email", "phone", "address"})
        for path in ("/api/platform/offices/{office_id}/users",
                     "/api/platform/offices/{office_id}/users/{user_id}"):
            self.assertIn(path, paths)
        update_properties = paths["/api/platform/offices/{office_id}/users/{user_id}"]["patch"]["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        self.assertEqual(update_properties, "#/components/schemas/PlatformOfficeUserUpdateRequest")
        self.assertIn("/api/platform/offices/{office_id}/status", paths)
        schema = self.app.openapi()
        platform_cookie = {"PlatformAdminSession": []}
        self.assertEqual(schema["paths"]["/api/platform/auth/login"]["post"]["security"], [])
        for path, operations in schema["paths"].items():
            if path.startswith("/api/platform/") and path != "/api/platform/auth/login":
                for method in operations:
                    if method in {"get", "post", "put", "patch", "delete"}:
                        self.assertEqual(operations[method]["security"], [platform_cookie], (path, method))
        self.assertEqual(schema["components"]["securitySchemes"]["PlatformAdminSession"]["name"],
                         "processwatch_platform_session")
        self.assertNotIn("ServiceBearer", schema["paths"]["/api/platform/offices"]["get"]["security"][0])
        self.assertNotIn("ProcessWatchSession", schema["paths"]["/api/platform/offices"]["get"]["security"][0])
        self.assertEqual(schema["paths"]["/api/auth/login"]["post"]["security"], [])
        self.assertEqual(schema["paths"]["/api/auth/recovery/request"]["post"]["security"], [])
        self.assertEqual(schema["paths"]["/api/processes"]["get"]["security"],
                         [{"ServiceBearer": []}, {"ProcessWatchSession": []}])
        with tempfile.TemporaryDirectory(dir=Path(__file__).parents[1] / ".test-temp") as td:
            token_db = str(Path(td) / "token-mode.sqlite3")
            token_repo = SQLiteRepository(token_db, environment="test")
            token_app = create_app(repository=token_repo,
                config=Config(db=token_db, environment="test", auth_mode="token", access_token="legacy-test"))
            token_schema = token_app.openapi()
            self.assertEqual(token_schema["paths"]["/api/platform/auth/login"]["post"]["security"], [])
            self.assertEqual(token_schema["paths"]["/api/platform/offices"]["get"]["security"], [platform_cookie])
            self.assertEqual(token_schema["paths"]["/api/processes"]["get"]["security"],
                             [{"ServiceBearer": []}, {"LegacyAdminBearer": []}])
            self.assertEqual(token_schema["paths"]["/api/service-credentials"]["get"]["security"],
                             [{"LegacyAdminBearer": []}])
            token_repo.close()
        self.assertNotIn("/api/platform/offices/{office_id}/recover", paths)
        for path in ("/api/platform/processes", "/api/platform/processes/{office_id}/{number}"):
            self.assertNotIn("purpose", {parameter["name"] for parameter in paths[path]["get"].get("parameters", [])})
        web = Path(__file__).parents[1] / "src" / "processwatch" / "web"
        html = (web / "platform-admin.html").read_text(encoding="utf-8")
        self.assertIn('src="/assets/platform-admin.js"', html)
        self.assertNotIn("<script>", html)
        self.assertTrue((web / "platform-admin.js").is_file())
        self.assertIn('id="office-drawer"', html)
        self.assertIn('aria-modal="true"', html)
        script = (web / "platform-admin.js").read_text(encoding="utf-8")
        self.assertIn("openOfficeDrawer(button.dataset.officeShow)", script)
        self.assertIn("api(`/api/platform/offices/${encodeURIComponent(officeId)}`)", script)
        self.assertIn('api("/api/platform/processes?limit=100")', script)
        self.assertNotIn('name="purpose"', script)
        self.assertNotIn("Finalidade administrativa da consulta", script)
        self.assertIn("Justificativa da coleta administrativa", script)
        self.assertIn("Não envia convite nem e-mail", script)
        self.assertIn("A senha não será exibida novamente", script)
        css = (web / "platform-admin.css").read_text(encoding="utf-8")
        self.assertIn("[hidden]{display:none!important}", css)
        self.assertIn(".sidebar nav{display:flex;min-width:0;max-width:100%;width:100%;overflow:auto}", css)

    def test_local_bootstrap_cli_creates_only_global_identity_in_explicit_database(self):
        isolated_path = str(Path(self.temp.name) / "bootstrap.sqlite3")
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
             patch.dict("os.environ", {"PROCESSWATCH_ENV": "test", "PROCESSWATCH_PRIMARY_DB": self.db_path}), \
             patch("processwatch.api_cli.getpass.getpass", side_effect=["isolated-bootstrap-password-123",
                                                                         "isolated-bootstrap-password-123"]):
            self.assertEqual(cli_main(["platform-admin", "bootstrap", "--db", isolated_path,
                                       "--email", "global@example.test"]), 0)
        self.assertNotIn("isolated-bootstrap-password-123", output.getvalue())
        check = SQLiteRepository(isolated_path, environment="test", primary_path=self.db_path)
        try:
            self.assertEqual(check.db.execute("SELECT count(*) FROM platform_admins").fetchone()[0], 1)
            self.assertEqual(check.db.execute("SELECT count(*) FROM users").fetchone()[0], 0)
        finally:
            check.close()

    def test_global_job_list_unifies_office_and_oab_jobs_with_filters_and_pagination(self):
        self.repo.db.execute("INSERT INTO watches(org,number,court,interval,created_at,next_due) VALUES(?,?,?,?,?,?)",
            ("office-a", "20002366920254013305", "trf1", 86400, "2026-01-01", "2026-01-02"))
        self.repo.db.commit()
        self.repo.enqueue_collection_group("local", "00000003800004010000", "manual_refresh")
        self.repo.enqueue_collection_group("office-a", "20002366920254013305", "manual_refresh")
        monitor = self.repo.create_oab_monitor("office-a", "123456", "SP")
        oab_job = self.repo.enqueue_oab_monitor_job("office-a", monitor["id"], manual=True)

        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                page = await client.get("/api/platform/jobs", params={"office_id": "office-a",
                    "limit": 2, "offset": 0})
                self.assertEqual(page.status_code, 200, page.text)
                self.assertEqual(page.json()["total"], 4)
                self.assertEqual(len(page.json()["items"]), 2)
                self.assertTrue(all(item["org"] == "office-a" for item in page.json()["items"]))
                filtered = await client.get("/api/platform/jobs", params={"office_id": "office-a",
                    "number": "20002366920254013305", "collection_type": "manual_refresh",
                    "source": "DataJud", "job_kind": "collection",
                    "created_after": "2020-01-01T00:00:00Z",
                    "created_before": "2030-01-01T00:00:00Z"})
                self.assertEqual(filtered.status_code, 200, filtered.text)
                self.assertEqual(filtered.json()["total"], 1)
                self.assertEqual(filtered.json()["items"][0]["source"], "DataJud")
                oab = await client.get("/api/platform/jobs", params={"job_kind": "oab",
                    "office_id": "office-a", "status": "queued"})
                self.assertEqual(oab.status_code, 200, oab.text)
                self.assertEqual(oab.json()["total"], 1)
                self.assertEqual(oab.json()["items"][0]["id"], oab_job["id"])
                self.assertEqual(oab.json()["items"][0]["oab"], "123456")
        asyncio.run(scenario())

    def test_global_collection_job_detail_and_failed_retry_preserve_history_and_dedupe(self):
        group = self.repo.enqueue_collection_group("local", "00000003800004010000", "manual_refresh")
        original_ids = [item["id"] for item in group["jobs"]]
        failed_id = original_ids[0]
        self.repo.db.execute("UPDATE collection_jobs SET status='completed',completed_at=? WHERE group_id=?",
                             ("2026-10-01T00:00:00+00:00", group["group_id"]))
        self.repo.db.execute("UPDATE collection_jobs SET status='failed',error=? WHERE id=?",
                             ('{"code":"temporary","message":"sample failure"}', failed_id))
        self.repo.db.commit()

        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                detail = await client.get(f"/api/platform/jobs/collection/{failed_id}")
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertNotIn("lease_token", detail.json()["job"])
                self.assertEqual(len(detail.json()["group_jobs"]), 3)
                self.assertIn("process_runs", detail.json())
                retry = await client.post(f"/api/platform/jobs/collection/{failed_id}/retry",
                                          json={"purpose": "repetir coleta após falha transitória"},
                                          headers={"Origin": "http://test"})
                self.assertEqual(retry.status_code, 202, retry.text)
                self.assertEqual(retry.json()["status"], "scheduled")
                self.assertEqual(len(retry.json()["jobs"]), 2)
                second = await client.post(f"/api/platform/jobs/collection/{failed_id}/retry",
                                           json={"purpose": "repetir coleta após falha transitória"},
                                           headers={"Origin": "http://test"})
                self.assertEqual(second.status_code, 202, second.text)
                self.assertEqual(second.json()["status"], "already_queued")
                self.assertEqual(second.json()["group_id"], retry.json()["group_id"])
                originals = self.repo.db.execute("SELECT id,status FROM collection_jobs WHERE group_id=? ORDER BY id",
                                                  (group["group_id"],)).fetchall()
                self.assertEqual([row["id"] for row in originals], original_ids)
                self.assertEqual(originals[0]["status"], "failed")
                audit = self.repo.db.execute("SELECT actor,org,action,purpose FROM access_log "
                    "WHERE action='platform_collection_retry' ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual((audit["actor"], audit["org"]), ("platform_admin:platform@example.test", "local"))
                self.assertIn("repetir coleta", audit["purpose"])
        asyncio.run(scenario())

    def test_global_oab_refresh_retry_and_details_are_scoped_and_audited(self):
        monitor = self.repo.create_oab_monitor("office-a", "654321", "RJ", enabled=False)
        failed = self.repo.enqueue_oab_monitor_job("office-a", monitor["id"], manual=True)
        self.repo.db.execute("UPDATE oab_monitor_jobs SET status='failed',completed_at=?,error=? WHERE id=?",
                             ("2026-10-15T12:00:00+00:00", '{"message":"fixture failure"}', failed["id"]))
        self.repo.db.commit()

        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                detail = await client.get(f"/api/platform/jobs/oab/{failed['id']}")
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(detail.json()["job"]["oab"], "654321")
                self.assertEqual(len(detail.json()["monitor_history"]), 1)
                retry = await client.post(f"/api/platform/jobs/oab/{failed['id']}/retry",
                                          json={"purpose": "repetir consulta OAB após falha"},
                                          headers={"Origin": "http://test"})
                self.assertEqual(retry.status_code, 202, retry.text)
                self.assertNotEqual(retry.json()["job_id"], failed["id"])
                self.assertEqual(retry.json()["run_kind"], "manual")
                refresh = await client.post(f"/api/platform/offices/office-a/oab-monitors/{monitor['id']}/refresh",
                                            json={"purpose": "consulta manual solicitada pelo operador"},
                                            headers={"Origin": "http://test"})
                self.assertEqual(refresh.status_code, 202, refresh.text)
                self.assertEqual(refresh.json()["job_id"], retry.json()["job_id"])
                denied = await client.post(f"/api/platform/offices/office-b/oab-monitors/{monitor['id']}/refresh",
                                           json={"purpose": "escopo incorreto deve falhar"},
                                           headers={"Origin": "http://test"})
                self.assertEqual(denied.status_code, 404)
        asyncio.run(scenario())

    def test_platform_integrations_reports_unknown_worker_without_runtime_probe(self):
        checked_at = datetime.now(timezone.utc).isoformat()
        self.repo.db.execute("INSERT INTO watches(org,number,court,interval,created_at,next_due) VALUES(?,?,?,?,?,?)",
            ("office-a", "20002366920254013305", "trf1", 86400, checked_at, checked_at))
        self.repo.db.commit()
        self.repo.db.execute("""INSERT INTO source_checks(org,number,source,checked_at,status,error)
            VALUES('office-a','20002366920254013305','DataJud',?,'failed',?)""",
            (checked_at, "API key: hidden-secret-value"))
        self.repo.db.execute("""INSERT INTO discovery_runs
            (org,source,criterion,oab,uf,period_start,period_end,checked_at,status,pages,reported,matched,error)
            VALUES('office-a','DJEN','OAB/UF','654321','RJ','2026-10-01','2026-10-02',?,'error',1,0,0,?)""",
            (checked_at, "Bearer: hidden-oab-secret"))
        self.repo.db.commit()
        group = self.repo.enqueue_collection_group("local", "00000003800004010000", "manual_refresh")
        self.repo.db.execute("""INSERT INTO source_checks(org,number,source,checked_at,status,error)
            VALUES('local','00000003800004010000','PJe/TRF1',?,'failed',?)""",
            (checked_at, "credential=hidden-secret-value"))
        self.repo.db.execute("UPDATE collection_jobs SET status='failed',error=? WHERE group_id=? AND source='DataJud'",
                             ('{"code":"connector_error","message":"token=hidden-job-secret"}', group["group_id"]))
        self.repo.db.commit()
        async def scenario():
            async with self.client() as client:
                await client.post("/api/platform/auth/login", json={
                    "email": "platform@example.test", "password": "strong-platform-password-123"})
                response = await client.get("/api/platform/integrations")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["worker"]["state"], "unknown")
                self.assertFalse(response.json()["worker"]["telemetry_available"])
                inventory = {item["source"]: item for item in response.json()["sources"]}
                self.assertFalse(inventory["DataJud"]["configured"])
                self.assertTrue(inventory["DJEN"]["implemented"])
                jobs = await client.get("/api/platform/jobs", params={"office_id": "local"})
                self.assertEqual(jobs.status_code, 200)
                self.assertNotIn("hidden-job-secret", jobs.text)
                job_id = group["jobs"][0]["id"]
                job_detail = await client.get(f"/api/platform/jobs/collection/{job_id}")
                self.assertEqual(job_detail.status_code, 200, job_detail.text)
                self.assertNotIn("hidden-job-secret", job_detail.text)
                self.assertNotIn("hidden-secret-value", job_detail.text)
                oab_monitor = self.repo.create_oab_monitor("office-a", "654321", "RJ", enabled=False)
                oab_job = self.repo.enqueue_oab_monitor_job("office-a", oab_monitor["id"], manual=True)
                self.repo.db.execute("UPDATE oab_monitor_jobs SET status='failed',error=? WHERE id=?",
                                     ("Bearer: hidden-oab-secret", oab_job["id"]))
                self.repo.db.commit()
                oab_detail = await client.get(f"/api/platform/jobs/oab/{oab_job['id']}")
                self.assertEqual(oab_detail.status_code, 200, oab_detail.text)
                self.assertNotIn("hidden-oab-secret", oab_detail.text)
                datajud = next(item for item in response.json()["items"] if item["source"] == "DataJud")
                self.assertEqual(datajud["last_status"], "failed")
                self.assertIn("[redigido]", datajud["last_error"])
                djen = next(item for item in response.json()["items"] if item["source"] == "DJEN")
                self.assertEqual(djen["oab_discovery_runs"], 1)
                self.assertEqual(djen["last_status"], "error")
                self.assertNotIn("hidden-oab-secret", djen["last_error"])
                self.assertNotIn("hidden-job-secret", response.text)
                self.assertEqual(len(response.json()["job_failures"]), 1)
                overview = await client.get("/api/platform/overview")
                self.assertEqual(overview.status_code, 200, overview.text)
                self.assertIn("oab_jobs", overview.json())
                self.assertEqual(overview.json()["worker_telemetry"]["state"], "unknown")
        asyncio.run(scenario())

    def test_platform_jobs_cli_filters_and_details_use_global_endpoints(self):
        calls = []
        def fake_request(url, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return ({"items": [], "total": 0}, {})
        with patch.object(api_cli, "read_url", return_value="http://127.0.0.1:8000"), \
             patch.object(api_cli, "_session", return_value="platform-token"), \
             patch.object(api_cli, "request", side_effect=fake_request), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli_main(["platform-admin", "jobs", "--office-id", "office-a",
                                       "--status", "failed", "--limit", "5"]), 0)
            self.assertEqual(cli_main(["platform-admin", "jobs", "show", "collection", "41"]), 0)
            self.assertEqual(cli_main(["platform-admin", "integrations", "--office-id", "office-a"]), 0)
        self.assertIn("office_id=office-a", calls[0][1])
        self.assertIn("status=failed", calls[0][1])
        self.assertEqual(calls[1][1], "/api/platform/jobs/collection/41")
        self.assertEqual(calls[2][1], "/api/platform/integrations?office_id=office-a")


if __name__ == "__main__":
    unittest.main()
