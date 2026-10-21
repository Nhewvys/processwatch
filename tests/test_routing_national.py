"""Routing and participant presentation tests; external providers are not called here."""
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.routing import route_process, public_query_url
from processwatch.source_matrix import load_document
from processwatch.repository import SQLiteRepository
from processwatch.providers.djen import parse_communication
from test_api import NUMBER


class NationalMatrixTests(unittest.TestCase):
    def test_registry_covers_national_tribunal_families_and_known_combinations(self):
        doc = load_document()
        self.assertEqual(len(doc["tribunals"]), 93)
        self.assertEqual(len(doc["sources"]), 192)
        self.assertEqual(sum(row["integration_state"] == "investigated_not_operational" for row in doc["sources"]), 15)
        self.assertTrue(any(row["id"] == "TRF4-EPROC-1G" for row in doc["sources"]))
        self.assertTrue(any(row["id"] == "TRF1-PJE-JE" and row["degree"] == "JE" for row in doc["sources"]))
        self.assertTrue(any(row["id"] == "TJSP-ESAJ-2G" for row in doc["sources"]))
        unknown = next(row for row in doc["sources"] if row["id"] == "TRF1-PJE-2G")
        self.assertIsNone(unknown["official_url"])

    def test_router_uses_tribunal_but_does_not_guess_system_or_degree(self):
        result = route_process(NUMBER)
        self.assertEqual(result["tribunal_id"], "TRF1")
        self.assertEqual(result["datajud_alias"], "trf1")
        self.assertEqual(result["status"], "system_unconfirmed")
        self.assertIsNone(result["selected_connector"])

    def test_router_needs_exact_system_degree_and_operational_connector(self):
        meta = {"sistema": {"nome": "PJe"}, "grau": "G1", "tribunal": "TRF1"}
        result = route_process(NUMBER, meta, connector_ids={"TRF1-PJE-1G"})
        self.assertEqual(result["status"], "routed_compatible_unvalidated")
        self.assertEqual(result["selected_connector"], "TRF1-PJE-1G")
        self.assertTrue(result["court_candidates"][0]["matches_metadata"])

    def test_metadata_conflict_never_routes(self):
        result = route_process(NUMBER, {"sistema": "eproc", "grau": "1G", "tribunal": "TRF4"})
        self.assertEqual(result["status"], "tribunal_mismatch")
        self.assertIsNone(result["selected_connector"])

    def test_trf4_g2_matches_only_the_official_second_degree_query(self):
        number="00000005400004040000"
        result=route_process(number,{"sistema":{"nome":"eproc"},"grau":"G2","tribunal":"TRF4"})
        matched=[row for row in result["court_candidates"] if row["matches_metadata"]]
        self.assertEqual([row["id"] for row in matched],["TRF4-EPROC-2G"])
        self.assertEqual(matched[0]["query_url"],
            "https://consulta.trf4.jus.br/trf4/controlador.php?acao=consulta_processual_pesquisa")
        self.assertNotIn("5008047",matched[0]["query_url"])

    def test_generic_official_query_is_available_when_datajud_system_conflicts(self):
        # DataJud labels this case Projudi, while the public TRF4 portal is a
        # generic lookup shared by both registered eproc degrees. Do not infer
        # an eproc connector, but do offer the verified tribunal query page.
        number="00000005400004040000"
        result=route_process(number,[{"tribunal":"TRF4","sistema":{"nome":"Projudi"},"grau":"G2"}])
        self.assertEqual(result["status"],"system_not_in_matrix")
        self.assertEqual(public_query_url(result),
            "https://consulta.trf4.jus.br/trf4/controlador.php?acao=consulta_processual_pesquisa")

    def test_ambiguous_official_query_urls_are_not_guessed(self):
        self.assertIsNone(public_query_url({"court_candidates":[
            {"query_url":"https://court.example/one"},
            {"query_url":"https://court.example/two"}]}))


