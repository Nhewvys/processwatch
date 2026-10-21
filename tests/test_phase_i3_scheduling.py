import asyncio
import copy
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.orchestrator import schedule_sync
from processwatch.providers.djen import DjenFetch
from processwatch.providers.contracts import ConnectorResult, ConnectorState
from processwatch.repository import SQLiteRepository
from processwatch.worker import run_collection_cycle, watch_forever
from processwatch.providers.djen import DjenClient


NUMBER = "00000003800004010000"
AUTH = {"Authorization": "Bearer schedule-test"}


def response(movements=None):
    source = {
        "id": "schedule-fixture", "numeroProcesso": NUMBER, "tribunal": "TRF1",
        "grau": "JE", "nivelSigilo": 0,
        "sistema": {"codigo": 0, "nome": "Sistema não verificado"},
        "classe": {"codigo": 436, "nome": "Procedimento"},
        "orgaoJulgador": {"codigo": 1, "nome": "Vara"}, "assuntos": [],
        "dataAjuizamento": "20250115000000",
        "dataHoraUltimaAtualizacao": "2025-02-01T12:00:00Z",
        "movimentos": movements if movements is not None else [
            {"codigo": 26, "dataHora": "2025-01-15T12:00:00Z", "nome": "Distribuição",
             "complementosTabelados": []}
        ],
    }
    return {"timed_out": False, "hits": {"total": {"value": 1, "relation": "eq"},
            "hits": [{"_id": "schedule-fixture", "_source": source}]}}


class FakeDataJud:
    def __init__(self):
        self.response = response()

    def fetch(self, number, court):
        return copy.deepcopy(self.response)


class EmptyDjen:
    async def fetch(self, number, start, end):
        return DjenFetch([], start.isoformat(), end.isoformat(), True)


class SuccessfulPjeFixture:
    """Deterministic provider: exercise the recurring PJe queue without network I/O."""
    async def collect(self, request):
        return ConnectorResult(request.source, ConnectorState.OPERATIONAL, found=True,
            data={"participants": [], "movements": [], "documents": [], "hearings": []},
            coverage={"movement_count": 0, "participants": 0})


