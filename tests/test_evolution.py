import asyncio
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import httpx

from processwatch.api import create_app
from processwatch.cnj import normalize
from processwatch.config import Config
from processwatch.consolidation import consolidate, process_metadata
from processwatch.engine import Engine
from processwatch.normalizer import normalize_response
from processwatch.providers.djen import DjenClient, DjenError, parse_communication
from processwatch.providers.contracts import ConnectorResult, ConnectorState
from processwatch.repository import SQLiteRepository
from processwatch.worker import run_collection_cycle
from test_api import FakeProvider, FORMATTED, NUMBER


class NoNetworkCourtProvider:
    async def collect(self, request):
        return ConnectorResult(source=request.source, state=ConnectorState.NOT_VERIFIED,
                               limitations=("Stub local de teste; nenhuma consulta externa executada.",))


class DataJudRegression(unittest.TestCase):
    def test_246_and_repeated_1051_keep_dates_and_original_name(self):
        movements = [
            {"codigo": 246, "nome": "Definitivo", "dataHora": "2026-04-24T10:00:00-03:00"},
            {"codigo": 1051, "nome": "Decurso de Prazo", "dataHora": "2026-04-23T11:00:00-03:00"},
            {"codigo": 1051, "nome": "Decurso de Prazo", "dataHora": "2026-04-20T12:00:00-03:00"},
            {"codigo": 1051, "nome": "Decurso de Prazo", "dataHora": "2026-04-19T13:00:00-03:00"},
        ]
        source = {"numeroProcesso": NUMBER, "tribunal": "TRF1", "grau": "G1", "id": "fixture",
                  "dataHoraUltimaAtualizacao": "2026-06-08T00:00:00Z", "movimentos": movements}
        raw = {"hits": {"hits": [{"_source": source}]}}
        snapshot = normalize_response(raw, NUMBER, "trf1")
        self.assertEqual(len(snapshot.movements), 4)
        self.assertEqual(sum(item.code == "1051" for item in snapshot.movements), 3)
        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteRepository(str(Path(directory) / "regression.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 3600, 100)
                run = repo.start_run("local", NUMBER)
                repo.apply("local", NUMBER, run, snapshot)
                rows = repo.timeline("local", NUMBER)
                events = consolidate(NUMBER, rows, [])
                self.assertEqual(len(events), 4)
                archived = next(item for item in events if item["code"] == "246")
                self.assertEqual(archived["titulo"], "Arquivamento definitivo")
                self.assertEqual(archived["original_title"], "Definitivo")
                meta = process_metadata(repo.get("local", NUMBER), rows)
                self.assertEqual(meta["ultima_atualizacao_documento"], "2026-06-08T00:00:00Z")
                self.assertEqual(meta["ultima_movimentacao"], "2026-04-24T13:00:00+00:00")
            finally:
                repo.close()


class DjenTests(unittest.IsolatedAsyncioTestCase):
    async def test_paginated_lookup_and_sanitized_text(self):
        pages = []
        def handler(request):
            pages.append(int(request.url.params["pagina"]))
            self.assertEqual(request.url.params["numeroProcesso"], NUMBER)
            page = int(request.url.params["pagina"])
            records = [{"id": page, "numero_processo": NUMBER,
                        "data_disponibilizacao": "2026-04-23", "tipo_comunicacao": "Intimação",
                        "texto": "<p>Prazo</p><script>alert('x')</script>",
                        "link": "javascript:alert(1)"}]
            return httpx.Response(200, json={"count": 2, "items": records})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await DjenClient(client).fetch(FORMATTED, date(2026, 4, 1), date(2026, 4, 30))
        self.assertEqual(pages, [1, 2])
        self.assertTrue(result.complete)
        self.assertEqual(len(result.items), 2)
        self.assertEqual(result.items[0].text_plain, "Prazo")
        self.assertIsNone(result.items[0].url)
        self.assertIsNone(result.items[0].published_at)

    async def test_errors_do_not_claim_absence(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
            with self.assertRaises(DjenError):
                await DjenClient(client).fetch(NUMBER)

    async def test_empty_period_and_bounded_rate_limit_retry(self):
        calls = []
        async def no_wait(seconds):
            calls.append(seconds)
        attempts = 0
        def handler(request):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, headers={"Retry-After": "2"})
            return httpx.Response(200, json={"count": 0, "items": []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await DjenClient(client, sleep=no_wait, min_interval=0).fetch(NUMBER, date(2026, 4, 1), date(2026, 4, 2))
        self.assertEqual(calls, [2])
        self.assertTrue(result.complete)
        self.assertEqual(result.items, [])


class NewApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_tabs_auth_and_partial_source(self):
        class UnavailableDjen:
            async def fetch(self, number, start, end):
                raise DjenError("DJEN indisponível (HTTP 403).")

        with tempfile.TemporaryDirectory() as directory:
            repo = SQLiteRepository(str(Path(directory) / "api.sqlite3"))
            try:
                engine = Engine(repo, FakeProvider())
                engine.register(NUMBER)
                app = create_app(repo, engine, config=Config(access_token="local-secret"), djen_client=UnavailableDjen())
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    self.assertEqual((await client.get("/api/processos/" + NUMBER)).status_code, 401)
                    page = await client.get("/")
                    self.assertEqual(page.status_code, 200)
                    for name in ("Resumo", "Movimentações", "Intimações", "Prazos", "Monitoramento"):
                        self.assertIn(name, page.text)
                    client.headers["Authorization"] = "Bearer local-secret"
                    refresh = await client.post("/api/processos/" + NUMBER + "/atualizar", json={"inicio": "2026-04-01", "fim": "2026-04-30"})
                    self.assertEqual(refresh.status_code, 202)
                    self.assertEqual(refresh.json()["status"], "accepted")
                    self.assertEqual(refresh.json()["sources"]["DataJud"]["status"], "pending")
                    sync = await client.get("/api/processes/" + NUMBER + "/sync")
                    self.assertEqual(sync.json()["status"], "pending")
                    publications = await client.get("/api/processos/" + NUMBER + "/intimacoes")
                    self.assertEqual(publications.json()["status"], "não consultado")
                    def worker_cycle():
                        worker_repo = SQLiteRepository(str(Path(directory) / "api.sqlite3"))
                        try:
                            return run_collection_cycle(
                                Engine(worker_repo, FakeProvider()), worker_repo,
                                UnavailableDjen(), Config(db=str(Path(directory) / "api.sqlite3"),
                                                          access_token="local-secret"),
                                pje_client=NoNetworkCourtProvider(),
                            )
                        finally:
                            worker_repo.close()
                    await asyncio.to_thread(worker_cycle)
                    self.assertEqual((await client.get("/api/processos/" + NUMBER + "/timeline")).json()["items"][0]["fonte"], "DataJud")
                    removed = await client.delete("/api/processos/" + NUMBER)
                    self.assertEqual(removed.json()["status"], "deleted")
                    self.assertEqual((await client.get("/api/processos/" + NUMBER)).status_code, 404)
            finally:
                repo.close()

