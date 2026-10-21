"""Regression coverage for retiring paid providers from the product runtime."""
import asyncio
import importlib.util
import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from processwatch.config import Config
from processwatch.orchestrator import schedule_sync
from processwatch.repository import SQLiteRepository


NUMBER = "00000003800004010000"


class FreeSourcesOnlyTests(unittest.TestCase):
    def test_paid_provider_module_is_removed(self):
        self.assertIsNone(importlib.util.find_spec("processwatch.providers.escavador"))

    def test_paid_environment_values_are_ignored_by_config(self):
        with patch.dict(os.environ, {"PROCESSWATCH_ESCAVADOR_ENABLED": "1",
                                     "ESCAVADOR_API_TOKEN": "legacy-test-token"}, clear=True):
            config = Config.from_env()
        self.assertFalse(hasattr(config, "escavador_enabled"))
        self.assertFalse(hasattr(config, "escavador_api_token"))

    def test_legacy_paid_environment_options_are_absent_from_example(self):
        sample = Path(".env.example").read_text(encoding="utf-8")
        self.assertNotIn("ESCAVADOR", sample)
        self.assertNotIn("api.escavador.com", sample)

    def test_orchestrator_schedules_only_public_free_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteRepository(str(Path(directory) / "free.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 3600, 100)
                config = Config(db=str(Path(directory) / "free.sqlite3"),
                                djen_initial_date=date(2024, 10, 1))
                result = schedule_sync(repo, "local", NUMBER, config)
                self.assertTrue(result["jobs"])
                self.assertFalse(any("ESCAVADOR" in job["source"].upper() for job in result["jobs"]))
            finally:
                repo.close()

    def test_repository_rejects_retired_source_in_group(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteRepository(str(Path(directory) / "free.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 3600, 100)
                with self.assertRaisesRegex(ValueError, "Fonte judicial adicional"):
                    repo.enqueue_collection_group("local", NUMBER, "initial", include_datajud=False,
                        include_djen=False, additional_sources=["ESCAVADOR-API-V2"])
            finally:
                repo.close()

    def test_repository_rejects_retired_source_after_datajud_routing(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteRepository(str(Path(directory) / "free.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 3600, 100)
                with self.assertRaisesRegex(ValueError, "Fonte judicial adicional"):
                    repo.enqueue_group_source("local", NUMBER, "missing", "ESCAVADOR-API-V2")
            finally:
                repo.close()

    def test_legacy_queued_job_is_closed_without_importing_or_calling_provider(self):
        from processwatch.jobs import SQLiteCollectionJobRunner
        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteRepository(str(Path(directory) / "legacy.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 3600, 100)
                group = repo.enqueue_collection_group("local", NUMBER, "initial", include_datajud=False,
                    include_djen=False)
                with repo.db:
                    repo.db.execute("UPDATE collection_jobs SET source='ESCAVADOR-API-V2' WHERE id=?",
                                    (group["jobs"][0]["id"],))
                job = repo.claim_collection_job("local", "test-worker")
                result = SQLiteCollectionJobRunner(None, repo, None, Config()).run(job)
                persisted = next(item for item in repo.collection_jobs("local", NUMBER)
                                 if item["id"] == job["id"])
                self.assertEqual(result.status, "failed")
                self.assertEqual(persisted["status"], "failed")
                self.assertEqual(json.loads(persisted["error"])["code"], "source_retired")
            finally:
                repo.close()

    def test_api_has_no_commercial_status_or_public_data(self):
        import httpx
        from processwatch.api import create_app
        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteRepository(str(Path(directory) / "api.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 3600, 100)
                app = create_app(repo, config=Config(access_token="test"))
                async def check():
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization": "Bearer test"}) as client:
                        page = await client.get("/")
                        details = await client.get(f"/api/processos/{NUMBER}")
                        return page.text, details.json()
                html, data = asyncio.run(check())
                self.assertNotIn("Escavador", html)
                self.assertNotIn("Escavador", data.get("fontes", {}))
                self.assertNotIn("escavador_publico", data)
            finally:
                repo.close()

    def test_product_documentation_does_not_offer_paid_configuration(self):
        from pathlib import Path
        source = Path("README.md").read_text(encoding="utf-8")
        self.assertNotIn("PROCESSWATCH_ESCAVADOR_ENABLED=1", source)
        self.assertNotIn("ESCAVADOR_API_TOKEN", source)

    def test_no_commercial_api_root_is_importable_from_application_package(self):
        from pathlib import Path
        package = Path("src/processwatch")
        all_source = "\n".join(path.read_text(encoding="utf-8") for path in package.rglob("*.py"))
        self.assertNotIn("https://api.escavador.com", all_source)

    def test_old_paid_jobs_cannot_be_scheduled_even_when_legacy_env_is_set(self):
        with patch.dict(os.environ, {"PROCESSWATCH_ESCAVADOR_ENABLED": "1",
                                     "ESCAVADOR_API_TOKEN": "ignored-test-token"}):
            config = Config.from_env()
        self.assertFalse(hasattr(config, "escavador_enabled"))
        self.assertFalse(hasattr(config, "escavador_api_token"))


if __name__ == "__main__":
    unittest.main()

