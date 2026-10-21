"""Durable queue, scheduling and worker integration tests (external APIs mocked)."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import tempfile
import unittest
import json
from pathlib import Path

from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.jobs import SQLiteCollectionJobRunner, execute_collection_job
from processwatch.orchestrator import schedule_sync
from processwatch.repository import SQLiteRepository
from processwatch.worker import run_collection_cycle
from processwatch.routing import route_process
from test_api import NUMBER, FakeProvider
from processwatch.providers.djen import DjenError


class EmptyDjen:
    async def fetch(self, number, start, end):
        from processwatch.providers.djen import DjenFetch
        return DjenFetch([], start.isoformat(), end.isoformat(), True)


class ForbiddenDjen:
    async def fetch(self, number, start, end):
        raise DjenError("DJEN recusou a consulta (HTTP 403); cobertura não confirmada.")


class CollectionJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "queue.sqlite3")
        self.repo = SQLiteRepository(self.path)
        self.engine = Engine(self.repo, FakeProvider())
        # These tests isolate the existing DataJud/DJEN queue; PJe routing and its
        # real browser flow are covered by the dedicated PJe connector tests.
        self.engine.provider.response["hits"]["hits"][0]["_source"]["sistema"]["nome"] = "Sistema não configurado"
        self.config = Config(db=self.path, djen_initial_date=date.today(), djen_min_interval=0)
        self.engine.register(NUMBER)

    def tearDown(self):
        if self.repo is not None:
            self.repo.close()
        self.temp.cleanup()

    def test_initial_schedule_is_idempotent_and_ordered(self):
        first = schedule_sync(self.repo, "local", NUMBER, self.config)
        second = schedule_sync(self.repo, "local", NUMBER, self.config)
        self.assertEqual(first["group_id"], second["group_id"])
        self.assertEqual([job["source"] for job in first["jobs"]],
                         ["DataJud", "DJEN", "Consolidation"])
        claimed = self.repo.claim_collection_job("local", "test-worker")
        self.assertEqual(claimed["source"], "DataJud")
        self.assertEqual(claimed["attempts"], 1)
        self.repo.finish_collection_job(claimed["id"], claimed["lease_token"], status="completed")
        next_job = self.repo.claim_collection_job("local", "test-worker")
        self.assertEqual(next_job["source"], "DJEN")

    def test_expired_lease_recovers_and_old_token_cannot_finish(self):
        schedule_sync(self.repo, "local", NUMBER, self.config)
        first = self.repo.claim_collection_job("local", "crashed-worker")
        self.repo.db.execute("UPDATE collection_jobs SET lease_until=? WHERE id=?",
                             ((datetime.now(timezone.utc)-timedelta(seconds=2)).isoformat(), first["id"]))
        self.repo.db.commit()
        recovered = self.repo.claim_collection_job("local", "restarted-worker")
        self.assertEqual(recovered["id"], first["id"])
        self.assertEqual(recovered["attempts"], 2)
        self.assertNotEqual(recovered["lease_token"], first["lease_token"])
        self.assertFalse(self.repo.finish_collection_job(first["id"], first["lease_token"], status="completed"))

    def test_two_connections_do_not_claim_same_job(self):
        schedule_sync(self.repo, "local", NUMBER, self.config)

        def claim(worker):
            connection = SQLiteRepository(self.path)
            try:
                return connection.claim_collection_job("local", worker)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ("worker-a", "worker-b")))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_worker_runs_datajud_djen_and_consolidation(self):
        schedule_sync(self.repo, "local", NUMBER, self.config)
        result = run_collection_cycle(self.engine, self.repo, EmptyDjen(), self.config)
        self.assertEqual([item[1].source for item in result], ["DataJud", "DJEN", "Consolidation"])
        rows = self.repo.latest_collection_group("local", NUMBER)
        self.assertEqual([row["status"] for row in rows], ["completed", "completed", "completed"])
        self.assertEqual(len(self.repo.timeline("local", NUMBER)), 2)
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM runs WHERE status='success'").fetchone()[0], 1)

    def test_datajud_job_enqueues_shared_form_pje_for_first_degree_trf1(self):
        source = self.engine.provider.response["hits"]["hits"][0]["_source"]
        source["grau"] = "1º grau"
        source["sistema"]["nome"] = "PJe"
        group = schedule_sync(self.repo, "local", NUMBER, self.config)
        job = self.repo.claim_collection_job("local", "pje-routing-test")
        self.assertEqual(job["source"], "DataJud")
        result = execute_collection_job(job, self.engine, self.repo, EmptyDjen(), self.config)
        self.assertEqual(result.status, "completed")
        jobs = self.repo.collection_jobs("local", NUMBER)
        pje = next(item for item in jobs if item["source"] == "TRF1-PJE-1G")
        self.assertEqual(pje["group_id"], group["group_id"])
        metadata = self.repo.get("local", NUMBER)["metadata"]
        self.assertIn("1º grau", metadata)
        route = route_process(NUMBER, json.loads(metadata))
        self.assertEqual(route["selected_connector"], "TRF1-PJE-1G")
        self.assertEqual(route["status"], "routed_compatible_unvalidated")

    def test_djen_authorization_failure_is_partial_not_empty(self):
        schedule_sync(self.repo, "local", NUMBER, self.config)
        run_collection_cycle(self.engine, self.repo, ForbiddenDjen(), self.config)
        rows = self.repo.latest_collection_group("local", NUMBER)
        djen = next(row for row in rows if row["source"] == "DJEN")
        self.assertEqual(djen["status"], "awaiting_access")
        self.assertTrue(djen["error"])
        checks = self.repo.source_coverage("local", NUMBER, "DJEN")
        self.assertEqual(checks[-1]["status"], "erro")
        self.assertEqual(next(row for row in rows if row["source"] == "Consolidation")["status"], "completed")

    def test_migration_from_v6_preserves_existing_records(self):
        self.repo.db.execute("""INSERT INTO raw_records
            (org,number,source,source_id,payload,collected_at)
            VALUES('local',?,'DataJud','fixture','{}','2026-10-09T00:00:00+00:00')""", (NUMBER,))
        self.repo.db.execute("""INSERT INTO communications
            (org,number,source_id,payload,collected_at)
            VALUES('local',?,'communication-fixture','{}','2026-10-09T00:00:00+00:00')""", (NUMBER,))
        self.repo.db.commit()
        self.repo.db.execute("DROP TABLE collection_jobs")
        self.repo.db.execute("DROP TABLE source_rate_limits")
        self.repo.db.execute("PRAGMA user_version=6")
        self.repo.db.commit()
        self.repo.close()
        self.repo = None

        migrated = SQLiteRepository(self.path)
        try:
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(migrated.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(migrated.db.execute("SELECT count(*) FROM watches").fetchone()[0], 1)
            self.assertEqual(migrated.db.execute("SELECT count(*) FROM raw_records").fetchone()[0], 1)
            self.assertEqual(migrated.db.execute("SELECT count(*) FROM communications").fetchone()[0], 1)
            self.assertEqual(migrated.db.execute("SELECT count(*) FROM collection_jobs").fetchone()[0], 0)
            backups = list(Path(self.temp.name).glob("queue.sqlite3.pre-v18-*.backup.sqlite3"))
            self.assertEqual(len(backups), 1)
            backup_db = __import__("sqlite3").connect(f"file:{backups[0]}?mode=ro", uri=True)
            self.assertEqual(backup_db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(backup_db.execute("SELECT count(*) FROM raw_records").fetchone()[0], 1)
            backup_db.close()
        finally:
            migrated.close()

    def test_human_session_interruption_expires_and_does_not_retry_automatically(self):
        self.repo.enqueue_collection_group("local", NUMBER, "initial", include_datajud=False,
            include_djen=False, additional_sources=["TRF1-PJE-JE"])
        job = self.repo.latest_collection_group("local", NUMBER)[0]
        verification_id = self.repo.create_human_verification("local", NUMBER, job["id"], ttl_seconds=180)
        claimed = self.repo.claim_collection_job("local", "human-worker")
        self.repo.update_human_verification(verification_id, "aguardando_verificacao_humana", worker_id="human-worker")
        self.repo.db.execute("UPDATE collection_jobs SET lease_until=? WHERE id=?",
            ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), claimed["id"]))
        self.repo.db.commit()
        self.repo.recover_expired_jobs("local")
        self.assertEqual(self.repo.collection_jobs("local", NUMBER)[0]["status"], "awaiting_access")
        self.assertEqual(self.repo.human_verification(verification_id)["status"], "expired")
        next_job = self.repo.claim_collection_job("local", "restarted-worker")
        self.assertNotEqual(next_job["source"], "TRF1-PJE-JE")

    def test_expired_human_request_is_not_claimed(self):
        self.repo.enqueue_collection_group("local", NUMBER, "initial", include_datajud=False,
            include_djen=False, additional_sources=["TRF1-PJE-JE"])
        job = self.repo.latest_collection_group("local", NUMBER)[0]
        verification_id = self.repo.create_human_verification("local", NUMBER, job["id"], ttl_seconds=30)
        self.repo.db.execute("UPDATE human_verifications SET expires_at=? WHERE id=?",
            ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(), verification_id))
        self.repo.db.commit()
        self.assertEqual(self.repo.expire_human_verifications("local", NUMBER), 1)
        self.assertEqual(self.repo.human_verification(verification_id)["status"], "expired")
        next_job = self.repo.claim_collection_job("local", "worker")
        self.assertNotEqual(next_job["source"], "TRF1-PJE-JE")

    def test_human_verification_can_be_cancelled_explicitly(self):
        self.repo.enqueue_collection_group("local", NUMBER, "initial", include_datajud=False,
            include_djen=False, additional_sources=["TRF1-PJE-JE"])
        job = self.repo.latest_collection_group("local", NUMBER)[0]
        verification_id = self.repo.create_human_verification("local", NUMBER, job["id"], ttl_seconds=180)
        self.assertTrue(self.repo.cancel_human_verification(verification_id))
        self.assertEqual(self.repo.human_verification(verification_id)["status"], "cancelled")
        pje_job = next(row for row in self.repo.collection_jobs("local", NUMBER) if row["source"] == "TRF1-PJE-JE")
        self.assertEqual(pje_job["status"], "awaiting_access")

    def test_only_one_pje_browser_session_runs_across_organizations(self):
        self.repo.register("other", NUMBER, "trf1", 3600, 100)
        for org in ("local", "other"):
            self.repo.enqueue_collection_group(org, NUMBER, "initial", include_datajud=False,
                include_djen=False, additional_sources=["TRF1-PJE-JE"])
        first = self.repo.claim_collection_job("local", "worker-a")
        self.assertEqual(first["source"], "TRF1-PJE-JE")
        self.assertIsNone(self.repo.claim_collection_job("other", "worker-b"))


if __name__ == "__main__":
    unittest.main()

