import asyncio
import copy
import importlib.util
import tempfile
import unittest
from pathlib import Path

if not importlib.util.find_spec("fastapi") or not importlib.util.find_spec("httpx"):
    raise unittest.SkipTest("Instale as dependências web para executar os testes da API.")

import httpx
from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.repository import SQLiteRepository
from processwatch.worker import run_collection_cycle
from processwatch.providers.contracts import ConnectorResult, ConnectorState
from datetime import date, timedelta


NUMBER = "00000003800004010000"
FORMATTED = "0000000-38.0000.4.01.0000"


def datajud_response(movements=None):
    movements = movements if movements is not None else [
        {"codigo": 26, "dataHora": "2025-01-15T12:00:00Z", "nome": "Distribuição", "complementosTabelados": []},
        {"codigo": 85, "dataHora": "2025-01-16T12:00:00Z", "nome": "Petição", "complementosTabelados": [{"codigo": 1, "nome": "tipo", "valor": "inicial"}]},
    ]
    source = {
        "id": "api-fixture", "numeroProcesso": NUMBER, "tribunal": "TRF1", "grau": "JE",
        "nivelSigilo": 0, "sistema": {"codigo": 1, "nome": "PJe"},
        "classe": {"codigo": 436, "nome": "Procedimento"}, "orgaoJulgador": {"codigo": 1, "nome": "Vara"},
        "assuntos": [], "dataAjuizamento": "20250115000000",
        "dataHoraUltimaAtualizacao": "2025-02-01T12:00:00Z", "movimentos": movements,
    }
    return {"timed_out": False, "hits": {"total": {"value": 1, "relation": "eq"}, "hits": [{"_id": "fixture", "_source": source}]}}


class FakeProvider:
    def __init__(self):
        self.response = datajud_response()

    def fetch(self, number, court):
        return copy.deepcopy(self.response)


class NoNetworkPje:
    """Explicit test double: API tests must never open a real tribunal connector."""
    async def collect(self, request):
        return ConnectorResult(request.source, ConnectorState.COURT_UNAVAILABLE,
                               found=None, limitations=("PJe isolado neste teste",))


