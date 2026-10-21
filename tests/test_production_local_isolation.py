import asyncio
import selectors
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from processwatch.api import create_app
from processwatch.cnj import normalize
from processwatch.config import Config
from processwatch.cli import main as cli_main
from processwatch.engine import Engine
from processwatch.orchestrator import schedule_sync
from processwatch.providers.datajud import DataJud
from processwatch.repository import SQLiteRepository
from processwatch.worker import start_background_worker, watch_forever


CNJ = "0000000-38.0000.4.01.0000"


class _RestrictedSocketSelector(selectors.SelectSelector):
    def select(self, timeout=None):
        if not self.get_map():
            if timeout:
                time.sleep(timeout)
            return []
        return super().select(timeout)


class _RestrictedSocketSelectorLoop(asyncio.SelectorEventLoop):
    """Run in-process ASGI tests where the Windows sandbox blocks socketpair."""

    def __init__(self):
        super().__init__(_RestrictedSocketSelector())

    def _make_self_pipe(self):
        self._ssock = self._csock = None

    def _write_to_self(self):
        pass

    def _read_from_self(self):
        pass

    def _close_self_pipe(self):
        pass


class _RestrictedSocketEventLoopPolicy(asyncio.DefaultEventLoopPolicy):
    _loop_factory = _RestrictedSocketSelectorLoop