class ParticipantPresentationTests(unittest.TestCase):
    def test_repeated_oab_mention_groups_but_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "test.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 3600, 100)
                raw1 = {"id": "publication-1", "numero_processo": NUMBER,
                        "destinatarioadvogados": [{"nome": "Advogado Exemplo", "numero_oab": "12345", "uf_oab": "BA",
                                                   "papel": "advogado constituído"}]}
                raw2 = {"id": "publication-2", "numero_processo": NUMBER,
                        "destinatarioadvogados": [{"nome": "Advogado Exemplo", "numero_oab": "12345", "uf_oab": "BA",
                                                   "papel": "destinatário de intimação"}]}
                items = [parse_communication(item, NUMBER).model_dump() for item in (raw1, raw2)]
                repo.save_communications("local", NUMBER, items, status="período consultado")
                participants = repo.participants("local", NUMBER)
                self.assertEqual(len(participants), 1)
                self.assertTrue(participants[0]["identity_confirmed"])
                self.assertEqual(participants[0]["mention_count"], 2)
                self.assertEqual(set(participants[0]["roles"]), {"advogado constituído", "destinatário de intimação"})
                self.assertEqual(set(participants[0]["evidence_ids"]), {"publication-1", "publication-2"})
                self.assertEqual(len(repo.communications("local", NUMBER)), 2)
            finally:
                repo.close()

    def test_existing_parties_api_and_web_render_single_group_with_evidence(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                repo = SQLiteRepository(str(Path(folder) / "test.sqlite3"))
                engine = Engine(repo, type("Provider", (), {"fetch": lambda self, n, c: {}})())
                engine.register(NUMBER)
                items = []
                for cid in ("web-pub-1", "web-pub-2"):
                    raw = {"id": cid, "numero_processo": NUMBER,
                           "destinatarioadvogados": [{"nome": "Advogado Exemplo", "numero_oab": "12345", "uf_oab": "BA"}]}
                    items.append(parse_communication(raw, NUMBER).model_dump())
                repo.save_communications("local", NUMBER, items, status="período consultado")
                app = create_app(repo, engine, config=Config(access_token="test-token"))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                             headers={"Authorization": "Bearer test-token"}) as client:
                    response = await client.get(f"/api/processos/{NUMBER}/partes")
                    self.assertEqual(response.status_code, 200)
                    data = response.json()
                    self.assertEqual(len(data["participants"]), 1)
                    self.assertEqual(data["participants"][0]["mention_count"], 2)
                    self.assertEqual(len(data["participants"][0]["evidence_ids"]), 2)
                    page = await client.get("/")
                    script = await client.get("/assets/app.js")
                    self.assertIn("/assets/app.js", page.text)
                    self.assertIn("menções em publicações", script.text)
                    self.assertIn("Publicações que fundamentam estas menções", script.text)
                repo.close()
        import asyncio
        asyncio.run(scenario())

    def test_same_name_without_identity_is_unconfirmed_and_roles_stay_separate(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "test.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 3600, 100)
                comms = []
                for cid, role in (("pub-a", "parte autora"), ("pub-b", "parte ré")):
                    raw = {"id": cid, "numero_processo": NUMBER, "partes": [{"nome": "Maria Exemplo", "papel": role}]}
                    comms.append(parse_communication(raw, NUMBER).model_dump())
                repo.save_communications("local", NUMBER, comms, status="período consultado")
                participants = repo.participants("local", NUMBER)
                self.assertEqual(len(participants), 2)
                self.assertTrue(all(not item["identity_confirmed"] for item in participants))
            finally:
                repo.close()

    def test_recipients_are_not_promoted_to_parties(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "test.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 3600, 100)
                raw = {"id": "pub-recipient", "numero_processo": NUMBER,
                       "destinatarios": ["Destinatário Exemplo"]}
                item = parse_communication(raw, NUMBER).model_dump()
                repo.save_communications("local", NUMBER, [item], status="período consultado")
                participant = repo.participants("local", NUMBER)[0]
                self.assertEqual(participant["kind"], "destinatário")
                self.assertNotIn("parte", participant["kinds"])
            finally:
                repo.close()


