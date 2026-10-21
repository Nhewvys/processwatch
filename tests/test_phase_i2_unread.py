import asyncio
import copy
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.models import Snapshot
from processwatch.normalizer import normalize_response
from processwatch.repository import SQLiteRepository


NUMBER = "00000003800004010000"
AUTH = {"Authorization": "Bearer i2-test"}


def response(movements=None):
    values = movements or [
        {"codigo": 26, "dataHora": "2025-01-01T10:00:00Z", "nome": "Distribuição", "complementosTabelados": []},
        {"codigo": 85, "dataHora": "2025-01-02T10:00:00Z", "nome": "Petição", "complementosTabelados": []},
    ]
    source = {"id": "i2-fixture", "numeroProcesso": NUMBER, "tribunal": "TRF1", "grau": "JE",
              "nivelSigilo": 0, "sistema": {"codigo": 1, "nome": "PJe"},
              "classe": {"codigo": 436, "nome": "Procedimento"}, "orgaoJulgador": {"codigo": 1, "nome": "Vara"},
              "assuntos": [], "dataAjuizamento": "20250101000000",
              "dataHoraUltimaAtualizacao": "2026-10-10T10:00:00Z", "movimentos": copy.deepcopy(values)}
    return {"timed_out": False, "hits": {"total": {"value": 1, "relation": "eq"},
            "hits": [{"_id": "i2-doc", "_source": source}]}}


class FakeProvider:
    def __init__(self):
        self.data = response()
        self.calls = 0

    def fetch(self, *_):
        self.calls += 1
        return copy.deepcopy(self.data)


class UnreadEventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = str(Path(self.tmp.name) / "i2.sqlite3")
        self.repo = SQLiteRepository(self.path)
        self.provider = FakeProvider()
        self.engine = Engine(self.repo, self.provider)
        self.engine.register(NUMBER)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def add_new_movement(self):
        self.engine.check(NUMBER)
        self.provider.data["hits"]["hits"][0]["_source"]["movimentos"].append({
            "codigo": 92, "dataHora": "2026-10-10T15:20:30Z", "nome": "Publicação",
            "complementosTabelados": []})
        self.engine.check(NUMBER)

    def pje_snapshot(self, name="Publicação"):
        return {"movements": [{"source_id": "pje-event-1", "source": "PJe/TRF1",
                "occurred_at": "2026-10-10T15:20:30+00:00", "name": name,
                "description": name, "complements": [], "documents": []}],
                "participants": [], "documents": [], "hearings": []}

    def app_client(self):
        config = Config(db=self.path, access_token="i2-test")
        return httpx.AsyncClient(transport=httpx.ASGITransport(
            app=create_app(self.repo, self.engine, config=config)), base_url="http://test")

    def test_v8_additive_migration_preserves_records_and_baselines_existing_events(self):
        self.engine.check(NUMBER)
        before = self.repo.db.execute("select count(*) from movements").fetchone()[0]
        self.repo.db.execute("drop table logical_event_members")
        self.repo.db.execute("drop table logical_event_states")
        self.repo.db.execute("drop table i2_state_meta")
        self.repo.db.execute("pragma user_version=8")
        self.repo.db.commit()
        self.repo.close()
        self.repo = SQLiteRepository(self.path)
        self.assertEqual(self.repo.db.execute("pragma user_version").fetchone()[0], 18)
        self.assertEqual(self.repo.db.execute("select count(*) from movements").fetchone()[0], before)
        self.assertEqual(self.repo.unread_summary("local", NUMBER)["unread_count"], 0)
        backups = list(Path(self.tmp.name).glob("i2.sqlite3.pre-v18-*.backup.sqlite3"))
        self.assertEqual(len(backups), 1)
        backup = sqlite3.connect(backups[0])
        self.assertEqual(backup.execute("pragma integrity_check").fetchone()[0], "ok")
        backup.close()

    def test_historical_import_is_baseline_and_new_event_is_unread_only_once(self):
        self.engine.check(NUMBER)
        self.assertEqual(self.repo.unread_summary("local", NUMBER)["unread_count"], 0)
        self.add_new_movement()
        self.assertEqual(self.repo.unread_summary("local", NUMBER)["unread_count"], 1)
        self.engine.check(NUMBER)
        self.assertEqual(self.repo.unread_summary("local", NUMBER)["unread_count"], 1)

    def test_event_date_and_first_seen_are_distinct(self):
        self.add_new_movement()
        event = next(item for item in self.repo.current_movement_events("local", NUMBER) if item["is_unread"])
        self.assertEqual(event["occurred_at"], "2026-10-10T15:20:30+00:00")
        self.assertNotEqual(event["first_seen_at"], event["occurred_at"])
        self.assertIsNone(event["read_at"])

    def test_corrected_source_record_keeps_first_observation_and_read_state(self):
        self.add_new_movement()
        old = next(item for item in self.repo.current_movement_events("local", NUMBER) if item["is_unread"])
        self.repo.mark_logical_event_read("local", NUMBER, old["logical_event_id"])
        read_old = next(item for item in self.repo.current_movement_events("local", NUMBER)
                        if item["id"] == old["id"])
        movement = self.provider.data["hits"]["hits"][0]["_source"]["movimentos"][-1]
        movement["complementosTabelados"] = [{"codigo": 1, "nome": "tipo", "valor": "corrigido"}]
        result = self.engine.check(NUMBER)
        self.assertEqual(result.corrected, 1)
        corrected = next(item for item in self.repo.current_movement_events("local", NUMBER)
                         if item["occurred_at"] == "2026-10-10T15:20:30+00:00")
        self.assertEqual(corrected["logical_event_id"], old["logical_event_id"])
        self.assertEqual(corrected["first_seen_at"], read_old["first_seen_at"])
        self.assertEqual(corrected["read_at"], read_old["read_at"])
        self.assertEqual(self.repo.unread_summary("local", NUMBER)["unread_count"], 0)

    def test_same_event_later_consolidated_from_pje_remains_read_and_counted_once(self):
        self.add_new_movement()
        event = next(item for item in self.repo.current_movement_events("local", NUMBER) if item["is_unread"])
        logical_id = event["logical_event_id"]
        self.assertTrue(self.repo.mark_logical_event_read("local", NUMBER, logical_id))
        self.repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", self.pje_snapshot(), status="consulta concluída")
        events = self.repo.current_movement_events("local", NUMBER)
        merged = next(item for item in events if set(item.get("fontes", [])) == {"DataJud", "PJe/TRF1"})
        self.assertEqual(merged["logical_event_id"], logical_id)
        self.assertFalse(merged["is_unread"])
        self.assertIsNotNone(merged["read_at"])
        self.assertEqual(self.repo.unread_summary("local", NUMBER)["unread_count"], 0)

    def test_split_does_not_copy_read_timestamp_to_other_event(self):
        self.add_new_movement()
        self.repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", self.pje_snapshot(), status="consulta concluída")
        merged = next(item for item in self.repo.current_movement_events("local", NUMBER)
                      if set(item.get("fontes", [])) == {"DataJud", "PJe/TRF1"})
        self.repo.mark_logical_event_read("local", NUMBER, merged["logical_event_id"])
        self.repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", self.pje_snapshot("Audiência designada"),
                                      status="consulta concluída")
        split = [item for item in self.repo.current_movement_events("local", NUMBER)
                 if item.get("occurred_at") == "2026-10-10T15:20:30+00:00"]
        self.assertEqual(len(split), 2)
        datajud = next(item for item in split if item["fontes"] == ["DataJud"])
        pje = next(item for item in split if item["fontes"] == ["PJe/TRF1"])
        self.assertIsNotNone(datajud["read_at"])
        self.assertIsNone(pje["read_at"])
        self.assertEqual(pje["read_status"], "historical")

    def test_api_auth_unread_filter_individual_and_process_mark_read(self):
        self.add_new_movement()
        async def scenario():
            async with self.app_client() as client:
                self.assertEqual((await client.get(f"/api/processos/{NUMBER}/eventos-nao-lidos")).status_code, 401)
                listed = await client.get(f"/api/processos/{NUMBER}/eventos-nao-lidos", headers=AUTH)
                self.assertEqual(listed.status_code, 200)
                self.assertEqual(listed.json()["count"], 1)
                event = listed.json()["items"][0]
                unread_timeline = await client.get(f"/api/processos/{NUMBER}/timeline?unread_only=true", headers=AUTH)
                self.assertEqual(len(unread_timeline.json()["items"]), 1)
                first = await client.post(f"/api/processos/{NUMBER}/eventos/{event['id']}/ler", headers=AUTH)
                second = await client.post(f"/api/processos/{NUMBER}/eventos/{event['id']}/ler", headers=AUTH)
                self.assertTrue(first.json()["changed"])
                self.assertFalse(second.json()["changed"])
                self.assertEqual(second.json()["unread_count"], 0)
                self.provider.data["hits"]["hits"][0]["_source"]["movimentos"].append({
                    "codigo": 60, "dataHora": "2026-10-10T16:21:31Z", "nome": "Intimação",
                    "complementosTabelados": []})
                self.engine.check(NUMBER)
                all_read = await client.post(f"/api/processos/{NUMBER}/eventos-nao-lidos/ler-todos", headers=AUTH)
                self.assertEqual(all_read.json()["marked_read"], 1)
                self.assertEqual(all_read.json()["unread_count"], 0)
                repeated_all = await client.post(f"/api/processos/{NUMBER}/eventos-nao-lidos/ler-todos", headers=AUTH)
                self.assertEqual(repeated_all.json()["marked_read"], 0)
        asyncio.run(scenario())

    def test_event_from_other_process_and_missing_event_are_not_marked(self):
        self.add_new_movement()
        async def scenario():
            async with self.app_client() as client:
                self.assertEqual((await client.post(f"/api/processos/{NUMBER}/eventos/not-real/ler", headers=AUTH)).status_code, 404)
                self.assertEqual((await client.post(f"/api/processos/10002365820254053305/eventos/not-real/ler", headers=AUTH)).status_code, 404)
        asyncio.run(scenario())

    def test_overview_counts_consolidated_unread_event_once_and_filters(self):
        self.add_new_movement()
        self.repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", self.pje_snapshot(), status="consulta concluída")
        async def scenario():
            async with self.app_client() as client:
                data = (await client.get("/api/processes/overview", headers=AUTH)).json()
                item = data["items"][0]
                self.assertEqual(item["unread_count"], 1)
                self.assertEqual(data["summary"]["unread_total"], 1)
                filtered = (await client.get("/api/processes/overview?unread_only=true", headers=AUTH)).json()
                self.assertEqual(filtered["total"], 1)
        asyncio.run(scenario())

    def test_repeated_pje_capture_does_not_duplicate_logical_state_or_unread_count(self):
        self.add_new_movement()
        snapshot = self.pje_snapshot()
        self.repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", snapshot, status="consulta concluída")
        first = self.repo.current_movement_events("local", NUMBER)
        before_members = self.repo.db.execute("select count(*) from logical_event_members").fetchone()[0]
        self.repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", snapshot, status="consulta concluída")
        second = self.repo.current_movement_events("local", NUMBER)
        after_members = self.repo.db.execute("select count(*) from logical_event_members").fetchone()[0]
        self.assertEqual(len(first), len(second))
        self.assertEqual(before_members, after_members)
        self.assertEqual(self.repo.unread_summary("local", NUMBER)["unread_count"], 1)

    def test_ambiguous_same_second_events_remain_separate(self):
        self.engine.check(NUMBER)
        stamp = "2026-10-10T15:20:30+00:00"
        for fp, comp in (("petition-a", "manifestação"), ("petition-b", "contestação")):
            self.repo.db.execute("""INSERT INTO movements
                (org,number,fingerprint,scope,anchor,when_at,code,name,complements,raw,first_seen,last_seen,active,kind)
                VALUES('local',? ,?,'JE','same','2026-10-10T15:20:30+00:00','85','Petição',?,'{}',?,?,1,'new')""",
                (NUMBER, fp, '[{"nome":"tipo","valor":"' + comp + '"}]', "2026-10-10T15:20:31+00:00", "2026-10-10T15:20:31+00:00"))
        self.repo.db.commit()
        self.repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", self.pje_snapshot("Petição"), status="consulta concluída")
        same_instant = [item for item in self.repo.current_movement_events("local", NUMBER)
                        if item["occurred_at"] == stamp]
        self.assertEqual(len(same_instant), 3)
        self.assertTrue(all(len(item.get("fontes", [])) == 1 for item in same_instant))

    def test_read_and_source_alternation_data_are_returned_without_external_calls(self):
        self.add_new_movement()
        self.repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", self.pje_snapshot(), status="consulta concluída")
        before = self.repo.db.execute("select count(*) from collection_jobs").fetchone()[0]
        async def scenario():
            async with self.app_client() as client:
                overview = await client.get("/api/processes/overview", headers=AUTH)
                timeline = await client.get(f"/api/processos/{NUMBER}/timeline", headers=AUTH)
                event = next(item for item in timeline.json()["items"]
                             if set(item.get("fontes", [])) == {"DataJud", "PJe/TRF1"})
                self.assertEqual(event["preferred_source"], "PJe/TRF1")
                self.assertEqual({row["source"] for row in event["source_details"]}, {"DataJud", "PJe/TRF1"})
                self.assertEqual(overview.status_code, 200)
        asyncio.run(scenario())
        after = self.repo.db.execute("select count(*) from collection_jobs").fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(self.provider.calls, 2)

    def test_import_and_mark_read_are_serialized_across_sqlite_connections(self):
        self.add_new_movement()
        event = next(item for item in self.repo.current_movement_events("local", NUMBER) if item["is_unread"])
        logical_id = event["logical_event_id"]
        barrier = threading.Barrier(2)
        def mark():
            repo = SQLiteRepository(self.path)
            try:
                barrier.wait()
                return repo.mark_logical_event_read("local", NUMBER, logical_id)
            finally:
                repo.close()
        def import_pje():
            repo = SQLiteRepository(self.path)
            try:
                barrier.wait()
                return repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", self.pje_snapshot(),
                                                status="consulta concluída")
            finally:
                repo.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [pool.submit(mark), pool.submit(import_pje)]
            [future.result(timeout=10) for future in results]
        merged = next(item for item in self.repo.current_movement_events("local", NUMBER)
                      if set(item.get("fontes", [])) == {"DataJud", "PJe/TRF1"})
        self.assertFalse(merged["is_unread"])
        self.assertIsNotNone(merged["read_at"])


if __name__ == "__main__":
    unittest.main()

