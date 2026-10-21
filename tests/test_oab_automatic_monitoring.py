"""Persistent OAB monitoring and process-independent communication inbox regressions."""
import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.domain import ComunicacaoJudicial
from processwatch.engine import Engine
from processwatch.providers.djen import OabFetch, parse_communication
from processwatch.repository import SQLiteRepository
from processwatch.worker import run_collection_cycle
from test_api import FakeProvider, NUMBER


AUTH = {"Authorization": "Bearer oab-test-token"}


def raw_communication(source_id="pub-001", number=NUMBER):
    return {
        "id": source_id,
        "numero_processo": number,
        "data_disponibilizacao": date.today().isoformat(),
        "data_publicacao": date.today().isoformat(),
        "tipoComunicacao": "Intimação",
        "texto": "Publicação de teste da consulta OAB.",
        "destinatarios": [{"nome": "Destinatária Teste"}],
        "destinatarioadvogados": [{"nome": "Advogado Teste", "numero_oab": "12345", "uf_oab": "SP"}],
    }


class OabAutomaticMonitoringTests(unittest.TestCase):
    def setUp(self):
        self._loop_policy = asyncio.get_event_loop_policy()
        if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "test.sqlite3")
        self.repo = SQLiteRepository(self.path)
        self.engine = Engine(self.repo, FakeProvider())
        self.config = Config(db=self.path, access_token="oab-test-token", djen_min_interval=0)

    def tearDown(self):
        if self.repo is not None:
            self.repo.close()
        self.tmp.cleanup()
        asyncio.set_event_loop_policy(self._loop_policy)

    def client(self, djen=None):
        app = create_app(self.repo, self.engine, config=self.config, djen_client=djen)
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    @staticmethod
    def _inline_async(coroutine):
        """Run the immediate awaitable used by deterministic worker fakes."""
        try:
            coroutine.send(None)
        except StopIteration as done:
            return done.value
        raise AssertionError("O fake DJEN não deveria suspender nem acessar a rede.")

    def test_authenticated_crud_duplicate_refresh_and_org_scope(self):
        async def scenario():
            async with self.client() as client:
                denied = await client.get("/api/monitoramentos/oab")
                self.assertEqual(denied.status_code, 401)
                made = await client.post("/api/monitoramentos/oab", headers=AUTH, json={
                    "oab": "12.345", "uf": "sp", "lawyer_name": "Advogada Teste",
                    "interval_hours": 24,
                })
                self.assertEqual(made.status_code, 201, made.text)
                monitor = made.json()
                self.assertEqual((monitor["oab"], monitor["uf"]), ("12345", "SP"))
                self.assertTrue(monitor["enabled"])
                duplicate = await client.post("/api/monitoramentos/oab", headers=AUTH, json={
                    "oab": "12345", "uf": "SP",
                })
                self.assertEqual(duplicate.status_code, 409)
                listed = await client.get("/api/monitoramentos/oab", headers=AUTH)
                self.assertEqual(len(listed.json()["items"]), 1)
                paused = await client.patch(f"/api/monitoramentos/oab/{monitor['id']}", headers=AUTH,
                                            json={"enabled": False})
                self.assertFalse(paused.json()["enabled"])
                self.assertIsNone(paused.json()["next_run_at"])
                active = await client.patch(f"/api/monitoramentos/oab/{monitor['id']}", headers=AUTH,
                                            json={"enabled": True, "interval_hours": 12})
                self.assertTrue(active.json()["enabled"])
                self.assertEqual(active.json()["interval_hours"], 12)
                refresh = await client.post(f"/api/monitoramentos/oab/{monitor['id']}/atualizar", headers=AUTH)
                self.assertEqual(refresh.status_code, 202)
                self.assertEqual(refresh.json()["status"], "queued")
                foreign = create_app(self.repo, self.engine, org="other", config=self.config)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=foreign), base_url="http://test",
                                             headers=AUTH) as other:
                    self.assertEqual((await other.get(f"/api/monitoramentos/oab/{monitor['id']}")).status_code, 404)
                removed = await client.delete(f"/api/monitoramentos/oab/{monitor['id']}", headers=AUTH)
                self.assertTrue(removed.json()["communications_preserved"])

        asyncio.run(scenario())

    def test_worker_schedules_and_collects_oab_without_process_watch(self):
        class FakeDjen:
            def __init__(self):
                self.calls = []

            async def fetch_by_oab(self, oab, uf, start, end):
                self.calls.append((oab, uf, start, end))
                item = parse_communication(raw_communication(), NUMBER)
                return OabFetch([item], 1, 1, start.isoformat(), end.isoformat())

        client = FakeDjen()
        monitor = self.repo.create_oab_monitor("local", "12345", "SP", interval_hours=24)
        with patch("processwatch.worker.asyncio.run", side_effect=self._inline_async):
            completed = run_collection_cycle(self.engine, self.repo, client, self.config)
        self.assertEqual(len(client.calls), 1)
        self.assertLessEqual((client.calls[0][3] - client.calls[0][2]).days, 30)
        self.assertTrue(any(result[1] == "completed" for result in completed))
        updated = self.repo.oab_monitor("local", monitor["id"])
        self.assertTrue(updated["baseline_completed"])
        self.assertIsNotNone(updated["last_success_at"])
        self.assertEqual(self.repo.db.execute("SELECT count(*) FROM watches").fetchone()[0], 0)
        inbox = self.repo.inbox_communications("local")
        self.assertEqual(inbox["total"], 1)
        self.assertFalse(inbox["items"][0]["process_monitored"])
        self.assertEqual(inbox["items"][0]["discovery_kind"], "historical_baseline")

    def test_failed_oab_run_does_not_advance_success_cursor(self):
        class FailingDjen:
            async def fetch_by_oab(self, oab, uf, start, end):
                raise RuntimeError("DJEN temporariamente indisponível")

        monitor = self.repo.create_oab_monitor("local", "12345", "SP")
        with patch("processwatch.worker.asyncio.run", side_effect=self._inline_async):
            run_collection_cycle(self.engine, self.repo, FailingDjen(), self.config)
        updated = self.repo.oab_monitor("local", monitor["id"])
        self.assertFalse(updated["baseline_completed"])
        self.assertIsNone(updated["cursor_end"])
        self.assertIsNone(updated["last_success_at"])
        self.assertIn("indisponível", updated["last_error"])

    def test_first_load_is_historical_and_later_publication_is_new(self):
        class SequentialDjen:
            def __init__(self):
                self.calls = 0

            async def fetch_by_oab(self, oab, uf, start, end):
                self.calls += 1
                ids = ["historic"] if self.calls == 1 else ["historic", "new-publication"]
                items = [parse_communication(raw_communication(identifier), NUMBER) for identifier in ids]
                return OabFetch(items, 1, len(items), start.isoformat(), end.isoformat())

        monitor = self.repo.create_oab_monitor("local", "12345", "SP")
        client = SequentialDjen()
        with patch("processwatch.worker.asyncio.run", side_effect=self._inline_async):
            run_collection_cycle(self.engine, self.repo, client, self.config)
            self.repo.db.execute("UPDATE oab_monitors SET next_run_at=? WHERE id=?",
                ((date.today()-timedelta(days=1)).isoformat()+"T00:00:00+00:00", monitor["id"]))
            self.repo.db.commit()
            run_collection_cycle(self.engine, self.repo, client, self.config)
        result = self.repo.inbox_communications("local")
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["new_count"], 1)
        by_id = {item["source_id"]: item for item in result["items"]}
        self.assertEqual(by_id["historic"]["discovery_kind"], "historical_baseline")
        self.assertEqual(by_id["new-publication"]["discovery_kind"], "new_publication")

    def test_same_publication_by_oab_and_process_is_one_inbox_row(self):
        monitor = self.repo.create_oab_monitor("local", "12345", "SP")
        communication = parse_communication(raw_communication("same-source-id"), NUMBER)
        fetched = OabFetch([communication], 1, 1,
                           (date.today()-timedelta(days=1)).isoformat(), date.today().isoformat())
        self.repo.save_oab_discovery("local", "12345", "SP", fetched,
                                     monitor_id=monitor["id"], discovery_kind="new_publication")
        self.engine.register(NUMBER)
        self.repo.save_communications("local", NUMBER, [communication.model_dump()], status="success")
        rows = self.repo.inbox_communications("local")
        self.assertEqual(rows["total"], 1)
        item = rows["items"][0]
        self.assertTrue(item["found_by_oab"])
        self.assertTrue(item["found_by_process"])
        self.assertTrue(item["process_monitored"])
        self.assertEqual(len(item["oab_refs"]), 1)
        self.assertEqual(len(self.repo.communications("local", NUMBER)), 1)

    def test_inbox_filters_read_state_and_process_linkage(self):
        monitor = self.repo.create_oab_monitor("local", "12345", "SP")
        communication = parse_communication(raw_communication("untracked-pub"), NUMBER)
        today = date.today()
        fetch = OabFetch([communication], 1, 1, today.isoformat(), today.isoformat())
        self.repo.save_oab_discovery("local", "12345", "SP", fetch,
                                     monitor_id=monitor["id"], discovery_kind="new_publication")

        async def scenario():
            async with self.client() as client:
                params = {"oab": "12345", "uf": "SP", "read": "false", "monitored": "false"}
                response = await client.get("/api/communications", params=params, headers=AUTH)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["total"], 1)
                item = response.json()["items"][0]
                self.assertFalse(item["process_monitored"])
                self.assertTrue(item["is_new"])
                self.assertEqual(item["oab_refs"], [{"oab": "12345", "uf": "SP"}])
                date_filter = await client.get("/api/communications", params={
                    "number": NUMBER, "inicio": today.isoformat(), "fim": today.isoformat(),
                    "kind": "intimation", "page": 1, "size": 1,
                }, headers=AUTH)
                self.assertEqual((date_filter.json()["total"], date_filter.json()["page"]), (1, 1))
                monitor_items = await client.get(f"/api/monitoramentos/oab/{monitor['id']}/comunicacoes",
                                                  params={"inicio": today.isoformat(), "fim": today.isoformat()},
                                                  headers=AUTH)
                self.assertEqual(monitor_items.json()["total"], 1)
                detail = await client.get(f"/api/communications/{NUMBER}/untracked-pub", headers=AUTH)
                self.assertEqual(detail.status_code, 200)
                marked = await client.patch(f"/api/communications/{NUMBER}/untracked-pub/read",
                                            headers=AUTH, json={"read": True})
                self.assertTrue(marked.json()["read"])
                read = await client.get("/api/communications", params={"read": "true"}, headers=AUTH)
                self.assertEqual(read.json()["total"], 1)
                again = await client.patch(f"/api/communications/{NUMBER}/untracked-pub/read",
                                           headers=AUTH, json={"read": False})
                self.assertFalse(again.json()["read"])
                self.assertEqual((await client.get("/api/communications", headers=AUTH)).json()["total"], 1)

        asyncio.run(scenario())

    def test_repeated_run_enqueues_are_deduplicated_and_window_overlaps(self):
        monitor = self.repo.create_oab_monitor("local", "12345", "SP")
        first = self.repo.enqueue_oab_monitor_job("local", monitor["id"], manual=True)
        second = self.repo.enqueue_oab_monitor_job("local", monitor["id"], manual=True)
        self.assertEqual(first["id"], second["id"])
        self.repo.db.execute("UPDATE oab_monitors SET cursor_end=? WHERE id=?",
                             ((date.today()-timedelta(days=8)).isoformat(), monitor["id"]))
        refreshed = self.repo.oab_monitor("local", monitor["id"])
        begin, end, kind = self.repo._oab_window(refreshed, date.today())
        self.assertEqual(begin, date.today()-timedelta(days=10))
        self.assertEqual(end, date.today())
        self.assertEqual(kind, "incremental")
        self.assertLessEqual((end-begin).days, 30)

    def test_worker_interruption_requeues_oab_job_without_advancing_cursor(self):
        monitor = self.repo.create_oab_monitor("local", "12345", "SP")
        job = self.repo.enqueue_oab_monitor_job("local", monitor["id"])
        claimed = self.repo.claim_oab_monitor_job("local")
        self.assertEqual(claimed["id"], job["id"])
        stale = (date.today()-timedelta(days=1)).isoformat()+"T00:00:00+00:00"
        self.repo.db.execute("UPDATE oab_monitor_jobs SET started_at=? WHERE id=?", (stale, job["id"]))
        self.repo.db.commit()
        self.assertEqual(self.repo.requeue_stale_oab_monitor_jobs("local"), 1)
        queued = self.repo.db.execute("SELECT status,attempts FROM oab_monitor_jobs WHERE id=?", (job["id"],)).fetchone()
        self.assertEqual((queued["status"], queued["attempts"]), ("queued", 1))
        self.assertIsNone(self.repo.oab_monitor("local", monitor["id"])["cursor_end"])

    def test_concurrent_manual_refreshes_create_only_one_active_job(self):
        monitor = self.repo.create_oab_monitor("local", "12345", "SP")
        barrier = threading.Barrier(2)

        def enqueue_from_independent_connection(_):
            connection = SQLiteRepository(self.path)
            try:
                barrier.wait(timeout=5)
                return connection.enqueue_oab_monitor_job("local", monitor["id"], manual=True)["id"]
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(enqueue_from_independent_connection, range(2)))
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.repo.db.execute(
            "SELECT count(*) FROM oab_monitor_jobs WHERE monitor_id=? AND status IN ('queued','running')",
            (monitor["id"],)).fetchone()[0], 1)

    def test_schema_v14_migration_preserves_legacy_communications(self):
        communication = parse_communication(raw_communication("migration-pub"), NUMBER)
        self.engine.register(NUMBER)
        self.repo.save_communications("local", NUMBER, [communication.model_dump()], status="success")
        self.repo.db.execute("PRAGMA user_version=13")
        self.repo.db.commit()
        self.repo.close()
        migrated = SQLiteRepository(self.path)
        try:
            self.assertEqual(migrated.db.execute("PRAGMA user_version").fetchone()[0], 18)
            self.assertEqual(migrated.db.execute("SELECT count(*) FROM communications").fetchone()[0], 1)
            self.assertEqual(migrated.db.execute("SELECT count(*) FROM communication_inbox").fetchone()[0], 1)
            self.assertEqual(migrated.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            backups = list(Path(self.tmp.name).glob("test.sqlite3.pre-v18-*.backup.sqlite3"))
            self.assertEqual(len(backups), 1)
            backup_db = sqlite3.connect(backups[0])
            try:
                self.assertEqual(backup_db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(backup_db.execute("SELECT count(*) FROM communications").fetchone()[0], 1)
            finally:
                backup_db.close()
        finally:
            migrated.close()
            self.repo = None


if __name__ == "__main__":
    unittest.main()
