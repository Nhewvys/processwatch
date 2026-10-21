"""TRF1/PJe parser, routing, queue persistence, and API regression tests."""
import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx

from processwatch.api import create_app
from processwatch.cnj import format_number
from processwatch.config import Config
from processwatch.consolidation import consolidate
from processwatch.consolidation import process_metadata
from processwatch.engine import Engine
from processwatch.jobs import SQLiteCollectionJobRunner
from processwatch.providers.contracts import CollectionOutcome, ConnectorEvidence, ConnectorResult, ConnectorState
from processwatch.providers.pje_trf1 import PJeTRF1Connector, _clean_participant_text, classify_browser_error, parse_detail_html
from processwatch.repository import SQLiteRepository
from processwatch.routing import route_process
from test_api import NUMBER


FIXTURE = f"""<!doctype html><html><body>
<table><tr><td><div>Número Processo</div></td><td>{format_number(NUMBER)}</td>
<td><div>Data da Distribuição</div></td><td>15/01/2025</td></tr>
<tr><td><div>Classe Judicial</div>PROCEDIMENTO DO JUIZADO ESPECIAL CÍVEL (436)</td>
<td><div>Assunto</div>DIREITO PREVIDENCIÁRIO (195) - Aposentadoria Rural (6098)</td></tr>
<tr><td><div>Jurisdição</div>Subseção Judiciária de Exemplo</td>
<td><div>Órgão Julgador</div>Juizado Especial Federal Endereço Rua Pública, 1</td></tr></table>
<table><tr><td><div>Valor da Causa</div></td><td>R$ 12.345,67</td>
<td><div>Segredo de Justiça</div></td><td>Não</td>
<td><div>Justiça Gratuita</div></td><td>Sim</td></tr></table>
<table id="j_id150:processoPartesPoloAtivoResumidoTableBinding"><tbody>
<tr><th>Participante</th><th>Situação</th></tr>
<tr><td>Parte Exemplo - CPF: 123.456.789-00 (AUTOR)</td><td>Ativo</td></tr>
<tr><td>Advogada Exemplo - OAB BA68297 - CPF: 987.654.321-00 (ADVOGADO)</td><td>Ativo</td></tr>
</tbody></table>
<table id="j_id150:processoPartesPoloPassivoResumidoTableBinding"><tbody>
<tr><th>Participante</th><th>Situação</th></tr><tr><td>Entidade Exemplo (RÉU)</td><td>Ativo</td></tr>
</tbody></table>
<table id="j_id150:processoParteOutrosInteressadosResumidoTableBinding"><tbody></tbody></table>
<table id="j_id150:processoEvento"><tbody>
<tr><th>Movimento</th><th>Documento</th></tr>
<tr><td>24/04/2026 09:25:07 - Arquivado Definitivamente</td><td></td></tr>
<tr><td>23/04/2026 20:18:53 - Decorrido prazo da parte em 22/04/2026 23:59.</td><td></td></tr>
</tbody></table><div>2 resultados encontrados</div>
<table id="j_id150:j_id568:j_id569"><tr><td>1</td><td>1</td></tr></table>
<table id="j_id150:processoDocumentoGridTab"><tbody><tr><th>Documento</th><th>Tipo</th></tr>
<tr><td>28/03/2026 - SENTENÇA <a title="Visualizar" onclick="openPopUp('doc','?idProcessoDoc=DOC-1')">ver</a></td><td>Sentença</td></tr></tbody></table>
</body></html>"""


class FakePJe:
    source_ids = {"TRF1-PJE-JE"}

    async def collect(self, request):
        data = parse_detail_html(FIXTURE, request.number, request.source)
        return ConnectorResult(request.source, ConnectorState.OPERATIONAL, True, data,
            coverage=data["coverage"], evidence=(ConnectorEvidence(request.source, method="fixture"),))


