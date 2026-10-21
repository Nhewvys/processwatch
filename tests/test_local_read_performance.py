import asyncio
import json
import tempfile
import unittest
import sys
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.repository import SQLiteRepository


NUMBER = "00000003800004010000"
AUTH = {"Authorization": "Bearer local-read-test"}


def run_async(coroutine):
    previous = asyncio.get_event_loop_policy()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(coroutine)
    finally:
        asyncio.set_event_loop_policy(previous)


class OfflineProvider:
    calls = 0

    def fetch(self, *args):
        type(self).calls += 1
        return {}


class LocalReadPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = str(Path(self.temp.name) / "local-read.sqlite3")
        self.repo = SQLiteRepository(self.path)
        self.repo.register("local", NUMBER, "trf1", 3600, 100)
        self.engine = Engine(self.repo, OfflineProvider())
        self.app = create_app(self.repo, self.engine,
                              config=Config(db=self.path, access_token="local-read-test"))

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def client(self, app=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app or self.app),
                                 base_url="http://test", headers=AUTH)

    def test_passive_process_and_communication_reads_do_not_enqueue_or_call_provider(self):
        before_jobs = self.repo.db.execute("SELECT count(*) FROM collection_jobs").fetchone()[0]
        before_access = self.repo.db.execute("SELECT count(*) FROM access_log").fetchone()[0]
        OfflineProvider.calls = 0

        async def scenario():
            async with self.client() as client:
                status = await client.get(f"/api/processes/{NUMBER}/sync")
                self.assertEqual(status.status_code, 200)
                self.assertEqual(self.repo.db.execute("SELECT count(*) FROM access_log").fetchone()[0], before_access)
                for path in (f"/api/processos/{NUMBER}", f"/api/processos/{NUMBER}/timeline",
                             f"/api/processos/{NUMBER}/intimacoes", "/api/communications",
                             "/api/processes/overview"):
                    response = await asyncio.wait_for(client.get(path), timeout=2.0)
                    self.assertEqual(response.status_code, 200, path)
                    self.assertIn("X-ProcessWatch-SQLite-Ms", response.headers)
                    self.assertIn("Server-Timing", response.headers)

        run_async(scenario())
        after_jobs = self.repo.db.execute("SELECT count(*) FROM collection_jobs").fetchone()[0]
        self.assertEqual(after_jobs, before_jobs)
        self.assertEqual(OfflineProvider.calls, 0)

    def test_communications_and_activity_are_local_and_office_scoped(self):
        stamp = datetime.now(timezone.utc).isoformat()
        payload = {"source_id": "comm-local", "tipo": "Intimação de teste", "published_at": stamp,
                   "text_plain": "Registro local"}
        self.repo.db.execute("INSERT INTO communications(org,number,source_id,payload,collected_at) VALUES(?,?,?,?,?)",
                             ("local", NUMBER, "comm-local", json.dumps(payload), stamp))
        self.repo.db.execute("""INSERT INTO movements(org,number,fingerprint,scope,anchor,when_at,code,name,
            complements,raw,first_seen,last_seen,active,kind) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("local", NUMBER, "movement-local", "G1", "anchor", stamp, "", "Despacho local", "[]", "{}",
             stamp, stamp, 1, "datajud"))
        office_b_app = create_app(self.repo, self.engine,
                                  config=Config(db=self.path, access_token="local-read-test"), org="office-b")
        start = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        end = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()

        async def scenario():
            async with self.client() as client:
                comms = (await client.get("/api/communications")).json()
                activity = (await client.get("/api/processes/activity",
                    params={"start": start, "end": end, "kind": "movement"})).json()
                self.assertEqual(comms["total"], 1)
                self.assertEqual(comms["items"][0]["number"], NUMBER)
                self.assertEqual(activity["total"], 1)
                self.assertEqual(activity["items"][0]["titulo"], "Despacho local")
            async with self.client(office_b_app) as other:
                other_comms = (await other.get("/api/communications")).json()
                other_activity = (await other.get("/api/processes/activity",
                    params={"start": start, "end": end, "kind": "movement"})).json()
                self.assertEqual(other_comms["total"], 0)
                self.assertEqual(other_activity["total"], 0)

        run_async(scenario())

    def test_overview_consolidation_has_a_hard_process_timeout(self):
        # asyncio.wait_for cannot interrupt synchronous work that blocks the
        # event loop. A separate process makes this regression timeout real.
        stamp = datetime.now(timezone.utc).isoformat()
        body = ("Narrativa processual para teste de leitura local. " * 200
                + "\nAUTOR: Maria Exemplo\nRÉU: Empresa Exemplo")
        for index in range(35):
            payload = {"source_id": f"load-{index}", "text_plain": body}
            self.repo.db.execute(
                "INSERT INTO communications(org,number,source_id,payload,collected_at) VALUES(?,?,?,?,?)",
                ("local", NUMBER, payload["source_id"], json.dumps(payload), stamp))
        self.repo.db.commit()
        script = """
import asyncio, json, sys, httpx
from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.repository import SQLiteRepository
repo = SQLiteRepository(sys.argv[1])
app = create_app(repo, Engine(repo, object()), config=Config(db=sys.argv[1], access_token='bounded-read'))
async def check():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/api/processes/overview', headers={'Authorization':'Bearer bounded-read'})
        print(json.dumps({'status':response.status_code, 'items':len(response.json().get('items', []))}))
try:
    asyncio.run(check())
finally:
    repo.close()
"""
        result = subprocess.run([sys.executable, "-c", script, self.path],
                                capture_output=True, text=True, timeout=8, check=True)
        self.assertEqual(json.loads(result.stdout), {"status": 200, "items": 1})

    def test_frontend_only_polls_sync_while_detail_job_is_active(self):
        app_response = None

        async def scenario():
            nonlocal app_response
            async with self.client() as client:
                app_response = await client.get("/")

        run_async(scenario())
        html = app_response.text + (Path(__file__).parents[1] / "src/processwatch/web/app.js").read_text(encoding="utf-8")
        self.assertNotIn("},2000);", html)
        self.assertIn("},5000);", html)
        self.assertIn("currentView!=='detail'||selected!==number", html)
        self.assertIn("},30000);", html)
        self.assertIn("/api/communications", html)
        self.assertIn("/api/processes/activity?", html)


if __name__ == "__main__":
    unittest.main()
