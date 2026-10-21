import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.repository import SQLiteRepository, now


TRF1 = "00000003800004010000"
TRF5 = "10002365820254053305"
TRF4 = "00000005400004040000"
AUTH = {"Authorization": "Bearer i1-test"}


class Offline:
    def fetch(self, *args):
        return {}


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.path = str(Path(self.temp.name) / "dashboard.sqlite3")
        self.repo = SQLiteRepository(self.path)
        self.engine = Engine(self.repo, Offline())

    def tearDown(self):
        self.repo.close()
        self.temp.cleanup()

    def app_client(self):
        app = create_app(self.repo, self.engine, config=Config(db=self.path, access_token="i1-test"))
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    def add_watch(self, number):
        return self.engine.register(number)

    def test_empty_and_authentication(self):
        async def scenario():
            async with self.app_client() as client:
                self.assertEqual((await client.get("/api/processes/overview", headers=AUTH)).json()["summary"]["total"], 0)
                self.assertEqual((await client.get("/api/processes/overview")).status_code, 401)
        asyncio.run(scenario())

    def test_overview_is_read_only_and_shows_pending_jobs_and_non_applicable_court_source(self):
        self.add_watch(TRF5)
        before = self.repo.db.execute("select count(*) from collection_jobs").fetchone()[0]
        async def scenario():
            async with self.app_client() as client:
                response = await client.get("/api/processes/overview", headers=AUTH)
                self.assertEqual(response.status_code, 200)
                item = response.json()["items"][0]
                self.assertEqual(item["state"], "not_queried")
                self.assertNotIn("PJe/TRF1", [source["source"] for source in item["sources"]])
                self.assertEqual(item["direct_court_status"], "not_verified")
                self.assertEqual(item["event_count"], 0)
        asyncio.run(scenario())
        after = self.repo.db.execute("select count(*) from collection_jobs").fetchone()[0]
        self.assertEqual(before, after)

    def test_trf1_first_degree_connector_is_reported_unvalidated_not_absent(self):
        self.add_watch(TRF1)
        metadata = [{"_scope": "1G", "grau": "1G", "sistema": {"nome": "PJe"}}]
        self.repo.db.execute("UPDATE watches SET metadata=? WHERE org='local' AND number=?",
                             (json.dumps(metadata), TRF1))

        async def scenario():
            async with self.app_client() as client:
                response = await client.get("/api/processes/overview", headers=AUTH)
                self.assertEqual(response.status_code, 200)
                item = response.json()["items"][0]
                self.assertEqual(item["direct_court_status"], "compatible_unvalidated")

        asyncio.run(scenario())

    def test_stale_snapshot_and_new_failure_keep_valid_data_but_flag_attention(self):
        self.add_watch(TRF1)
        metadata = [{"_scope": "JE", "grau": "JE", "sistema": {"nome": "PJe"}, "classe": {"nome": "JEF"}}]
        self.repo.db.execute("UPDATE watches SET metadata=?,last_checked=? WHERE org='local' AND number=?",
                             (json.dumps(metadata), now(), TRF1))
        self.repo.save_court_snapshot("local", TRF1, "PJe/TRF1", {
            "movements": [{"source_id": "m1", "occurred_at": "2026-01-01T10:00:00-03:00", "name": "Juntada de documento"}],
            "participants": [{"original_name": "Pessoa"}], "documents": [], "hearings": []}, status="consulta concluída")
        self.repo.db.execute("""INSERT INTO movements(org,number,fingerprint,scope,anchor,when_at,code,name,complements,raw,
            first_seen,last_seen,active,kind) VALUES('local',?,?, 'JE','a','2026-01-01T10:00:00-03:00','',
            'Juntada de documento','[]','{}',?,?,1,'historical')""",
            (TRF1, "dj-match", now(), now()))
        stamp = now()
        self.repo.db.execute("""INSERT INTO source_checks(org,number,source,checked_at,status,count,error)
            VALUES('local',?,'PJe/TRF1',?,'timeout',1,?)""", (TRF1, stamp, json.dumps({"message": "Tempo esgotado"})))
        async def scenario():
            async with self.app_client() as client:
                item = (await client.get("/api/processes/overview", headers=AUTH)).json()["items"][0]
                self.assertEqual(item["state"], "failed")
                pje = next(source for source in item["sources"] if source["source"] == "PJe/TRF1")
                self.assertEqual(pje["status"], "failed")
                self.assertEqual(pje["records_count"], 2)
                self.assertIsNotNone(pje["last_success_at"])
                self.assertEqual(item["event_count"], 1)
        asyncio.run(scenario())

    def test_pending_running_and_human_attention_are_distinct(self):
        self.add_watch(TRF1)
        metadata = [{"_scope": "JE", "grau": "JE", "sistema": {"nome": "PJe"}}]
        self.repo.db.execute("UPDATE watches SET metadata=? WHERE org='local' AND number=?", (json.dumps(metadata), TRF1))
        self.repo.db.commit()
        queued = self.repo.enqueue_collection_group("local", TRF1, "initial", additional_sources=("TRF1-PJE-JE",))
        pje_job = next(row for row in queued["jobs"] if row["source"] == "TRF1-PJE-JE")
        async def read_state(expected):
            async with self.app_client() as client:
                data = (await client.get("/api/processes/overview", headers=AUTH)).json()
                self.assertEqual(data["items"][0]["state"], expected, data["items"][0])
                return data
        asyncio.run(read_state("pending"))
        self.repo.db.execute("UPDATE collection_jobs SET status='running',started_at=? WHERE id=?",
                             (now(), pje_job["id"]))
        asyncio.run(read_state("running"))
        verification_id = self.repo.create_human_verification("local", TRF1, pje_job["id"])
        self.repo.update_human_verification(verification_id, "aguardando_verificacao_humana")
        self.assertEqual(self.repo.latest_human_verification("local", TRF1)["status"], "aguardando_verificacao_humana")
        self.assertEqual(self.repo.latest_human_verification("local", TRF1)["job_id"], pje_job["id"])
        asyncio.run(read_state("human"))

    def test_filter_sort_and_pagination(self):
        self.add_watch(TRF1)
        self.add_watch(TRF5)
        async def scenario():
            async with self.app_client() as client:
                response = await client.get("/api/processes/overview?court=TRF5&sort=number&page=1&size=1", headers=AUTH)
                data = response.json()
                self.assertEqual(data["total"], 1)
                self.assertEqual(data["page"], 1)
                self.assertEqual(len(data["items"]), 1)
                self.assertEqual(data["items"][0]["court"].upper(), "TRF5")
                self.assertEqual((await client.get("/api/processes/overview?state=bogus", headers=AUTH)).status_code, 400)
        asyncio.run(scenario())

    def test_process_list_fields_and_filters_use_only_explicit_office_data(self):
        self.add_watch(TRF1)
        self.add_watch(TRF5)
        self.repo.db.execute("UPDATE watches SET metadata=? WHERE org='local' AND number=?",
                             (json.dumps([{"_scope": "JE", "grau": "JE", "sistema": {"nome": "PJe"}}]), TRF1))
        self.repo.save_court_snapshot("local", TRF1, "PJe/TRF1", {
            "situation": "Em andamento",
            "participants": [
                {"name": "Parte Exemplo", "kind": "parte", "polo": "ativo", "role": "AUTOR"},
                {"name": "Parte Contrária", "kind": "parte", "polo": "passivo", "role": "RÉU"},
                {"name": "Advogada Exemplo", "kind": "advogado"},
            ],
            "movements": [{"source_id": "m-public-1", "occurred_at": "2026-10-01T14:32:00-03:00",
                           "name": "Despacho público", "description": "Despacho público"}],
        }, status="consulta concluída")

        async def scenario():
            async with self.app_client() as client:
                response = await client.get("/api/processes/overview?lawyer=Advogada%20Exemplo&size=10", headers=AUTH)
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual(data["total"], 1)
                item = data["items"][0]
                self.assertEqual(item["parties"], ["Parte Contrária", "Parte Exemplo"])
                self.assertEqual(item["active_parties"], ["Parte Exemplo"])
                self.assertEqual(item["lawyers"], ["Advogada Exemplo"])
                self.assertEqual(item["situation"], "Em tramitação")
                self.assertEqual(item["last_movement"]["description"], "Despacho público")
                self.assertEqual(item["last_movement"]["at"], "2026-10-01T14:32:00-03:00")
                self.assertEqual((await client.get("/api/processes/overview?monitoring_status=invalid", headers=AUTH)).status_code, 400)
                # TRF5 has no direct cover in this fixture: no parties or lawyers are invented.
                trf5 = await client.get("/api/processes/overview?court=TRF5", headers=AUTH)
                self.assertEqual(trf5.json()["items"][0]["parties"], [])
                self.assertEqual(trf5.json()["items"][0]["lawyers"], [])
        asyncio.run(scenario())

    def test_list_and_details_keep_recipient_visible_without_promoting_to_party(self):
        self.add_watch(TRF1)
        self.repo.db.execute("UPDATE watches SET metadata=? WHERE org='local' AND number=?",
            (json.dumps([{"tribunal":"TRF1","_scope":"JE","grau":"JE","sistema":{"nome":"PJe"}}]), TRF1))
        self.repo.save_court_snapshot("local", TRF1, "PJe/TRF1", {
            "participants": [
                {"name":"Ana Requerente","kind":"parte","polo":"ativo","role":"REQUERENTE","source":"PJe/TRF1"},
                {"name":"Instituto Demandado","kind":"parte","polo":"passivo","role":"REQUERIDO","source":"PJe/TRF1"},
                {"name":"Rafael da Silva Braga","kind":"advogado","oab":"66895","uf":"RS","source":"PJe/TRF1"},
            ], "movements": [], "documents": [], "hearings": []}, status="consulta concluída")
        self.repo.save_communications("local", TRF1, [{"source_id":"pub-people","participants":[
            {"participant_id":"lawyer-publication","original_name":"Rafael da Silva Braga",
             "normalized_name":"rafael da silva braga","kind":"advogado","kinds":["advogado"],
             "oab":"66895","uf":"RS","source":"DJEN_PUBLICACAO","evidence_id":"pub-people"},
            {"participant_id":"recipient-publication","original_name":"Elis Regina Rodrigues de Rodrigues",
             "normalized_name":"elis regina rodrigues de rodrigues","kind":"destinatário","kinds":["destinatário"],
             "polo":"P","source":"DJEN_PUBLICACAO","evidence_id":"pub-people"},
        ]},{"source_id":"pub-people-2","participants":[
            {"participant_id":"lawyer-publication-2","original_name":"Rafael da Silva Braga",
             "normalized_name":"rafael da silva braga","kind":"advogado","kinds":["advogado"],
             "oab":"66895","uf":"RS","source":"DJEN_PUBLICACAO","evidence_id":"pub-people-2"},
            {"participant_id":"recipient-publication-2","original_name":"Elis Regina Rodrigues de Rodrigues",
             "normalized_name":"elis regina rodrigues de rodrigues","kind":"destinatário","kinds":["destinatário"],
             "polo":"P","source":"DJEN_PUBLICACAO","evidence_id":"pub-people-2"},
        ]}], status="período consultado")

        async def scenario():
            async with self.app_client() as client:
                overview=(await client.get("/api/processes/overview",headers=AUTH)).json()["items"][0]
                details=(await client.get(f"/api/processos/{TRF1}/partes",headers=AUTH)).json()
                self.assertTrue(details.get("participants"))
                self.assertTrue(all(item.get("participant_id") for item in details["participants"]))
                self.assertEqual(overview["active_parties"],["Ana Requerente"])
                self.assertEqual(overview["parties"],["Ana Requerente","Instituto Demandado"])
                self.assertIn("Rafael da Silva Braga",overview["lawyers"])
                self.assertNotIn("Elis Regina Rodrigues de Rodrigues",overview["parties"])
                self.assertEqual({item["nome"] for item in details["partes"]},
                                 {"Ana Requerente","Instituto Demandado"})
                elis=next(item for item in details["destinatarios"] if item["original_name"]=="Elis Regina Rodrigues de Rodrigues")
                self.assertEqual(elis["communication_role"],"destinatário da comunicação")
                self.assertEqual(elis["polo_code_original"],"P")
                self.assertIsNone(elis["polo_normalized"])
                self.assertEqual(elis["polo_mapping_status"],"mni_2_0_compatibility_unconfirmed_for_djen")
                self.assertEqual([item["original_name"] for item in details["destinatarios"]],
                                 ["Elis Regina Rodrigues de Rodrigues"])
                self.assertEqual(details["destinatarios"][0]["mention_count"], 2)
                self.assertEqual(set(details["destinatarios"][0]["evidence_ids"]),
                                 {"pub-people", "pub-people-2"})
                self.assertEqual(len([item for item in details["advogados"]
                                      if item["original_name"]=="Rafael da Silva Braga"]),1)
                self.assertEqual(details["destinatarios"][0]["identity_status"],
                                 "probable_case_name_kind")
        asyncio.run(scenario())

    def test_six_parties_remain_visible_when_only_one_active_pole_is_confirmed(self):
        self.add_watch(TRF1)
        self.repo.db.execute("UPDATE watches SET metadata=? WHERE org='local' AND number=?",
            (json.dumps([{"tribunal":"TRF1","_scope":"JE","grau":"JE","sistema":{"nome":"PJe"}}]), TRF1))
        people=[{"name":f"Pessoa {i}","kind":"parte","source":"PJe/TRF1",
                 **({"polo":"ativo","role":"AUTORA"} if i==1 else {})} for i in range(1,7)]
        self.repo.save_court_snapshot("local",TRF1,"PJe/TRF1",{"participants":people},status="consulta concluída")
        async def scenario():
            async with self.app_client() as client:
                overview=(await client.get("/api/processes/overview",headers=AUTH)).json()["items"][0]
                details=(await client.get(f"/api/processos/{TRF1}/partes",headers=AUTH)).json()
                self.assertEqual(overview["party_summary"]["name"],"Pessoa 1")
                self.assertEqual(overview["party_summary"]["label"],"Polo ativo")
                self.assertEqual(len(details["partes"]),6)
                self.assertEqual(len([p for p in details["participants"] if "parte" in p["kinds"]]),6)
        asyncio.run(scenario())

    def test_participant_in_older_partial_snapshot_is_not_lost_from_details_api(self):
        self.add_watch(TRF1)
        self.repo.db.execute("UPDATE watches SET metadata=? WHERE org='local' AND number=?",
            (json.dumps([{"tribunal":"TRF1","_scope":"JE","grau":"JE","sistema":{"nome":"PJe"}}]), TRF1))
        self.repo.save_court_snapshot("local",TRF1,"PJe/TRF1",{"participants":[
            {"name":"Parte preservada em captura anterior","kind":"parte","source":"PJe/TRF1"},
            {"name":"Parte da captura mais nova","kind":"parte","source":"PJe/TRF1"}
        ]},status="consulta concluída")
        self.repo.save_court_snapshot("local",TRF1,"PJe/TRF1",{"participants":[
            {"name":"Parte da captura mais nova","kind":"parte","source":"PJe/TRF1"}
        ]},status="consulta parcial")
        async def scenario():
            async with self.app_client() as client:
                details=(await client.get(f"/api/processos/{TRF1}/partes",headers=AUTH)).json()
                names={item["nome"] for item in details["partes"]}
                self.assertEqual(names,{"Parte preservada em captura anterior","Parte da captura mais nova"})
                self.assertEqual(len(details["participants"]),2)
        asyncio.run(scenario())

    def test_unknown_poles_use_identified_party_summary_and_details_show_every_party(self):
        self.add_watch(TRF1)
        self.repo.db.execute("UPDATE watches SET metadata=? WHERE org='local' AND number=?",
            (json.dumps([{"tribunal":"TRF1","_scope":"JE","grau":"JE","sistema":{"nome":"PJe"}}]), TRF1))
        people=[{"name":f"Parte {i}","kind":"parte","source":"PJe/TRF1"} for i in range(1,7)]
        self.repo.save_court_snapshot("local",TRF1,"PJe/TRF1",{"participants":people},status="consulta concluída")
        async def scenario():
            async with self.app_client() as client:
                overview=(await client.get("/api/processes/overview",headers=AUTH)).json()["items"][0]
                details=(await client.get(f"/api/processos/{TRF1}/partes",headers=AUTH)).json()
                self.assertEqual(overview["party_summary"]["label"],"Parte identificada")
                self.assertEqual(overview["party_summary"]["additional_count"],5)
                self.assertEqual(len(details["partes"]),6)
        asyncio.run(scenario())

    def test_all_confirmed_active_and_passive_parties_are_returned_and_summary_counts_authors(self):
        self.add_watch(TRF1)
        self.repo.db.execute("UPDATE watches SET metadata=? WHERE org='local' AND number=?",
            (json.dumps([{"tribunal":"TRF1","_scope":"JE","grau":"JE","sistema":{"nome":"PJe"}}]), TRF1))
        people=([{"name":f"Autora {i}","kind":"parte","polo":"ativo","role":"AUTORA","source":"PJe/TRF1"} for i in range(1,4)]
                +[{"name":f"Réu {i}","kind":"parte","polo":"passivo","role":"RÉU","source":"PJe/TRF1"} for i in range(1,3)])
        self.repo.save_court_snapshot("local",TRF1,"PJe/TRF1",{"participants":people},status="consulta concluída")
        async def scenario():
            async with self.app_client() as client:
                overview=(await client.get("/api/processes/overview",headers=AUTH)).json()["items"][0]
                details=(await client.get(f"/api/processos/{TRF1}/partes",headers=AUTH)).json()
                self.assertEqual(overview["party_summary"]["additional_count"],4)
                self.assertEqual(len(details["partes"]),5)
        asyncio.run(scenario())

    def test_lawyer_without_party_link_remains_in_table_and_details(self):
        self.add_watch(TRF1)
        self.repo.db.execute("UPDATE watches SET metadata=? WHERE org='local' AND number=?",
            (json.dumps([{"tribunal":"TRF1","_scope":"JE","grau":"JE","sistema":{"nome":"PJe"}}]), TRF1))
        self.repo.save_court_snapshot("local",TRF1,"PJe/TRF1",{"participants":[
            {"name":"Advogada sem vínculo","kind":"advogado","oab":"12345","uf":"SP","source":"PJe/TRF1"}
        ]},status="consulta concluída")
        async def scenario():
            async with self.app_client() as client:
                overview=(await client.get("/api/processes/overview",headers=AUTH)).json()["items"][0]
                details=(await client.get(f"/api/processos/{TRF1}/partes",headers=AUTH)).json()
                self.assertIsNone(overview["party_summary"]["name"])
                self.assertEqual(overview["lawyers"],["Advogada sem vínculo"])
                self.assertEqual([p["original_name"] for p in details["advogados"]],["Advogada sem vínculo"])
                self.assertEqual(details["partes"],[])
        asyncio.run(scenario())

    def test_trf4_court_snapshot_participants_are_included_in_details_api(self):
        self.add_watch(TRF4)
        self.repo.save_court_snapshot("local",TRF4,"eproc/TRF4",{"participants":[
            {"name":"Parte identificada TRF4","kind":"parte","source":"eproc/TRF4"},
            {"name":"Advogado identificado TRF4","kind":"advogado","source":"eproc/TRF4"},
        ]},status="captura autorizada")
        async def scenario():
            async with self.app_client() as client:
                details=(await client.get(f"/api/processos/{TRF4}/partes",headers=AUTH)).json()
                self.assertEqual([p["nome"] for p in details["partes"]],["Parte identificada TRF4"])
                self.assertEqual([p["original_name"] for p in details["advogados"]],["Advogado identificado TRF4"])
        asyncio.run(scenario())

    def test_public_cover_access_is_separate_from_formal_secrecy(self):
        self.add_watch(TRF1)
        self.repo.db.execute("UPDATE watches SET metadata=? WHERE org='local' AND number=?",
            (json.dumps([{"tribunal":"TRF1","_scope":"JE","grau":"JE",
                          "sistema":{"nome":"PJe"}}]), TRF1))
        self.repo.save_court_snapshot("local", TRF1, "PJe/TRF1",
            {"participants": [{"name": "Parte Exemplo", "kind": "parte"}]},
            status="consulta concluída")
        async def scenario():
            async with self.app_client() as client:
                general = (await client.get(f"/api/processos/{TRF1}", headers=AUTH)).json()
                self.assertTrue(general["acesso_publico_observado"])
                self.assertEqual(general["acesso_publico_fonte"], "PJe/TRF1")
                self.assertNotEqual(general.get("segredo_justica"), "Não")
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

