"""Phase D regression tests for tribunal-specific source presentation and scheduling."""
import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.orchestrator import schedule_sync
from processwatch.repository import SQLiteRepository
from processwatch.routing import route_process


TRF1 = "00000003800004010000"
TRF4 = "10002368520254043305"
TRF5 = "10002365820254053305"


class NoopDataJud:
    def fetch(self, number, court):
        return {"timed_out": False, "hits": {"total": {"value": 0}, "hits": []}}


class PhaseDRoutingTests(unittest.TestCase):
    def test_cnj_selects_datajud_alias_for_trf1_trf4_and_trf5(self):
        expected = ((TRF1, "TRF1", "trf1"), (TRF4, "TRF4", "trf4"), (TRF5, "TRF5", "trf5"))
        for number, tribunal, alias in expected:
            with self.subTest(tribunal=tribunal):
                route = route_process(number)
                self.assertEqual(route["tribunal_id"], tribunal)
                self.assertEqual(route["datajud_alias"], alias)
                self.assertEqual(route["datajud_status"], "operational")

    def test_unsupported_tribunals_do_not_schedule_trf1_pje(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "phase-d.sqlite3"))
            try:
                for number, alias in ((TRF4, "trf4"), (TRF5, "trf5")):
                    Engine(repo, NoopDataJud()).register(number)
                    scheduled = schedule_sync(repo, "local", number,
                        Config(db=str(Path(folder) / "phase-d.sqlite3")), force=True)
                    sources = [job["source"] for job in scheduled["jobs"]]
                    self.assertIn("DataJud", sources)
                    self.assertIn("DJEN", sources)
                    self.assertNotIn("TRF1-PJE-1G", sources)
                    self.assertNotIn("TRF1-PJE-JE", sources)
                    self.assertEqual(repo.get("local", number)["court"], alias)
            finally:
                repo.close()

    def test_trf5_sync_api_says_not_implemented_without_pje_trf1_label(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                path = str(Path(folder) / "api.sqlite3")
                repo = SQLiteRepository(path)
                try:
                    engine = Engine(repo, NoopDataJud())
                    engine.register(TRF5)
                    schedule_sync(repo, "local", TRF5,
                        Config(db=path, access_token="test-token"), force=True)
                    app = create_app(repo, engine, config=Config(db=path, access_token="test-token"))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization": "Bearer test-token"}) as client:
                        sync = (await client.get(f"/api/processes/{TRF5}/sync")).json()
                        direct = [job for job in sync["jobs"] if job["source"] == "Consulta direta ao tribunal"]
                        self.assertEqual(len(direct), 1)
                        self.assertEqual(direct[0]["status"], "not_implemented")
                        self.assertEqual(direct[0]["message"], "Consulta direta ao tribunal ainda não implementada")
                        self.assertFalse(any("PJe/TRF1" in job["source"] for job in sync["jobs"]))
                        self.assertEqual(sync["routing"]["tribunal_id"], "TRF5")
                        details = (await client.get(f"/api/processos/{TRF5}")).json()
                        self.assertIsNone(details["pje_publico"])
                        self.assertEqual(details["consulta_direta"]["message"],
                                         "Consulta direta ao tribunal ainda não implementada")
                        refresh = await client.post(f"/api/processos/{TRF5}/atualizar", json={})
                        self.assertEqual(refresh.status_code, 202)
                        self.assertEqual(refresh.json()["sources"]["Consulta direta ao tribunal"]["status"],
                                         "not_implemented")
                        self.assertNotIn("PJe", refresh.json()["sources"])
                        home = await client.get("/")
                        script = await client.get("/assets/app.js")
                        page = script.text
                        self.assertIn("/assets/app.js", home.text)
                        self.assertIn("data.consulta_direta?.message", page)
                        self.assertIn("not_implemented:'Não implementada'", page)
                        self.assertNotIn("<option>PJe/TRF1</option>", page)
                        self.assertIn("sourcesOf=item=>item.fontes?.length?item.fontes:[item.fonte]", page)
                finally:
                    repo.close()
        asyncio.run(scenario())

    def test_pje_connector_is_selected_only_for_confirmed_trf1_metadata(self):
        route = route_process(TRF1, {"tribunal": "TRF1", "sistema": "PJe", "grau": "JE"},
                              connector_ids={"TRF1-PJE-JE"})
        self.assertEqual(route["selected_connector"], "TRF1-PJE-JE")
        trf5 = route_process(TRF5, {"tribunal": "TRF5", "sistema": "PJe", "grau": "JE"},
                             connector_ids={"TRF1-PJE-JE"})
        self.assertEqual(trf5["tribunal_id"], "TRF5")
        self.assertIsNone(trf5["selected_connector"])

    def test_official_system_codes_can_confirm_a_registered_system(self):
        route = route_process(TRF1, {"sistema": {"codigo": 1}, "grau": "JE"},
                              connector_ids={"TRF1-PJE-JE"})
        self.assertEqual(route["system_names"], ["pje"])
        self.assertEqual(route["selected_connector"], "TRF1-PJE-JE")

    def test_unknown_numeric_system_code_does_not_match_a_connector(self):
        route = route_process(TRF1, {"sistema": {"codigo": 99}, "grau": "JE"},
                              connector_ids={"TRF1-PJE-JE"})
        self.assertIsNone(route["selected_connector"])
        self.assertEqual(route["system_names"], ["99"])


if __name__ == "__main__":
    unittest.main()

