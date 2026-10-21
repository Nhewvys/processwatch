import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.repository import SQLiteRepository
from processwatch.trf4_assisted import SOURCE, normalize_capture, merge_capture
from processwatch.jobs import execute_collection_job


NUMBER = "00000005400004040000"
EXTENSION_ID = "mnbildfdkooflihnapccjchegadodfih"


def capture(movements=None):
    return {"schema_version": 1, "source": "TRF4", "capture_mode": "chrome_extension_assisted",
            "extractor_version": "1.0.0", "process_number": NUMBER, "page_host": "consulta.trf4.jus.br",
            "captured_at": "2026-10-11T12:00:00-03:00", "class": "Agravo de Instrumento",
            "court_organ": "Gabinete", "situation": None, "subjects": [],
            "pagination_status": "possibly_incomplete",
            "movements": movements if movements is not None else [
                {"source_id": "move-1", "occurred_at": "2026-10-10T12:30:00", "date_original": "10/10/2026 12:30",
                 "name": "Distribuído por sorteio", "documents": []}], "documents": []}


class Trf4ExtensionTests(unittest.TestCase):
    def test_authorized_capture_flows_through_shared_worker_to_api_timeline(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / "authorized.sqlite3")
                repo = SQLiteRepository(path)
                repo.register("local", NUMBER, "TRF4", 86400, 100)
                config = Config(db=path, access_token="test-token", chrome_extension_id=EXTENSION_ID)
                engine = Engine(repo, object())
                app = create_app(repo, engine, config=config)
                headers = {"Authorization": "Bearer test-token", "Origin": f"chrome-extension://{EXTENSION_ID}"}
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                             base_url="http://test", headers=headers) as client:
                    nonce = (await client.post("/api/extension/trf4/nonce", json={})).json()["nonce"]
                    queued = await client.post("/api/extension/trf4/queue-import", json=capture(), headers={
                        "X-ProcessWatch-Nonce": nonce})
                    self.assertEqual(queued.status_code, 202, queued.text)
                    self.assertEqual(queued.json()["job_status"], "queued")
                    self.assertFalse(queued.json()["complete"])
                    job = repo.claim_collection_job("local", "authorized-capture-worker")
                    self.assertEqual(job["source"], "TRF4-EPROC-DIRECT")
                    outcome = execute_collection_job(job, engine, repo, object(), config)
                    self.assertEqual(outcome.status, "completed")
                    saved = repo.latest_court_snapshot("local", NUMBER, SOURCE)
                    self.assertEqual(len(saved["data"]["movements"]), 1)
                    self.assertEqual(saved["data"]["movements"][0]["source"], SOURCE)
                    compacted = json.loads(repo.collection_jobs("local", NUMBER, group_id=queued.json()["group_id"])[0]["params"])
                    self.assertNotIn("authorized_capture", compacted)
                    self.assertTrue(compacted["authorized_capture_processed"])
                    timeline = await client.get(f"/api/processos/{NUMBER}/timeline")
                    self.assertEqual(timeline.status_code, 200, timeline.text)
                    rows = [item for item in timeline.json()["items"] if item.get("fonte") == SOURCE]
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["titulo"], "Distribuído por sorteio")
                    sync = await client.get(f"/api/processes/{NUMBER}/sync")
                    self.assertEqual(sync.status_code, 200)
                repo.close()
        asyncio.run(scenario())

    def test_normalization_preserves_source_text_and_never_persists_unverified_document_url(self):
        item = capture()["movements"][0]
        item["name"] = "Juntada de petição — texto original"
        item["documents"] = [{"source_id": "doc-1", "description": "Petição",
                              "url": "https://consulta.trf4.jus.br/trf4/doc/1?token=secret"}]
        result = normalize_capture(capture([item]), NUMBER)
        self.assertEqual(result["source"], SOURCE)
        self.assertEqual(result["movements"][0]["name"], "Juntada de petição — texto original")
        self.assertIsNone(result["movements"][0]["documents"][0]["url"])
        self.assertFalse(result["movements"][0]["documents"][0]["public_link_validated"])
        self.assertFalse(result["movements"][0]["documents"][0]["public_link_candidate_available"])
        self.assertNotIn("secret", json.dumps(result))

    def test_rejects_wrong_process_host_html_and_invalid_pagination(self):
        for changes, expected in [({"page_host": "evil.example"}, "página oficial"),
                                  ({"process_number": "00000003800004010000"}, "não corresponde"),
                                  ({"pagination_status": "complete"}, "paginação")]:
            payload = capture()
            payload.update(changes)
            with self.assertRaisesRegex(ValueError, expected):
                normalize_capture(payload, NUMBER)
        payload = capture()
        payload["movements"][0]["name"] = "<script>alert(1)</script>"
        with self.assertRaisesRegex(ValueError, "Conteúdo ativo"):
            normalize_capture(payload, NUMBER)

    def test_incremental_merge_is_idempotent_and_keeps_old_rows(self):
        first = normalize_capture(capture(), NUMBER)
        newer = capture([{"source_id": "move-2", "occurred_at": "2026-10-11T09:00:00",
                          "date_original": "11/10/2026 09:00", "name": "Conclusos para julgamento",
                          "documents": []}])
        second = normalize_capture(newer, NUMBER)
        merged = merge_capture(first, second)
        merged_twice = merge_capture(merged, second)
        self.assertEqual({item["source_id"] for item in merged["movements"]}, {"move-1", "move-2"})
        self.assertEqual(merged_twice["movements"], merged["movements"])
        self.assertFalse(merged["coverage"]["complete"])

    def test_authenticated_import_persists_snapshot_and_timeline_as_historical_baseline(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / "test.sqlite3")
                repo = SQLiteRepository(path)
                try:
                    repo.register("local", NUMBER, "TRF4", 86400, 100)
                    config = Config(db=path, access_token="test-token", chrome_extension_id=EXTENSION_ID)
                    app = create_app(repo, Engine(repo, object()), config=config)
                    transport = httpx.ASGITransport(app=app)
                    headers = {"Authorization": "Bearer test-token", "Origin": f"chrome-extension://{EXTENSION_ID}"}
                    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
                        nonce = await client.post("/api/extension/trf4/nonce", json={})
                        self.assertEqual(nonce.status_code, 200, nonce.text)
                        imported = await client.post("/api/extension/trf4/import", json=capture(),
                            headers={"X-ProcessWatch-Nonce": nonce.json()["nonce"]})
                        self.assertEqual(imported.status_code, 201, imported.text)
                        self.assertTrue(imported.json()["baseline"])
                        self.assertEqual(imported.json()["movement_count"], 1)
                        self.assertEqual(repo.db.execute("PRAGMA user_version").fetchone()[0], 18)
                        raw = repo.latest_court_snapshot("local", NUMBER, SOURCE)
                        self.assertEqual(len(raw["data"]["movements"]), 1)
                        self.assertIsNotNone(raw["source_id"])
                        timeline = await client.get(f"/api/processos/{NUMBER}/timeline")
                        self.assertEqual(timeline.status_code, 200, timeline.text)
                        trf4 = [item for item in timeline.json()["items"] if item.get("fonte") == SOURCE]
                        self.assertEqual(len(trf4), 1)
                        self.assertEqual(trf4[0]["titulo"], "Distribuído por sorteio")
                        self.assertFalse(trf4[0]["is_unread"])
                        nonce2 = (await client.post("/api/extension/trf4/nonce", json={})).json()["nonce"]
                        repeated = await client.post("/api/extension/trf4/import", json=capture(),
                            headers={"X-ProcessWatch-Nonce": nonce2})
                        self.assertEqual(repeated.status_code, 201, repeated.text)
                        self.assertEqual(repeated.json()["newly_observed_count"], 0)
                        self.assertEqual(len(repo.latest_court_snapshot("local", NUMBER, SOURCE)["data"]["movements"]), 1)
                        extended = capture([*capture()["movements"],
                            {"source_id": "move-2", "occurred_at": "2026-10-11T09:00:00",
                             "date_original": "11/10/2026 09:00", "name": "Conclusos para julgamento", "documents": []}])
                        nonce3 = (await client.post("/api/extension/trf4/nonce", json={})).json()["nonce"]
                        added = await client.post("/api/extension/trf4/import", json=extended,
                            headers={"X-ProcessWatch-Nonce": nonce3})
                        self.assertEqual(added.status_code, 201, added.text)
                        self.assertEqual(added.json()["newly_observed_count"], 1)
                        self.assertEqual(repo.unread_summary("local", NUMBER)["unread_count"], 1)
                        # Nonce is one-use and cannot authorize a second replay.
                        replay = await client.post("/api/extension/trf4/import", json=capture(),
                            headers={"X-ProcessWatch-Nonce": nonce.json()["nonce"]})
                        self.assertEqual(replay.status_code, 403)
                finally:
                    repo.close()
        asyncio.run(scenario())

    def test_import_rejects_other_origin_and_requires_existing_watch(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / "test.sqlite3")
                repo = SQLiteRepository(path)
                config = Config(db=path, access_token="test-token", chrome_extension_id=EXTENSION_ID)
                app = create_app(repo, Engine(repo, object()), config=config)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                             headers={"Authorization": "Bearer test-token"}) as client:
                    bad = await client.post("/api/extension/trf4/nonce", json={},
                                            headers={"Origin": "http://evil.example"})
                    self.assertEqual(bad.status_code, 403)
                    good = await client.post("/api/extension/trf4/nonce", json={},
                        headers={"Origin": f"chrome-extension://{EXTENSION_ID}"})
                    self.assertEqual(good.status_code, 200)
                    missing = await client.post("/api/extension/trf4/import", json=capture(), headers={
                        "Origin": f"chrome-extension://{EXTENSION_ID}",
                        "X-ProcessWatch-Nonce": good.json()["nonce"]})
                    self.assertEqual(missing.status_code, 404)
                repo.close()
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