class PhaseI3SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=".test-temp")
        self.path = str(Path(self.temp.name) / "i3.sqlite3")
        self.repo = SQLiteRepository(self.path)
        self.provider = FakeDataJud()
        self.engine = Engine(self.repo, self.provider)
        self.engine.register(NUMBER)
        self.config = Config(db=self.path, api_key="fixture",
                             djen_initial_date=date(2024, 10, 1), djen_min_interval=0,
                             min_interval=0, court_min_interval=0)

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def _activate_due(self, when=None):
        self.repo.configure_monitor_schedule("local", NUMBER, enabled=True, interval_hours=24)
        due = when or datetime.now(timezone.utc)
        self.repo.db.execute("UPDATE monitor_schedules SET next_run_at=? WHERE org=? AND number=?",
                             ((due - timedelta(seconds=1)).isoformat(), "local", NUMBER))
        self.repo.db.commit()
        return due

    def test_new_and_existing_process_schedules_are_disabled_by_default(self):
        schedule = self.repo.monitor_schedule("local", NUMBER)
        self.assertFalse(schedule["enabled"])
        self.assertEqual(schedule["interval_hours"], 24)
        self.assertIsNone(schedule["next_run_at"])
        self.assertEqual(schedule["status"], "paused")
        self.assertEqual(self.repo.db.execute("PRAGMA user_version").fetchone()[0], 18)

    def test_activation_pause_and_frequency_change_are_persisted(self):
        active = self.repo.configure_monitor_schedule("local", NUMBER, enabled=True, interval_hours=6)
        self.assertTrue(active["enabled"])
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["interval_hours"], 6)
        self.assertGreater(datetime.fromisoformat(active["next_run_at"]), datetime.now(timezone.utc))
        paused = self.repo.configure_monitor_schedule("local", NUMBER, enabled=False, interval_hours=48)
        self.assertFalse(paused["enabled"])
        self.assertEqual(paused["status"], "paused")
        self.assertIsNone(paused["next_run_at"])
        self.assertEqual(paused["interval_hours"], 48)

    def test_rejects_frequency_outside_conservative_choices(self):
        with self.assertRaises(ValueError):
            self.repo.configure_monitor_schedule("local", NUMBER, enabled=True, interval_hours=1)

    def test_due_schedule_creates_datajud_djen_and_consolidation_once(self):
        due = self._activate_due()
        first = self.repo.enqueue_due_monitor_schedules("local", now_at=due)
        second = self.repo.enqueue_due_monitor_schedules("local", now_at=due)
        self.assertEqual(first[0]["jobs_created"], 3)
        self.assertEqual(second, [])
        jobs = self.repo.scheduled_job_group("local", NUMBER)
        self.assertEqual([job["source"] for job in jobs], ["DataJud", "DJEN", "Consolidation"])
        self.assertTrue(all(job["collection_type"] == "scheduled_refresh" for job in jobs))
        schedule = self.repo.monitor_schedule("local", NUMBER)
        self.assertEqual(schedule["status"], "queued")
        self.assertGreater(datetime.fromisoformat(schedule["next_run_at"]), due)

    def test_not_due_schedule_creates_no_job(self):
        self.repo.configure_monitor_schedule("local", NUMBER, enabled=True, interval_hours=48)
        self.assertEqual(self.repo.enqueue_due_monitor_schedules("local"), [])
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM collection_jobs").fetchone()[0], 0)

    def test_existing_manual_job_is_coalesced_instead_of_duplicated(self):
        due = self._activate_due()
        schedule_sync(self.repo, "local", NUMBER, self.config, collection_type="manual_refresh", force=True)
        result = self.repo.enqueue_due_monitor_schedules("local", now_at=due)
        self.assertEqual(result[0]["status"], "coalesced")
        self.assertEqual(result[0]["jobs_created"], 0)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM collection_jobs WHERE collection_type='scheduled_refresh'").fetchone()[0], 0)

    def test_two_scheduler_connections_atomically_create_only_one_cycle(self):
        due = self._activate_due()

        def tick(_):
            other = SQLiteRepository(self.path)
            try:
                return other.enqueue_due_monitor_schedules("local", now_at=due)
            finally:
                other.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(tick, (1, 2)))
        self.assertEqual(sum(bool(result) for result in results), 1)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM collection_jobs WHERE collection_type='scheduled_refresh'").fetchone()[0], 3)

    def test_global_worker_slot_prevents_concurrent_process_jobs(self):
        self.repo.register("other", NUMBER, "trf1", 86400, 100)
        schedule_sync(self.repo, "local", NUMBER, self.config)
        schedule_sync(self.repo, "other", NUMBER, self.config)
        first = self.repo.claim_collection_job("local", "worker-a")
        self.assertIsNotNone(first)
        self.assertIsNone(self.repo.claim_collection_job("other", "worker-b"))

    def test_active_job_prevents_equivalent_second_job_after_restart(self):
        due = self._activate_due()
        self.repo.enqueue_due_monitor_schedules("local", now_at=due)
        self.repo.close()
        self.repo = SQLiteRepository(self.path)
        again = self.repo.enqueue_due_monitor_schedules("local", now_at=due)
        self.assertEqual(again, [])
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM collection_jobs WHERE collection_type='scheduled_refresh'").fetchone()[0], 3)
        self.assertTrue(self.repo.monitor_schedule("local", NUMBER)["enabled"])

    def test_authenticated_api_configures_frequency_and_reports_real_worker_state(self):
        async def scenario():
            runtime = {"running": False}
            app = create_app(self.repo, self.engine, config=Config(access_token="schedule-test"),
                             scheduler_status_provider=lambda: runtime["running"])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                self.assertEqual((await client.get(f"/api/processes/{NUMBER}/schedule")).status_code, 401)
                self.assertEqual((await client.get(f"/api/processes/{NUMBER}/schedule", headers=AUTH)).status_code, 200)
                missing = await client.put("/api/processes/10002366620254013306/schedule",
                    json={"enabled": True, "interval_hours": 24}, headers=AUTH)
                self.assertEqual(missing.status_code, 404)
                invalid = await client.put(f"/api/processes/{NUMBER}/schedule",
                    json={"enabled": True, "interval_hours": 7}, headers=AUTH)
                self.assertEqual(invalid.status_code, 400)
                changed = await client.put(f"/api/processes/{NUMBER}/schedule",
                    json={"enabled": True, "interval_hours": 12}, headers=AUTH)
                self.assertEqual(changed.status_code, 200)
                self.assertTrue(changed.json()["enabled"])
                self.assertEqual(changed.json()["interval_hours"], 12)
                self.assertFalse(changed.json()["scheduler_running"])
                status = await client.get("/api/scheduler/status", headers=AUTH)
                self.assertEqual(status.json()["enabled_processes"], 1)
                self.assertFalse(status.json()["worker_running"])
                page = await client.get("/", headers=AUTH)
                self.assertIn('id="schedule-save"', page.text)
                self.assertIn('id="schedule-hours"', page.text)
                runtime["running"] = True
                health = await client.get("/api/health")
                self.assertTrue(health.json()["scheduler_active"])
        asyncio.run(scenario())

    def test_end_to_end_worker_persists_schedule_and_marks_new_event_unread(self):
        schedule_sync(self.repo, "local", NUMBER, self.config, collection_type="initial")
        run_collection_cycle(self.engine, self.repo, EmptyDjen(), self.config)
        self.assertEqual(self.repo.unread_summary("local", NUMBER)["unread_count"], 0)

        # The process now has explicit PJe system metadata. The scheduled cycle
        # must enqueue that applicable source; the fixture keeps this test offline.
        pje_metadata = json.loads(self.repo.get("local", NUMBER)["metadata"])
        for item in pje_metadata:
            item["sistema"] = {"codigo": 1, "nome": "PJe"}
        self.repo.db.execute("UPDATE watches SET metadata=? WHERE org=? AND number=?",
                             (json.dumps(pje_metadata), "local", NUMBER))
        self.repo.db.commit()
        source = self.provider.response["hits"]["hits"][0]["_source"]
        source["sistema"] = {"codigo": 1, "nome": "PJe"}
        source["movimentos"].append({"codigo": 92, "dataHora": "2026-10-11T12:00:00Z",
            "nome": "Publicação de sentença", "complementosTabelados": []})
        async def activate_via_api():
            app = create_app(self.repo, self.engine,
                config=Config(db=self.path, access_token="schedule-test"), scheduler_active=False)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                result = await client.put(f"/api/processes/{NUMBER}/schedule",
                    json={"enabled": True, "interval_hours": 24}, headers=AUTH)
                self.assertEqual(result.status_code, 200)
        asyncio.run(activate_via_api())
        due = datetime.now(timezone.utc)
        self.repo.db.execute("UPDATE monitor_schedules SET next_run_at=? WHERE org=? AND number=?",
                             ((due-timedelta(seconds=1)).isoformat(), "local", NUMBER))
        self.repo.db.commit()

        stop = Event()
        completed = []
        def worker_loop():
            worker_repo = SQLiteRepository(self.path)
            try:
                watch_forever(Engine(worker_repo, self.provider), worker_repo, "local", poll=0.02,
                    stop=stop, djen_client=EmptyDjen(), config=self.config, queue_mode=True,
                    pje_client=SuccessfulPjeFixture())
            except BaseException as exc:
                completed.append(exc)
            finally:
                worker_repo.close()
        thread = Thread(target=worker_loop, daemon=True)
        thread.start()
        deadline = time.monotonic() + 8
        try:
            while time.monotonic() < deadline:
                current = self.repo.monitor_schedule("local", NUMBER)
                if current["status"] in ("completed", "failed", "partial") and current["last_execution_at"]:
                    break
                time.sleep(0.02)
        finally:
            stop.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(completed, [])
        results = self.repo.scheduled_job_group("local", NUMBER)
        self.assertEqual({job["source"] for job in results},
                         {"DataJud", "DJEN", "Consolidation", "TRF1-PJE-JE"})
        self.assertTrue(all(job["status"] == "completed" for job in results))
        self.assertEqual(self.repo.unread_summary("local", NUMBER)["unread_count"], 1)
        schedule = self.repo.monitor_schedule("local", NUMBER)
        self.assertEqual(schedule["status"], "completed")
        self.assertIsNotNone(schedule["last_scheduled_at"])
        self.assertIsNotNone(schedule["last_execution_at"])
        self.assertGreater(datetime.fromisoformat(schedule["next_run_at"]), due)
        self.assertTrue(any(job["source"] == "TRF1-PJE-JE" and job["status"] == "completed"
                            for job in self.repo.collection_jobs("local", NUMBER,
                                group_id=schedule["last_group_id"])))
        self.repo.close()
        self.repo = SQLiteRepository(self.path)
        persisted = self.repo.monitor_schedule("local", NUMBER)
        self.assertTrue(persisted["enabled"])
        self.assertEqual(self.repo.unread_summary("local", NUMBER)["unread_count"], 1)
        self.assertEqual(self.repo.enqueue_due_monitor_schedules("local"), [])

    def test_djen_honors_retry_after_with_bounded_request_retries(self):
        class FakeClient:
            def __init__(self):
                self.calls = 0

            async def get(self, url, **kwargs):
                self.calls += 1
                request = httpx.Request("GET", url)
                if self.calls == 1:
                    return httpx.Response(429, headers={"Retry-After": "3"}, request=request)
                return httpx.Response(200, json={"items": []}, request=request)

        async def scenario():
            sleeps = []
            transport = FakeClient()
            async def sleep(delay):
                sleeps.append(delay)
            client = DjenClient(transport, min_interval=0, sleep=sleep)
            result = await client._get(transport, {"numeroProcesso": NUMBER})
            self.assertEqual(result, {"items": []})
            self.assertEqual(transport.calls, 2)
            self.assertIn(3.0, sleeps)
        asyncio.run(scenario())

    def test_schema_migration_is_additive_and_keeps_existing_records(self):
        before = self.repo.db.execute("SELECT count(*) FROM watches").fetchone()[0]
        self.repo.db.execute("PRAGMA user_version=9")
        self.repo.db.commit()
        self.repo.close()
        self.repo = SQLiteRepository(self.path)
        self.assertEqual(self.repo.db.execute("PRAGMA user_version").fetchone()[0], 18)
        self.assertEqual(self.repo.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.repo.db.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM watches").fetchone()[0], before)
        self.assertFalse(self.repo.monitor_schedule("local", NUMBER)["enabled"])


if __name__ == "__main__":
    unittest.main()


