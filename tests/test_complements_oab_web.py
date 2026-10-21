"""Offline HTTP and presentation regressions for movement titles and OAB search."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from datetime import date

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.providers.djen import DjenClient
from processwatch.providers.contracts import ConnectorError, ConnectorResult, ConnectorState
from processwatch.repository import SQLiteRepository
from processwatch.worker import run_collection_cycle
from processwatch.sgt_catalog import import_export
from test_api import NUMBER, FakeProvider, datajud_response


CATALOG = Path(__file__).resolve().parents[1] / "data/tpu/SGT-2026-10-06.json"
AUTH = {"Authorization": "Bearer test-token"}


class EmptyDjen:
    async def fetch(self, number, start, end):
        from processwatch.providers.djen import DjenFetch
        return DjenFetch([], start.isoformat(), end.isoformat(), True)


class UnavailablePje:
    async def collect(self, request):
        error = ConnectorError(request.source, "not_configured", "Conector não configurado no teste.",
                               ConnectorState.NOT_IMPLEMENTED, False, "test")
        return ConnectorResult(request.source, ConnectorState.NOT_IMPLEMENTED, found=None, error=error)


class ComplementWebTests(unittest.TestCase):
    def test_titles_originals_and_unknown_complements_via_api(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                repo = SQLiteRepository(str(Path(folder) / "db.sqlite3"))
                import_export(repo, CATALOG)
                provider = FakeProvider()
                provider.response = datajud_response([
                    {"id": "doc", "codigo": 581, "nome": "Documento", "dataHora": "2026-01-01T10:00:00Z",
                     "complementosTabelados": [{"codigo": 4, "nome": "Certidão", "valor": 107}]},
                    {"id": "pet", "codigo": 85, "nome": "Petição", "dataHora": "2026-01-02T10:00:00Z",
                     "complementosTabelados": [{"codigo": 19, "nome": "Renúncia de mandato", "valor": 111}]},
                    {"id": "conc", "codigo": 51, "nome": "Conclusão", "dataHora": "2026-01-03T10:00:00Z",
                     "complementosTabelados": [{"codigo": 3, "nome": "para julgamento", "valor": 36}]},
                    {"id": "unknown", "codigo": 581, "nome": "Documento", "dataHora": "2026-01-04T10:00:00Z",
                     "complementosTabelados": [{"codigo": 99999, "nome": "Outros documentos", "valor": 80}]},
                    {"id": "unnamed", "codigo": 581, "nome": "Documento", "dataHora": "2026-01-05T10:00:00Z",
                     "complementosTabelados": [{"codigo": 99998, "descricao": "tipo_de_documento", "valor": 81}]},
                ])
                engine = Engine(repo, provider)
                engine.register(NUMBER)
                engine.check(NUMBER)
                app = create_app(repo, engine, config=Config(db=str(Path(folder) / "db.sqlite3"), access_token="test-token"))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=AUTH) as client:
                    response = await client.get(f"/api/processos/{NUMBER}/timeline")
                    self.assertEqual(response.status_code, 200)
                    movements = {item["original_id"]: item for item in response.json()["items"]}
                    self.assertEqual(movements["doc"]["titulo"], "Documento — Certidão")
                    self.assertEqual(movements["pet"]["titulo"], "Petição — Renúncia de mandato")
                    self.assertEqual(movements["conc"]["titulo"], "Conclusão — Para julgamento")
                    self.assertEqual(movements["unknown"]["titulo"], "Documento — Outros documentos")
                    self.assertEqual(movements["unknown"]["complement_display"][0]["origin"], "DataJud")
                    self.assertFalse(movements["unknown"]["complement_display"][0]["validated"])
                    self.assertIn("tipo_de_documento", movements["unnamed"]["titulo"].casefold())
                    self.assertEqual(movements["pet"]["original_title"], "Petição")
                    self.assertEqual(movements["pet"]["original"]["complementosTabelados"][0]["nome"], "Renúncia de mandato")
                    detail = await client.get(f"/api/processos/{NUMBER}/movimentacoes")
                    self.assertIn("Petição — Renúncia de mandato", [item["title"] for item in detail.json()["items"]])
                repo.close()
        asyncio.run(scenario())


class OabWebTests(unittest.TestCase):
    def test_authenticated_search_pages_explicit_add_and_no_duplicates(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                repo = SQLiteRepository(str(Path(folder) / "db.sqlite3"))
                requests = []

                def respond(request):
                    requests.append(dict(request.url.params))
                    items = [{"id": identifier, "numero_processo": NUMBER, "data_disponibilizacao": "2026-03-23",
                              "tipoComunicacao": "Intimação", "texto": "Texto público da comunicação",
                              "destinatarios": [{"nome": "Destinatário Exemplo"}],
                              "destinatarioadvogados": [{"nome": "Advogado Exemplo", "numero_oab": "12345", "uf_oab": "BA"}]}
                             for identifier in range(25)]
                    return httpx.Response(200, json={"count": 25, "items": items})

                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as external:
                    config = Config(db=str(Path(folder) / "db.sqlite3"), access_token="test-token",
                                    djen_initial_date=date.today(), djen_min_interval=0)
                    app = create_app(repo, Engine(repo, FakeProvider()),
                                     config=config,
                                     djen_client=DjenClient(external))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                        payload = {"oab": "12.345", "uf": "ba", "inicio": "2026-03-23", "fim": "2026-03-23"}
                        self.assertEqual((await client.post("/api/discovery/oab/search", json=payload)).status_code, 401)
                        bad = await client.post("/api/discovery/oab/search", json={**payload, "fim": "2026-05-01"}, headers=AUTH)
                        self.assertEqual(bad.status_code, 400)
                        found = await client.post("/api/discovery/oab/search", json=payload, headers=AUTH)
                        self.assertEqual(found.status_code, 200)
                        self.assertEqual((found.json()["total"], len(found.json()["items"])), (1, 1))
                        self.assertEqual(len(found.json()["items"][0]["communications"]), 25)
                        self.assertIn("publicações", found.json()["warning"].lower())
                        self.assertIn("Texto público", found.json()["items"][0]["communication"]["text_plain"])
                        self.assertEqual(repo.db.execute("SELECT count(*) FROM watches").fetchone()[0], 0)
                        more = await client.get("/api/discovery/oab/results", params={**payload, "page": 2}, headers=AUTH)
                        self.assertEqual(len(more.json()["items"]), 0)
                        selected = await client.post("/api/discovery/oab/add", json={**payload, "numbers": [NUMBER, NUMBER],
                                              "portfolio": "Carteira teste", "interval": 3600}, headers=AUTH)
                        self.assertEqual((selected.status_code, selected.json()["added"]), (200, 1))
                        self.assertEqual([item["source"] for item in selected.json()["results"][0]["sync_jobs"]],
                                         ["DataJud", "DJEN", "Consolidation"])
                        self.assertEqual(repo.db.execute("SELECT count(*) FROM collection_jobs").fetchone()[0], 3)
                        self.assertEqual(repo.portfolio_numbers("local", "Carteira teste"), [NUMBER])
                        communications = await client.get(f"/api/processos/{NUMBER}/intimacoes", headers=AUTH)
                        participants = await client.get(f"/api/processos/{NUMBER}/partes", headers=AUTH)
                        self.assertEqual(len(communications.json()["items"]), 25)
                        self.assertGreater(len(participants.json()["participants"]), 0)
                        recipients = participants.json()["destinatarios"]
                        self.assertEqual(len(recipients), 1)
                        self.assertEqual(recipients[0]["mention_count"], 25)
                        self.assertEqual(len(recipients[0]["evidence_ids"]), 25)
                        lawyers = participants.json()["advogados"]
                        self.assertEqual(len(lawyers), 1)
                        self.assertEqual(lawyers[0]["mention_count"], 25)
                        self.assertTrue(all("sources" in person and "DJEN_PUBLICACAO" in person["sources"]
                                            for person in participants.json()["participants"]))
                        home = await client.get("/", headers=AUTH)
                        app_script = await client.get("/assets/app.js", headers=AUTH)
                        self.assertIn("/assets/app.js", home.text)
                        self.assertIn("Proveniência:", app_script.text)
                        def worker_cycle():
                            worker_repo = SQLiteRepository(str(Path(folder) / "db.sqlite3"))
                            try:
                                return run_collection_cycle(
                                    Engine(worker_repo, FakeProvider()), worker_repo,
                                    EmptyDjen(), config, pje_client=UnavailablePje(),
                                )
                            finally:
                                worker_repo.close()
                        await asyncio.to_thread(worker_cycle)
                        sync = await client.get(f"/api/processes/{NUMBER}/sync", headers=AUTH)
                        self.assertEqual(sync.json()["status"], "partial")
                        self.assertEqual([job["status"] for job in sync.json()["jobs"]
                                          if job["source"] in ("DataJud", "DJEN", "Consolidation")],
                                         ["completed", "completed", "completed"])
                        again = await client.post("/api/discovery/oab/add", json={**payload, "numbers": [NUMBER],
                                           "portfolio": "Carteira teste", "interval": 7200}, headers=AUTH)
                        self.assertEqual(again.json()["results"][0]["status"], "já monitorado")
                        self.assertEqual(repo.db.execute("SELECT count(*) FROM collection_jobs").fetchone()[0], 4)
                        self.assertEqual(repo.db.execute("SELECT count(*) FROM collection_jobs WHERE source='TRF1-PJE-JE'").fetchone()[0], 1)
                        self.assertEqual(repo.get("local", NUMBER)["interval"], 3600)
                        self.assertEqual(len(requests), 1)
                        self.assertEqual(requests[0]["numeroOab"], "12345")
                repo.close()
        asyncio.run(scenario())