class PJeParserTests(unittest.TestCase):
    def test_browser_failures_have_distinct_sanitized_categories(self):
        empty = classify_browser_error(RuntimeError("net::ERR_EMPTY_RESPONSE at https://example.test/?secret=abc"))
        timeout = classify_browser_error(TimeoutError("navigation timeout"))
        captcha = classify_browser_error(RuntimeError("CAPTCHA challenge"))
        forbidden = classify_browser_error(RuntimeError("HTTP 403"))
        self.assertEqual(empty[0:1], ("err_empty_response",))
        self.assertEqual(empty[2], ConnectorState.EMPTY_RESPONSE)
        self.assertNotIn("secret", empty[1])
        self.assertEqual(timeout[0], "timeout")
        self.assertEqual(captcha[2], ConnectorState.CAPTCHA)
        self.assertFalse(captcha[3])
        self.assertEqual(forbidden[2], ConnectorState.UNAUTHORIZED)
        self.assertFalse(forbidden[3])

    def test_parser_preserves_original_movements_and_participant_documents(self):
        data = parse_detail_html(FIXTURE, NUMBER)
        self.assertEqual(data["distribution_date"], "2025-01-15")
        self.assertEqual(data["class_code"], "436")
        self.assertEqual(data["court_organ"]["name"], "Juizado Especial Federal")
        self.assertEqual(data["case_value"], "R$ 12.345,67")
        self.assertEqual(data["judicial_secrecy"], False)
        self.assertEqual(data["judicial_secrecy_evidence"], "campo explícito da capa processual pública")
        self.assertEqual(data["legal_aid"], True)
        self.assertEqual(len(data["participants"]), 3)
        lawyer = next(item for item in data["participants"] if item["kind"] == "advogado")
        self.assertEqual((lawyer["oab"], lawyer["uf"]), ("68297", "BA"))
        party = next(item for item in data["participants"] if item["kind"] == "parte")
        self.assertEqual(party["cpf"], "12345678900")
        self.assertEqual(party["document_type"], "cpf")
        self.assertEqual(len(data["movements"]), 2)
        self.assertEqual(data["movements"][0]["name"], "Arquivado Definitivamente")
        self.assertNotIn("situation", {key for key, value in data.items() if value})
        self.assertTrue(data["documents"][0]["metadata_only"])
        self.assertEqual(data["documents"][0]["source_id"], "DOC-1")
        self.assertFalse(data["documents"][0]["public_link_candidate_available"])
        self.assertIsNone(data["documents"][0]["url"])
        self.assertEqual(data["coverage"]["movement_count"], 2)

    def test_unknown_secrecy_is_not_inferred_from_public_process_lookup(self):
        html = FIXTURE.replace('<div>Segredo de Justiça</div></td><td>Não</td>',
                               '<div>Outro campo</div></td><td>Sem informação</td>')
        data = parse_detail_html(html, NUMBER)
        self.assertIsNone(data["judicial_secrecy"])
        self.assertIsNone(data["judicial_secrecy_evidence"])

    def test_process_metadata_preserves_value_and_tri_state_secrecy_provenance(self):
        watch={"number":NUMBER,"court":"trf1","metadata":"[]","last_checked":None}
        details=process_metadata(watch,[],source_data={"case_value":"R$ 12.345,67","judicial_secrecy":False},
                                 source_label="PJe/TRF1",source_collected_at="2026-10-13T12:00:00+00:00")
        self.assertEqual(details["valor_da_causa"],"R$ 12.345,67")
        self.assertEqual(details["valor_da_causa_fonte"],"PJe/TRF1")
        self.assertEqual(details["segredo_justica"],"Não")
        self.assertEqual(details["segredo_justica_fonte"],"PJe/TRF1")
        unknown=process_metadata(watch,[])
        self.assertNotIn("segredo_justica", unknown)
        self.assertNotIn("segredo_justica_fonte", unknown)

    def test_document_href_is_only_a_candidate_until_public_access_is_verified(self):
        html = FIXTURE.replace('onclick="openPopUp(\'doc\',\'?idProcessoDoc=DOC-1\')"',
                               'href="https://pje1g-consultapublica.trf1.jus.br/documento?id=123456" '
                               'onclick="openPopUp(\'doc\',\'?idProcessoDoc=123456\')"')
        html = html.replace("<td>Sentença</td>", "<td>Sentença<script>function abrirPopUp(){}</script></td>")
        data = parse_detail_html(html, NUMBER)
        document = data["documents"][0]
        self.assertEqual(document["source_id"], "123456")
        self.assertTrue(document["public_link_candidate_available"])
        self.assertFalse(document["public_link_validated"])
        self.assertEqual(document["url"],
            "https://pje1g-consultapublica.trf1.jus.br/documento?id=123456")
        self.assertFalse(document["public_link_validated"])
        self.assertEqual(document["type"], "Sentença")

    def test_movement_document_id_is_captured_and_bound_to_its_movement(self):
        html = FIXTURE.replace(
            '<td>24/04/2026 09:25:07 - Arquivado Definitivamente</td><td></td>',
            '<td>24/04/2026 09:25:07 - Arquivado Definitivamente</td>'
            '<td><a title="Visualizar" href="?idProcessoDoc=DOC-77">Certidão</a></td>',
        )
        data = parse_detail_html(html, NUMBER)
        movement = data["movements"][0]
        self.assertEqual(len(movement["documents"]), 1)
        self.assertEqual(movement["documents"][0]["source_id"], "DOC-77")
        self.assertEqual(movement["documents"][0]["reference"], movement["source_id"])
        self.assertEqual(movement["documents"][0]["url"],
            "https://pje1g-consultapublica.trf1.jus.br/consultapublica/ConsultaPublica/?idProcessoDoc=DOC-77")
        self.assertFalse(movement["documents"][0]["public_link_validated"])

    def test_document_id_extraction_rejects_untrusted_absolute_hosts(self):
        html = FIXTURE.replace(
            'onclick="openPopUp(\'doc\',\'?idProcessoDoc=DOC-1\')"',
            'href="https://malicious.example/?idProcessoDoc=LEAK" '
            'onclick="openPopUp(\'doc\',\'?idProcessoDoc=DOC-1\')"',
        )
        # An onclick emitted by the portal remains a source reference, but an
        # untrusted href never becomes a public-link candidate.
        document = parse_detail_html(html, NUMBER)["documents"][0]
        self.assertEqual(document["source_id"], "DOC-1")
        self.assertFalse(document["public_link_candidate_available"])
        self.assertIsNone(document["url"])

    def test_parser_rejects_a_detail_for_a_different_process(self):
        base = "1000237" + NUMBER[7:]
        other = base[:7] + f"{98 - int(base[:7] + base[9:] + '00') % 97:02d}" + base[9:]
        with self.assertRaisesRegex(ValueError, "não corresponde"):
            parse_detail_html(FIXTURE, other)

    def test_missing_count_is_partial_coverage_not_zero_movements(self):
        data = parse_detail_html(FIXTURE.replace("2 resultados encontrados", ""), NUMBER)
        self.assertFalse(data["coverage"]["movement_total_known"])
        self.assertFalse(data["coverage"]["complete"])

    def test_connector_refuses_unconfirmed_routing_fields(self):
        async def scenario():
            connector = PJeTRF1Connector()
            result = await connector.collect(type("Request", (), {
                "number": NUMBER, "source": "TRF1-PJE-JE", "tribunal": "TRF4",
                "system": "eproc", "degree": "JE"})())
            self.assertEqual(result.state, ConnectorState.NOT_VERIFIED)
        asyncio.run(scenario())

    def test_structured_roles_are_parsed_without_personal_ids(self):
        lawyer = _clean_participant_text("Advogada Exemplo - OAB BA 68297 - CPF: 111.222.333-44 (ADVOGADO)", "ativo")
        party = _clean_participant_text("Parte Exemplo (AUTOR)", "ativo")
        self.assertEqual((lawyer["kind"], lawyer["oab"], lawyer["uf"]), ("advogado", "68297", "BA"))
        self.assertNotIn("CPF", str(lawyer))
        self.assertEqual((party["kind"], party["role"]), ("parte", "AUTOR"))

    def test_captcha_is_a_barrier_not_an_empty_search(self):
        class Locator:
            async def count(self):
                return 0
        class Page:
            def locator(self, _selector):
                return Locator()
        async def scenario():
            self.assertTrue(await PJeTRF1Connector._captcha_visible(Page(), "Complete o CAPTCHA para continuar"))
            self.assertFalse(await PJeTRF1Connector._captcha_visible(Page(), "Consulta concluída; processo encontrado"))
        asyncio.run(scenario())


