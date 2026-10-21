import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.repository import SQLiteRepository


NUMBER = "00000003800004010000"


class NoopProvider:
    def fetch(self, number, court):
        return {"timed_out": False, "hits": {"total": {"value": 0}, "hits": []}}


class ServiceCredentialAndDocsTests(unittest.TestCase):
    def setUp(self):
        test_root = Path(__file__).parents[1] / ".test-temp"
        test_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=test_root)
        self.db = str(Path(self.temp.name) / "service.sqlite3")
        self.repo = SQLiteRepository(self.db)
        self.engine = Engine(self.repo, NoopProvider())

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def run_async(self, fn):
        asyncio.run(fn())

    def test_account_admin_creates_service_token_with_permissions_and_scope(self):
        self.repo.create_office("office-a", "Office A")
        self.repo.create_office("office-b", "Office B")
        self.repo.create_user("admin@example.test", "a-very-strong-password-123", "office-a", "office_admin")
        self.repo.create_user("lawyer@example.test", "a-very-strong-password-456", "office-a", "lawyer")
        self.engine.register(NUMBER, "office-a")
        self.engine.register("10002365820254053305", "office-b")
        app = create_app(self.repo, self.engine, config=Config(db=self.db, auth_mode="accounts"))

        async def scenario():
            admin = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
            login = await admin.post("/api/auth/login", json={
                "email": "admin@example.test", "password": "a-very-strong-password-123"})
            self.assertEqual(login.status_code, 200)
            account_openapi = (await admin.get("/openapi.json")).json()
            self.assertEqual(account_openapi["paths"]["/api/service-credentials"]["post"]["security"],
                             [{"ProcessWatchSession": []}])
            created = await admin.post("/api/service-credentials", headers={"Origin": "http://test"}, json={
                "name": "Read-only integration", "permissions": ["processes:read", "communications:read"],
                "expires_in_days": 30})
            self.assertEqual(created.status_code, 201, created.text)
            result = created.json()
            token = result["token"]
            credential_id = result["credential"]["id"]
            self.assertTrue(token.startswith("pwsvc_"))
            self.assertNotIn("token_hash", result["credential"])
            stored = self.repo.db.execute("SELECT token_hash,org FROM service_credentials WHERE id=?",
                                          (credential_id,)).fetchone()
            self.assertEqual(stored["token_hash"], hashlib.sha256(token.encode()).hexdigest())
            self.assertNotEqual(stored["token_hash"], token)
            self.assertEqual(stored["org"], "office-a")

            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                       headers={"Authorization": f"Bearer {token}"})
            processes = await client.get("/api/processes")
            self.assertEqual(processes.status_code, 200)
            self.assertEqual([item["number"] for item in processes.json()], [NUMBER])
            audit = self.repo.db.execute("SELECT actor,org,purpose FROM access_log WHERE action='service_api_request' ORDER BY id DESC LIMIT 1").fetchone()
            self.assertEqual(audit["actor"], f"service:{credential_id}")
            self.assertEqual(audit["org"], "office-a")
            self.assertIn("GET /api/processes", audit["purpose"])
            self.assertNotIn(token, audit["purpose"])
            self.assertEqual((await client.get("/api/communications")).status_code, 200)
            self.assertEqual((await client.get("/api/processes/overview")).status_code, 200)
            self.assertEqual((await client.get("/api/processes/10002365820254053305/sync")).status_code, 403)
            self.assertEqual((await client.post("/api/processes", json={"number": "00000003800004010000"})).status_code, 403)
            self.assertEqual((await client.patch("/api/communications/00000003800004010000/pub-1/read",
                json={"read": True})).status_code, 403)
            self.assertEqual((await client.post("/api/monitoramentos/oab", json={
                "oab": "12345", "uf": "SP"})).status_code, 403)
            self.assertEqual((await client.get("/api/processes/10002365820254053305/sync")).status_code, 403)
            self.assertIsNotNone(self.repo.db.execute(
                "SELECT last_used_at FROM service_credentials WHERE id=?", (credential_id,)).fetchone()[0])
            self.assertEqual((await client.get("/api/service-credentials")).status_code, 403)
            self.assertEqual((await client.get("/api/office")).status_code, 403)
            self.assertEqual((await client.put("/api/office", json={
                "name": "Unauthorized"})).status_code, 403)
            self.assertEqual((await client.get("/api/admin/users")).status_code, 403)

            lawyer = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
            self.assertEqual((await lawyer.post("/api/auth/login", json={
                "email": "lawyer@example.test", "password": "a-very-strong-password-456"})).status_code, 200)
            self.assertEqual((await lawyer.get("/api/service-credentials")).status_code, 403)
            self.assertEqual((await lawyer.post("/api/service-credentials",
                headers={"Origin": "http://test"}, json={"name": "Denied",
                "permissions": ["processes:read"]})).status_code, 403)
            self.assertEqual((await admin.post("/api/service-credentials", json={
                "name": "No CSRF", "permissions": ["processes:read"]})).status_code, 403)
            self.assertEqual((await admin.post("/api/service-credentials",
                headers={"Origin": "http://test"}, json={
                "name": "Invalid permission", "permissions": ["processes:write"]})).status_code, 400)

            listed = await admin.get("/api/service-credentials")
            self.assertEqual(listed.status_code, 200)
            self.assertNotIn("token", listed.text)
            revoked = await admin.post(f"/api/service-credentials/{credential_id}/revoke",
                                       headers={"Origin": "http://test"})
            self.assertEqual(revoked.status_code, 200)
            self.assertEqual((await client.get("/api/processes")).status_code, 401)
            await client.aclose()
            await lawyer.aclose()
            await admin.aclose()
        self.run_async(scenario)

    def test_service_token_works_with_token_mode_legacy_admin_and_rotation(self):
        self.engine.register(NUMBER, "local")
        config = Config(db=self.db, auth_mode="token", access_token="legacy-admin-token")
        app = create_app(self.repo, self.engine, config=config)

        async def scenario():
            admin = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"Authorization": "Bearer legacy-admin-token"})
            created = await admin.post("/api/service-credentials", json={"name": "Worker client",
                "permissions": ["jobs:read"], "expires_in_days": 7})
            self.assertEqual(created.status_code, 201, created.text)
            old_token = created.json()["token"]
            credential_id = created.json()["credential"]["id"]
            self.assertEqual((await admin.get("/api/service-credentials")).status_code, 200)
            openapi = (await admin.get("/openapi.json")).json()
            self.assertEqual(openapi["paths"]["/api/service-credentials"]["post"]["security"],
                             [{"LegacyAdminBearer": []}])
            service = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"Authorization": f"Bearer {old_token}"})
            self.assertEqual((await service.get("/api/scheduler/status")).status_code, 200)
            self.assertEqual((await service.get("/api/processes")).status_code, 403)
            rotated = await admin.post(f"/api/service-credentials/{credential_id}/rotate", json={"expires_in_days": 10})
            self.assertEqual(rotated.status_code, 200, rotated.text)
            new_token = rotated.json()["token"]
            self.assertNotEqual(new_token, old_token)
            self.assertEqual((await service.get("/api/scheduler/status")).status_code, 401)
            new_service = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"Authorization": f"Bearer {new_token}"})
            self.assertEqual((await new_service.get("/api/scheduler/status")).status_code, 200)
            await service.aclose()
            await new_service.aclose()
            await admin.aclose()
        self.run_async(scenario)

    def test_expired_service_token_is_rejected(self):
        app = create_app(self.repo, self.engine, config=Config(db=self.db, access_token="legacy"))
        metadata, token = self.repo.create_service_credential("local", "Expiring", ["processes:read"],
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        self.repo.db.execute("UPDATE service_credentials SET expires_at=? WHERE id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), metadata["id"]))
        self.repo.db.commit()

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/processes", headers={"Authorization": f"Bearer {token}"})
                self.assertEqual(response.status_code, 401)
        self.run_async(scenario)

    def test_docs_and_redoc_use_only_local_resources_and_openapi_declares_auth(self):
        app = create_app(self.repo, self.engine, config=Config(db=self.db, access_token="doc-token"))

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                docs = await client.get("/docs")
                redoc = await client.get("/redoc")
                home = await client.get("/")
                app_script = await client.get("/assets/app.js")
                self.assertEqual(home.status_code, 200)
                self.assertIn('<script src="/assets/app.js" defer></script>', home.text)
                self.assertEqual(app_script.status_code, 200)
                self.assertIn("initializeAccess();", app_script.text)
                self.assertIn("script-src 'self'", app_script.headers.get("content-security-policy", ""))
                self.assertIn("application/javascript", app_script.headers.get("content-type", ""))
                self.assertEqual(docs.status_code, 200)
                self.assertIn("/assets/docs/swagger-ui/swagger-ui-bundle.js", docs.text)
                self.assertIn("/assets/docs/swagger-ui/swagger-initialize.js", docs.text)
                self.assertIn('<link rel="icon" href="/assets/logo.png">', docs.text)
                self.assertNotIn("<script>", docs.text.casefold())
                self.assertNotIn("cdn.", docs.text.casefold())
                self.assertEqual(redoc.status_code, 200)
                self.assertIn("/assets/docs/redoc/redoc.standalone.js", redoc.text)
                self.assertIn('<link rel="icon" href="/assets/logo.png">', redoc.text)
                self.assertNotIn("cdn.", redoc.text.casefold())
                for asset in ("swagger-ui/swagger-ui.css", "swagger-ui/swagger-ui-bundle.js",
                              "swagger-ui/swagger-ui-standalone-preset.js", "swagger-ui/swagger-initialize.js",
                              "redoc/redoc.standalone.js", "redoc/logo-mini.svg", "docs.css"):
                    response = await client.get("/assets/docs/" + asset)
                    self.assertEqual(response.status_code, 200, asset)
                    self.assertGreater(len(response.content), 10, asset)
                redoc_bundle = await client.get("/assets/docs/redoc/redoc.standalone.js")
                self.assertNotIn(b"cdn.redoc.ly", redoc_bundle.content)
                schema = await client.get("/openapi.json")
                self.assertEqual(schema.status_code, 200)
                self.assertIn("script-src 'self'", schema.headers.get("content-security-policy", ""))
                payload = schema.json()
                self.assertEqual(payload["components"]["securitySchemes"]["ServiceBearer"]["scheme"], "bearer")
                self.assertIn("/api/monitoramentos/oab", payload["paths"])
                operation = payload["paths"]["/api/monitoramentos/oab"]["post"]
                self.assertTrue(any("ServiceBearer" in item for item in operation["security"]))
                self.assertEqual(operation["x-processwatch-required-permissions"], ["oab:manage"])
                admin_operation = payload["paths"]["/api/service-credentials"]["post"]
                self.assertEqual(admin_operation["security"], [{"LegacyAdminBearer": []}])
                self.assertEqual(payload["paths"]["/api/office"]["get"]["summary"],
                                 "Consultar dados do escritório ativo")
                self.assertIn("Somente administradores", admin_operation["description"])
        self.run_async(scenario)

    def test_early_auth_failure_keeps_security_headers(self):
        app = create_app(self.repo, self.engine, config=Config(db=self.db, access_token="expected"))

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                response = await client.get("/api/processes")
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["x-frame-options"], "DENY")
                self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.run_async(scenario)


if __name__ == "__main__":
    unittest.main()