class ApiTests(unittest.TestCase):
    def test_calendar_records_are_persisted_and_office_scoped(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / "calendar.sqlite3")
                repo = SQLiteRepository(path)
                repo.create_office("office-b", "Outro escritório")
                engine = Engine(repo, FakeProvider())
                engine.register(FORMATTED, "local")
                app = create_app(repo, engine, config=Config(db=path, access_token="test-token"))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                             headers={"Authorization": "Bearer test-token"}) as client:
                    unauthenticated_app = create_app(repo, engine, config=Config(db=path, access_token="test-token"))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=unauthenticated_app),
                                                 base_url="http://test") as anonymous:
                        self.assertEqual((await anonymous.get("/api/calendar")).status_code, 401)
                    upcoming_date = date.today() + timedelta(days=3)
                    made = await client.post("/api/calendar", json={"item_type": "deadline", "title": "Revisar prazo",
                        "starts_at": f"{upcoming_date.isoformat()}T12:00:00-03:00", "number": FORMATTED, "responsible": "Advogada"})
                    self.assertEqual(made.status_code, 201)
                    self.assertTrue(made.json()["starts_at"].endswith("15:00:00+00:00"))
                    self.assertEqual(made.json()["origin"], "manual")
                    items = await client.get("/api/calendar?item_type=deadline&status=open")
                    self.assertEqual(len(items.json()["items"]), 1)
                    self.assertEqual((await client.get("/api/reports/summary")).json()["upcoming_deadlines"], 1)
                    changed = await client.patch("/api/calendar/" + made.json()["id"], json={"status": "confirmed"})
                    self.assertEqual(changed.status_code, 200)
                    self.assertEqual((await client.get("/api/calendar?status=open")).json()["items"], [])
                    self.assertEqual((await client.patch("/api/calendar/" + made.json()["id"], json={"status": "scheduled"})).status_code, 400)
                    other_app = create_app(repo, engine, org="office-b", config=Config(db=path, access_token="test-token"))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app), base_url="http://test",
                                                 headers={"Authorization": "Bearer test-token"}) as other:
                        self.assertEqual((await other.get("/api/calendar")).json()["items"], [])
                        self.assertEqual((await other.patch("/api/calendar/" + made.json()["id"],
                            json={"status": "discarded"})).status_code, 404)
                repo.close()
        asyncio.run(scenario())

    def test_register_import_repeat_and_timeline(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                repo = SQLiteRepository(str(Path(tmp) / "api.sqlite3"))
                provider = FakeProvider()
                engine = Engine(repo, provider)
                config = Config(db=str(Path(tmp) / "api.sqlite3"), api_key="test", access_token="test-token",
                                djen_initial_date=date.today(), djen_min_interval=0)
                app = create_app(repo, engine, config=config)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                             headers={"Authorization": "Bearer test-token"}) as client:
                    home = await client.get("/")
                    self.assertEqual(home.status_code, 200)
                    self.assertIn("Monitoramento processual", home.text)
                    self.assertIn("img-src 'self' data:", home.headers["content-security-policy"])
                    self.assertIn('id="sync-status"', home.text)
                    app_script = await client.get("/assets/app.js")
                    self.assertEqual(app_script.status_code, 200)
                    self.assertIn("/api/processes/${number}/sync", app_script.text)
                    logo = await client.get("/assets/logo.png")
                    self.assertEqual(logo.status_code, 200)
                    self.assertEqual(logo.headers["content-type"], "image/png")
                    health = await client.get("/api/health")
                    self.assertEqual(health.json()["status"], "ok")
                    self.assertTrue(health.json()["datajud_configured"])
                    created = await client.post("/api/processes", json={"number": FORMATTED, "interval": 3600})
                    self.assertEqual(created.status_code, 201)
                    self.assertIsNone(created.json()["check"])
                    self.assertEqual([job["source"] for job in created.json()["synchronization"]["jobs"]],
                                     ["DataJud", "DJEN", "Consolidation"])
                    class EmptyDjen:
                        async def fetch(self, number, start, end):
                            from processwatch.providers.djen import DjenFetch
                            return DjenFetch([], start.isoformat(), end.isoformat(), True)
                    def worker_cycle():
                        worker_repo = SQLiteRepository(str(Path(tmp) / "api.sqlite3"))
                        try:
                            return run_collection_cycle(Engine(worker_repo, provider), worker_repo,
                                                        EmptyDjen(), config, pje_client=NoNetworkPje())
                        finally:
                            worker_repo.close()
                    await asyncio.to_thread(worker_cycle)
                    listed = await client.get("/api/processes")
                    self.assertEqual(listed.json()[0]["formatted_number"], FORMATTED)
                    self.assertEqual(listed.json()[0]["interval"], 3600)
                    timeline = await client.get(f"/api/processes/{NUMBER}/timeline")
                    self.assertEqual(len(timeline.json()["movements"]), 2)
                    self.assertEqual(timeline.json()["movements"][0]["grade"], "JE")
                    self.assertEqual(timeline.json()["movements"][0]["original"]["nome"], "Distribuição")
                    self.assertEqual(timeline.json()["movements"][1]["complements"][0]["valor"], "inicial")
                    movements = copy.deepcopy(provider.response["hits"]["hits"][0]["_source"]["movimentos"])
                    movements.append({"codigo": 92, "dataHora": "2025-02-02T12:00:00Z", "nome": "Publicação", "complementosTabelados": []})
                    provider.response["hits"]["hits"][0]["_source"]["movimentos"] = movements
                    checked = await client.post(f"/api/processes/{NUMBER}/check")
                    self.assertEqual(checked.json()["result"]["new"], 1)
                    events = await client.get(f"/api/processes/{NUMBER}/events")
                    self.assertEqual(events.json()[0]["movement"]["name"], "Publicação")
                    runs = await client.get(f"/api/processes/{NUMBER}/runs")
                    self.assertEqual(len(runs.json()), 2)
                repo.close()
        asyncio.run(scenario())

    def test_check_all_and_invalid_number(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                repo = SQLiteRepository(str(Path(tmp) / "api.sqlite3"))
                engine = Engine(repo, FakeProvider())
                engine.register(FORMATTED)
                app = create_app(repo, engine, config=Config(db=str(Path(tmp) / "api.sqlite3"), access_token="test-token"))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                             headers={"Authorization": "Bearer test-token"}) as client:
                    health = await client.get("/api/health")
                    self.assertFalse(health.json()["datajud_configured"])
                    checked = await client.post("/api/processes/check-all")
                    self.assertEqual((checked.json()["checked"], checked.json()["failed"]), (1, 0))
                    invalid = await client.post("/api/processes", json={"number": "123"})
                    self.assertEqual(invalid.status_code, 400)
                repo.close()
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