class PJeMotorIntegrationTests(unittest.TestCase):
    def _repo(self, path):
        repo = SQLiteRepository(str(path))
        repo.register("local", NUMBER, "trf1", 3600, 100)
        with repo.db:
            repo.db.execute("UPDATE watches SET metadata=? WHERE org='local' AND number=?",
                ('[{"tribunal":"TRF1","sistema":{"nome":"PJe"},"grau":"JE"}]', NUMBER))
        return repo

    def test_snapshot_with_explicit_negative_secrecy_remains_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            repo=self._repo(Path(folder)/"explicit-secrecy.sqlite3")
            try:
                repo.save_court_snapshot("local",NUMBER,"PJe/TRF1",{
                    "judicial_secrecy":False,
                    "judicial_secrecy_evidence":"campo explícito da capa pública",
                },status="consulta concluída")
                saved=repo.latest_court_snapshot("local",NUMBER)
                self.assertIsNotNone(saved)
                self.assertIs(saved["data"]["judicial_secrecy"],False)
            finally:
                repo.close()

    def test_router_uses_verified_trf1_pje_je_combination(self):
        result = route_process(NUMBER, {"tribunal": "TRF1", "sistema": "PJe", "grau": "JE"},
                               connector_ids={"TRF1-PJE-JE"})
        self.assertEqual(result["status"], "routed")
        self.assertEqual(result["selected_connector"], "TRF1-PJE-JE")

    def test_case_details_returns_source_value_secrecy_and_public_query_url(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                repo=self._repo(Path(folder)/"detail.sqlite3")
                try:
                    repo.save_court_snapshot("local",NUMBER,"PJe/TRF1",{
                        "case_value":"R$ 12.345,67","case_value_evidence":"capa processual pública",
                        "judicial_secrecy":False,"judicial_secrecy_evidence":"campo explícito da capa processual pública",
                        "participants":[],"movements":[],"documents":[],"hearings":[]},status="consulta concluída")
                    engine=Engine(repo,type("Provider",(),{"fetch":lambda *a:{}})())
                    app=create_app(repo,engine,config=Config(access_token="case-detail-test"))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test",
                                                 headers={"Authorization":"Bearer case-detail-test"}) as client:
                        data=(await client.get(f"/api/processos/{NUMBER}")).json()
                        self.assertEqual(data["valor_da_causa"],"R$ 12.345,67")
                        self.assertEqual(data["valor_da_causa_fonte"],"PJe/TRF1")
                        self.assertEqual(data["segredo_justica"],"Não")
                        self.assertEqual(data["segredo_justica_fonte"],"PJe/TRF1")
                        self.assertEqual(data["public_consultation_url"],
                            "https://pje1g-consultapublica.trf1.jus.br/consultapublica/ConsultaPublica/listView.seam")
                finally:
                    repo.close()
        asyncio.run(scenario())

    def test_job_stores_snapshot_and_consolidates_pje_events(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = self._repo(Path(folder) / "db.sqlite3")
            try:
                repo.enqueue_collection_group("local", NUMBER, "initial", include_datajud=False,
                    include_djen=False, additional_sources=["TRF1-PJE-JE"])
                job = repo.claim_collection_job("local", "test-worker")
                self.assertEqual(job["source"], "TRF1-PJE-JE")
                runner = SQLiteCollectionJobRunner(Engine(repo, type("Provider", (), {"fetch": lambda *a: {}})()),
                    repo, object(), Config(access_token="test-token"), pje_client=FakePJe())
                outcome = runner.run(job)
                self.assertEqual(outcome.status, "completed")
                saved = repo.latest_court_snapshot("local", NUMBER)
                self.assertEqual(len(saved["data"]["movements"]), 2)
                events = consolidate(NUMBER, [], [], repo, court_snapshots=[saved["data"]])
                self.assertEqual(len(events), 2)
                self.assertTrue(all(item["fonte"] == "PJe/TRF1" for item in events))
                self.assertEqual(repo.latest_source_check("local", NUMBER, "PJe/TRF1")["status"], "consulta concluída")
                second = repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", saved["data"], status="consulta concluída")
                self.assertEqual(second, saved["source_id"])
            finally:
                repo.close()

    def test_api_combines_only_oab_confirmed_identity_and_exposes_source_events(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                repo = self._repo(Path(folder) / "db.sqlite3")
                try:
                    parsed = parse_detail_html(FIXTURE, NUMBER)
                    for person in parsed["participants"]:
                        person["participant_id"] = person["name"]
                        person["normalized_name"] = person["name"].casefold()
                    repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", parsed,
                                             status="consulta concluída", coverage=parsed["coverage"])
                    app = create_app(repo, Engine(repo, type("Provider", (), {"fetch": lambda *a: {}})()),
                                     config=Config(access_token="test-token"))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization": "Bearer test-token"}) as client:
                        home = await client.get("/")
                        script = await client.get("/assets/app.js")
                        self.assertIn("/assets/app.js", home.text)
                        self.assertIn("sourcesOf=item=>item.fontes?.length?item.fontes:[item.fonte]", script.text)
                        self.assertNotIn("<option>PJe/TRF1</option>", script.text)
                        parties = (await client.get(f"/api/processos/{NUMBER}/partes")).json()
                        self.assertEqual(len(parties["participants"]), 3)
                        moves = (await client.get(f"/api/processos/{NUMBER}/movimentacoes")).json()
                        self.assertEqual(len(moves["items"]), 2)
                        self.assertEqual({row["source"] for row in moves["items"]}, {"PJe/TRF1"})
                        general = (await client.get(f"/api/processos/{NUMBER}")).json()
                        self.assertEqual(general["fontes"]["Consulta direta ao tribunal"], "completed")
                finally:
                    repo.close()
        asyncio.run(scenario())

    def test_failed_attempt_keeps_last_successful_snapshot_visible(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                repo = self._repo(Path(folder) / "db.sqlite3")
                try:
                    parsed = parse_detail_html(FIXTURE, NUMBER)
                    for person in parsed["participants"]:
                        person["participant_id"] = person["name"]
                        person["normalized_name"] = person["name"].casefold()
                    repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", parsed,
                        status="consulta concluída", coverage=parsed["coverage"])
                    repo.record_court_attempt("local", NUMBER, "PJe/TRF1", status="court_unavailable",
                        coverage={"movement_count": 0},
                        error={"code": "court_unavailable", "message": "Portal temporariamente indisponível."})
                    app = create_app(repo, Engine(repo, type("Provider", (), {"fetch": lambda *a: {}})()),
                                     config=Config(access_token="test-token"))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization": "Bearer test-token"}) as client:
                        general = (await client.get(f"/api/processos/{NUMBER}")).json()
                        self.assertIsNotNone(general["pje_publico"])
                        self.assertEqual(general["fontes"]["Consulta direta ao tribunal"], "failed")
                        parties = (await client.get(f"/api/processos/{NUMBER}/partes")).json()
                        self.assertGreater(len(parties["participants"]), 0)
                        moves = (await client.get(f"/api/processos/{NUMBER}/movimentacoes")).json()
                        self.assertGreater(len(moves["items"]), 0)
                        sync = (await client.get(f"/api/processes/{NUMBER}/sync")).json()
                        self.assertTrue(any(job["status"] == "failed" for job in sync["jobs"]
                                            if job["source"] == "PJe/TRF1"))
                finally:
                    repo.close()
        asyncio.run(scenario())

    def test_authenticated_api_resumes_pending_pje_and_exposes_human_state(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as folder:
                repo = self._repo(Path(folder) / "db.sqlite3")
                try:
                    repo.enqueue_collection_group("local", NUMBER, "initial", include_datajud=False,
                        include_djen=False, additional_sources=["TRF1-PJE-JE"])
                    job = repo.claim_collection_job("local", "test-worker")
                    repo.finish_collection_job(job["id"], job["lease_token"], status="awaiting_access",
                        error={"code":"captcha_identified","message":"verification required","retryable":False})
                    app = create_app(repo, Engine(repo, type("Provider", (), {"fetch": lambda *a: {}})() ),
                                     config=Config(access_token="test-token"))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as anon:
                        self.assertEqual((await anon.post(f"/api/processos/{NUMBER}/pje/retomar")).status_code, 401)
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization":"Bearer test-token"}) as client:
                        resumed = await client.post(f"/api/processos/{NUMBER}/pje/retomar")
                        self.assertEqual(resumed.status_code, 202)
                        self.assertEqual(repo.collection_jobs("local", NUMBER)[0]["status"], "queued")
                        params = __import__("json").loads(repo.collection_jobs("local", NUMBER)[0]["params"])
                        self.assertTrue(params["human_assist"])
                        status = (await client.get(f"/api/processes/{NUMBER}/sync")).json()
                        self.assertEqual(status["status"], "aguardando_verificacao_humana")
                        self.assertIn("verification_id", resumed.json())
                        home = await client.get("/")
                        script = await client.get("/assets/app.js")
                        self.assertIn("/assets/app.js", home.text)
                        self.assertIn("janela local do navegador", script.text)
                        self.assertIn("Abrir consulta assistida no navegador", script.text)
                        cancelled = await client.post(f"/api/processos/{NUMBER}/pje/retomada/{resumed.json()['verification_id']}/cancelar")
                        self.assertEqual(cancelled.json()["status"], "cancelled")
                finally:
                    repo.close()
        asyncio.run(scenario())