class ProductionLocalIsolationTests(unittest.TestCase):
    def setUp(self):
        self._old_loop_policy = asyncio.get_event_loop_policy()
        asyncio.set_event_loop_policy(_RestrictedSocketEventLoopPolicy())
        temp_root = Path.cwd() / ".test-temp"
        temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=temp_root)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "processwatch.sqlite3"
        self.production = Config(environment="production", auth_mode="accounts",
                                 session_cookie_secure=True,
                                 public_origin="https://processwatch.example.test",
                                 db=str(self.db_path), primary_db=str(self.db_path))

    def tearDown(self):
        asyncio.set_event_loop_policy(self._old_loop_policy)
        self.temp.cleanup()

    def test_production_configuration_requires_accounts_and_secure_https(self):
        with self.assertRaisesRegex(ValueError, "accounts"):
            Config(environment="production", auth_mode="token")
        with self.assertRaisesRegex(ValueError, "SESSION_COOKIE_SECURE"):
            Config(environment="production", auth_mode="accounts", public_origin="https://example.test")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            Config(environment="production", auth_mode="accounts", session_cookie_secure=True,
                   public_origin="http://example.test")

    def test_production_requires_absolute_persistent_database_path(self):
        with self.assertRaisesRegex(ValueError, "absoluto"):
            SQLiteRepository("relative.sqlite3", environment="production",
                             primary_path=str(self.root / "primary.sqlite3"))
        with self.assertRaisesRegex(ValueError, "persistente"):
            SQLiteRepository(":memory:", environment="production")

    def test_clean_production_database_is_v18_and_does_not_seed_local(self):
        repo = SQLiteRepository(str(self.db_path), environment="production",
                                primary_path=str(self.db_path))
        try:
            self.assertEqual(repo.db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertIsNone(repo.office("local"))
            self.assertEqual(repo.list_offices(), [])
        finally:
            repo.close()

    def test_local_mode_retains_legacy_office_and_registration(self):
        repo = SQLiteRepository(":memory:", environment="development")
        try:
            self.assertIsNotNone(repo.office("local"))
            Engine(repo, DataJud(Config())).register(CNJ, "local")
            self.assertIsNotNone(repo.get("local", normalize(CNJ)))
        finally:
            repo.close()

    def test_production_cli_refuses_local_before_opening_database(self):
        with patch("processwatch.cli.Config.from_env", return_value=self.production), \
             patch("processwatch.cli.SQLiteRepository") as repository:
            self.assertEqual(cli_main(["--org", "local", "add", CNJ]), 1)
            repository.assert_not_called()

    def test_legacy_local_login_service_api_platform_api_and_worker_are_blocked(self):
        # Build a realistic legacy database in the local profile, then reopen it
        # under production policy. No operational database is used by this test.
        legacy = SQLiteRepository(str(self.db_path), environment="principal",
                                  primary_path=str(self.root / "other-primary.sqlite3"))
        legacy.create_office("alpha", "Alpha")
        local_user = legacy.create_user("local@example.test", "Local-password-9831!",
                                        "local", role="office_admin")
        alpha_user = legacy.create_user("alpha@example.test", "Alpha-password-9831!",
                                        "alpha", role="office_admin")
        normalized = normalize(CNJ)
        Engine(legacy, DataJud(Config())).register(CNJ, "local")
        Engine(legacy, DataJud(Config())).register(CNJ, "alpha")
        schedule_sync(legacy, "local", normalized, Config())
        oab_due = datetime.now(timezone.utc) - timedelta(minutes=1)
        legacy.create_oab_monitor("local", "12345", "SP", now_at=oab_due)
        legacy.enqueue_due_oab_monitor_jobs("local", now_at=oab_due)
        _, local_service_token = legacy.create_service_credential(
            "local", "legacy-local", ["processes:read"],
            (datetime.now(timezone.utc) + timedelta(days=1)).isoformat())
        owner = legacy.bootstrap_platform_admin("owner@example.test", "Owner-password-9831!")
        platform_session = "platform-session-test-value"
        legacy.create_platform_admin_session(owner["id"], platform_session,
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        local_session = "office-session-test-value"
        legacy.create_auth_session(local_user["id"], local_session, "local",
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        alpha_session = "alpha-session-test-value"
        legacy.create_auth_session(alpha_user["id"], alpha_session, "alpha",
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat())
        legacy.close()

        repo = SQLiteRepository(str(self.db_path), environment="production",
                                primary_path=str(self.db_path))
        try:
            app = create_app(repo, config=self.production)
            async def exercise_api():
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://processwatch.example.test",
                ) as client:
                    login = await client.post("/api/auth/login", json={
                        "email": "local@example.test", "password": "Local-password-9831!"})
                    self.assertEqual(login.status_code, 403)

                    local_session_response = await client.get("/api/processes", cookies={
                        "processwatch_session": local_session})
                    self.assertEqual(local_session_response.status_code, 403)

                    local_service_response = await client.get("/api/processes", headers={
                        "Authorization": f"Bearer {local_service_token}"})
                    self.assertEqual(local_service_response.status_code, 403)
                    legacy_bearer = await client.get("/api/processes", headers={
                        "Authorization": "Bearer development-legacy-token"})
                    self.assertEqual(legacy_bearer.status_code, 401)

                    alpha_response = await client.get("/api/processes", cookies={
                        "processwatch_session": alpha_session})
                    self.assertEqual(alpha_response.status_code, 200)
                    self.assertEqual([item["number"] for item in alpha_response.json()], [normalized])

                    global_cookie = {"processwatch_platform_session": platform_session}
                    filtered = await client.get("/api/platform/processes", cookies=global_cookie)
                    self.assertEqual(filtered.status_code, 200)
                    self.assertNotIn("local", {item["org"] for item in filtered.json()["items"]})
                    explicit = await client.get("/api/platform/processes?office_id=local",
                                                cookies=global_cookie)
                    self.assertEqual(explicit.status_code, 404)

                    jobs = await client.get("/api/platform/jobs", cookies=global_cookie)
                    self.assertEqual(jobs.status_code, 200)
                    self.assertNotIn("local", {item["org"] for item in jobs.json()["items"]})

            asyncio.run(exercise_api())

            claimed = repo.claim_collection_job(None, "production-worker")
            self.assertIsNone(claimed)
            self.assertTrue(repo.collection_jobs("local", normalized))
            self.assertIsNone(repo.claim_oab_monitor_job(None))
            self.assertEqual(repo.enqueue_due_oab_monitor_jobs(None), [])
            self.assertTrue(repo.db.execute(
                "SELECT 1 FROM oab_monitor_jobs WHERE org='local' AND status='queued'").fetchone())
            with self.assertRaisesRegex(ValueError, "escritório reservado"):
                Engine(repo, DataJud(self.production)).register(CNJ, "local")
            with self.assertRaisesRegex(ValueError, "escritório reservado"):
                schedule_sync(repo, "local", normalized, self.production)
            with self.assertRaisesRegex(ValueError, "escritório reservado"):
                repo.create_oab_monitor("local", "99999", "SP")
            with self.assertRaisesRegex(ValueError, "escritório reservado"):
                start_background_worker(str(self.db_path), self.production, org="local")
            with self.assertRaisesRegex(ValueError, "escritório reservado"):
                watch_forever(Engine(repo, DataJud(self.production)), repo, "local", config=self.production)
        finally:
            repo.close()


if __name__ == "__main__":
    unittest.main()
