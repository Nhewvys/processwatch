import asyncio
import tempfile
import unittest
from datetime import date
from pathlib import Path

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.discovery import by_oab, normalize_document_digits
from processwatch.engine import Engine
from processwatch.providers.djen import DjenClient
from processwatch.repository import SQLiteRepository


class NoopDataJud:
    def fetch(self, number, court):
        raise AssertionError("A pesquisa por OAB não deve consultar DataJud.")


class DiscoveryNormalizationTests(unittest.TestCase):
    def test_oab_formatting_and_uf_are_normalized_for_exact_query(self):
        criterion = by_oab(" 12.345/a ", " ba ", "DJEN")
        self.assertEqual((criterion.oab, criterion.uf), ("12345A", "BA"))

    def test_unknown_uf_and_invalid_oab_are_rejected(self):
        with self.assertRaises(ValueError):
            by_oab("12345", "XX", "DJEN")
        with self.assertRaises(ValueError):
            by_oab("12#345", "BA", "DJEN")

    def test_document_number_formatting_can_be_normalized_without_search_or_storage(self):
        self.assertEqual(normalize_document_digits("123.456.789-09", "CPF"), "12345678909")
        self.assertEqual(normalize_document_digits("12.345.678/0001-90", "CNPJ"), "12345678000190")
        with self.assertRaises(ValueError):
            normalize_document_digits("123.456", "CPF")


class DiscoveryApiTests(unittest.TestCase):
    def test_only_verified_oab_mode_is_exposed_and_processes_are_deduplicated(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                db = str(Path(folder) / "temp.sqlite3")
                repo = SQLiteRepository(db)
                calls = []
                base = "1000237" + "20254013305"
                check = 98 - int(base + "00") % 97
                second_number = base[:7] + f"{check:02d}" + base[7:]
                numbers = ["00000003800004010000", "00000003800004010000", second_number]

                def respond(request):
                    calls.append(dict(request.url.params))
                    items = []
                    for index, number in enumerate(numbers):
                        items.append({"id": f"comm-{index}", "numero_processo": number,
                                      "data_disponibilizacao": "2026-03-23",
                                      "tipoComunicacao": "Intimação", "texto": f"Publicação {index}",
                                      "destinatarioadvogados": [{"nome": "Advogado Exemplo",
                                                                  "numero_oab": "12345", "uf_oab": "BA"}]})
                    return httpx.Response(200, json={"count": len(items), "items": items})

                config = Config(db=db, access_token="test-token", djen_initial_date=date(2026, 3, 23),
                                djen_min_interval=0)
                async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as external:
                    app = create_app(repo, Engine(repo, NoopDataJud()), config=config,
                                     djen_client=DjenClient(external))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                        denied = await client.get("/api/discovery/capabilities")
                        self.assertEqual(denied.status_code, 401)
                        headers = {"Authorization": "Bearer test-token"}
                        capabilities = await client.get("/api/discovery/capabilities", headers=headers)
                        self.assertEqual([x["criterion"] for x in capabilities.json()["available"]], ["oab_uf"])
                        self.assertEqual({x["criterion"] for x in capabilities.json()["unavailable"]},
                                         {"party_name", "cpf_cnpj", "lawyer_name"})
                        home = await client.get("/", headers=headers)
                        self.assertIn("Pesquisa automatizada disponível: OAB/UF", home.text)
                        self.assertIn("CPF/CNPJ ou nome do advogado ainda não está disponível", home.text)
                        self.assertNotIn('id="cpf-search"', home.text)
                        payload = {"oab": "12.345", "uf": "ba", "inicio": "2026-03-23", "fim": "2026-03-23"}
                        found = await client.post("/api/discovery/oab/search", json=payload, headers=headers)
                        self.assertEqual(found.status_code, 200)
                        body = found.json()
                        self.assertEqual(body["total"], 2)
                        self.assertEqual(body["publication_total"], 3)
                        self.assertEqual(len(body["items"]), 2)
                        first = next(item for item in body["items"]
                                     if item["number"] == "00000003800004010000")
                        self.assertEqual(len(first["communications"]), 2)
                        self.assertEqual(calls[0]["numeroOab"], "12345")
                        self.assertEqual(calls[0]["ufOab"], "BA")
                        self.assertEqual(repo.db.execute("SELECT count(*) FROM watches").fetchone()[0], 0)
                        raw = repo.db.execute("SELECT payload FROM raw_records").fetchall()
                        self.assertTrue(all("numero_processo" in item[0] for item in raw))
                repo.close()

        asyncio.run(scenario())

    def test_empty_result_is_a_successful_period_query_not_a_monitoring_record(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                db = str(Path(folder) / "empty.sqlite3")
                repo = SQLiteRepository(db)
                config = Config(db=db, access_token="test-token", djen_initial_date=date(2026, 3, 23),
                                djen_min_interval=0)
                async with httpx.AsyncClient(transport=httpx.MockTransport(
                        lambda _: httpx.Response(200, json={"count": 0, "items": []}))) as external:
                    app = create_app(repo, Engine(repo, NoopDataJud()), config=config,
                                     djen_client=DjenClient(external))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                                 headers={"Authorization": "Bearer test-token"}) as client:
                        response = await client.post("/api/discovery/oab/search", json={
                            "oab": "12345", "uf": "BA", "inicio": "2026-03-23", "fim": "2026-03-23"})
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual((response.json()["total"], response.json()["publication_total"]), (0, 0))
                        self.assertEqual(response.json()["coverage"]["status"], "success")
                        self.assertEqual(repo.db.execute("SELECT count(*) FROM watches").fetchone()[0], 0)
                repo.close()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

