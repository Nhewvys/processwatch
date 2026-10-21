"""Deterministic unit tests for source reconciliation and public documents."""
import asyncio
import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import httpx
from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.event_reconciliation import _families_in_text, _official_tpu_families, reconcile_movements
from processwatch.consolidation import _safe_documents
from processwatch.normalizer import normalize_response
from processwatch.repository import SQLiteRepository


NUMBER = "00000003800004010000"


def event(source, title, when="2026-07-01T10:15:30+00:00", *, code=None, complements=None,
          tpu=None, documents=None, original=None):
    prefix = "pje" if source == "PJe/TRF1" else "datajud"
    return {
        "id": f"{prefix}:{title}:{when}", "number": "00000003800004010000",
        "tipo": "movimentacao_tribunal" if prefix == "pje" else "movimentacao",
        "titulo": title, "original_title": title, "descricao": title,
        "occurred_at": when, "temporal_context": "data da movimentação",
        "fonte": source, "fontes": [source], "code": code,
        "complements": complements or [], "complement_display": [], "tpu": tpu,
        "documents": documents or [], "original": original or {"source_title": title, "source": source},
        "related_to": [],
    }


def official_tpu(code, name, description, parents):
    return {"code": str(code), "official_name": name, "official_description": description,
            "version": "SGT-2026-10-06", "source": "https://www.cnj.jus.br/sgt/sgt_ws.php?wsdl",
            "parent_codes": parents}


class PhaseGReconciliationTests(unittest.TestCase):
    def test_explicit_semantic_family_patterns_cover_six_requested_act_types(self):
        samples = {
            "decurso_prazo": "Decorrido prazo de uma parte em determinada data",
            "remessa": "Remetidos os Autos em grau de recurso",
            "juntada": "Juntada de certidão de intimação",
            "publicacao": "Publicação disponibilizada no diário",
            "conclusao": "Conclusos para julgamento",
            "intimacao": "Expedida intimação para a parte",
        }
        for family, description in samples.items():
            with self.subTest(family=family):
                self.assertIn(family, _families_in_text(description))

    def test_official_tpu_name_prevents_negated_description_from_creating_another_family(self):
        movement = event("DataJud", "Remessa", code="123", tpu=official_tpu(
            "123", "Remessa", "Remessa interna ou externa. Não deve ser confundida com conclusão.", ["48"]))
        self.assertEqual(_official_tpu_families(movement), {"remessa"})

    def test_official_description_can_identify_juntada_for_document_or_petition_code(self):
        movement = event("DataJud", "Petição", code="85", tpu=official_tpu(
            "85", "Petição", "Registra a juntada aos autos de determinada petição.", ["14", "48", "67"]))
        self.assertEqual(_official_tpu_families(movement), {"juntada"})

    def test_official_communication_description_matches_specific_pje_event_without_parent_codes(self):
        when = "2025-06-09T22:27:07+00:00"
        datajud = event("DataJud", "Expedida/Certificada", when, code="12282",
            tpu=official_tpu("12282", "Expedida/Certificada",
                "Indica o registro da expedição de comunicação eletrônica, podendo também ser utilizado por meio digital.", []))
        document = {"source": "PJe/TRF1", "source_id": "doc-reference",
                    "description": "09/06/2025 22:27:06 - Sentença Tipo A", "url": None}
        pje = event("PJe/TRF1", "Expedida/certificada a comunicação eletrônica", when,
                    documents=[document])
        result = reconcile_movements([datajud, pje])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["titulo"], pje["titulo"])
        self.assertEqual(result[0]["fontes"], ["DataJud", "PJe/TRF1"])
        self.assertEqual(result[0]["documents"], [document])
        self.assertIsNone(result[0]["documents"][0]["url"])
        self.assertIn("official_tpu_definition_phrase_present_in_other_source",
                      result[0]["equivalence"]["evidence"])

    def test_specific_single_word_certificate_complement_is_corrobating_evidence(self):
        when = "2025-06-09T22:27:08+00:00"
        datajud = event("DataJud", "Documento", when, code="581",
            complements=[{"codigo": 4, "descricao": "tipo_de_documento", "nome": "Certidão", "valor": 107}],
            tpu=official_tpu("581", "Documento",
                "Registra a juntada de documento aos autos.", ["14", "48"]))
        pje = event("PJe/TRF1", "Juntada de Certidão", when)
        result = reconcile_movements([datajud, pje])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["titulo"], pje["titulo"])
        self.assertEqual(result[0]["complements"], datajud["complements"])
        self.assertIn("complement_text_present_in_other_source", result[0]["equivalence"]["evidence"])

    def test_specific_court_action_beats_generic_tpu_category_even_with_more_datajud_fields(self):
        when = "2025-06-09T22:27:08+00:00"
        datajud = event("DataJud", "Documento", when, code="581",
            complements=[{"codigo": 4, "descricao": "tipo_de_documento", "nome": "Certidão", "valor": 107}],
            tpu=official_tpu("581", "Documento", "Registra a juntada de documento aos autos.", ["14", "48"]))
        datajud["titulo"] = "Documento — Certidão"
        pje = event("PJe/TRF1", "Juntada de Certidão", when)
        merged = reconcile_movements([datajud, pje])[0]
        self.assertEqual(merged["titulo"], "Juntada de Certidão")
        self.assertEqual(merged["code"], "581")
        self.assertIn(datajud["complements"][0], merged["complements"])

    def test_real_case_a_decurso_with_party_complement_uses_detailed_pje_text(self):
        when = "2025-11-06T23:59:00-03:00"
        party = "Quiteria da Silva Santos"
        datajud = event("DataJud", "Decurso de Prazo", when, code="1051",
            complements=[{"codigo": 1, "descricao": "nome_da_parte", "nome": party, "valor": party}],
            tpu=official_tpu("1051", "Decurso de Prazo", "Decorrido prazo de #{nome_da_parte} em #{data}.",
                             ["14", "48"]))
        pje = event("PJe/TRF1", f"Decorrido prazo de {party} em 06/11/2025", when)
        result = reconcile_movements([datajud, pje])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["titulo"], pje["titulo"])
        self.assertIn("timestamp_official_tpu_family:decurso_prazo", result[0]["equivalence"]["evidence"])
        self.assertEqual(len(result[0]["source_details"]), 2)

    def test_real_case_b_remittance_uses_official_code_and_matching_complement(self):
        when = "2025-09-10T14:09:34+00:00"
        datajud = event("DataJud", "Remessa", when, code="123",
            complements=[{"codigo": 18, "descricao": "motivo_da_remessa",
                          "nome": "em grau de recurso", "valor": 38}],
            tpu=official_tpu("123", "Remessa", "Indica remessa interna ou externa de autos.", ["48"]))
        pje = event("PJe/TRF1", "Remetidos os Autos (em grau de recurso) para Turma Recursal", when)
        result = reconcile_movements([datajud, pje])
        self.assertEqual(len(result), 1)
        self.assertEqual(result, reconcile_movements([datajud, pje]))
        self.assertEqual(result[0]["titulo"], pje["titulo"])
        self.assertIn("timestamp_official_tpu_family:remessa", result[0]["equivalence"]["evidence"])

    def test_events_from_different_processes_are_never_reconciled(self):
        datajud = event("DataJud", "Distribuição")
        pje = event("PJe/TRF1", "Distribuição")
        pje["number"] = "10002366920254013306"
        self.assertEqual(len(reconcile_movements([datajud, pje])), 2)

    def test_same_clear_act_family_can_merge_without_a_parent_hierarchy(self):
        datajud = event("DataJud", "Remessa", code="123", complements=[] ,
            tpu={"code": "123", "official_name": "Remessa", "version": "SGT-2026-10-06",
                 "source": "https://www.cnj.jus.br/sgt/sgt_ws.php?wsdl"})
        pje = event("PJe/TRF1", "Remetidos os Autos (em grau de recurso) para Turma Recursal")
        result = reconcile_movements([datajud, pje])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["equivalence"]["status"], "confirmed")

    def test_multiple_same_second_deadlines_stay_ambiguous_without_matching_party(self):
        when = "2025-11-06T23:59:00-03:00"
        tpu = official_tpu("1051", "Decurso de Prazo", "Decorrido prazo de parte em data", ["14", "48"])
        generic = event("DataJud", "Decurso de Prazo", when, code="1051",
                        complements=[{"nome": "06/11/2025", "valor": "06/11/2025"}], tpu=tpu)
        pje_a = event("PJe/TRF1", "Decorrido prazo de Parte A em 06/11/2025", when)
        pje_b = event("PJe/TRF1", "Decorrido prazo de Parte B em 06/11/2025", when)
        result = reconcile_movements([generic, pje_a, pje_b])
        self.assertEqual(len(result), 3)
        self.assertTrue(all(not row.get("equivalence") for row in result))

    def test_two_recipients_at_same_second_match_only_their_own_cross_source_records(self):
        when = "2025-11-06T23:59:00-03:00"
        tpu = official_tpu("1051", "Decurso de Prazo", "Decorrido prazo de parte em data", ["14", "48"])
        left = event("DataJud", "Decurso de Prazo", when, code="1051",
            complements=[{"nome": "Ana Silva", "valor": "Ana Silva"}], tpu=tpu)
        right = event("DataJud", "Decurso de Prazo", when, code="1051",
            complements=[{"nome": "Bia Souza", "valor": "Bia Souza"}], tpu=tpu)
        pje_a = event("PJe/TRF1", "Decorrido prazo de Ana Silva em 06/11/2025", when)
        pje_b = event("PJe/TRF1", "Decorrido prazo de Bia Souza em 06/11/2025", when)
        result = reconcile_movements([left, right, pje_a, pje_b])
        self.assertEqual(len(result), 2)
        self.assertTrue(all(len(row["fontes"]) == 2 for row in result))
        self.assertEqual({row["titulo"] for row in result}, {pje_a["titulo"], pje_b["titulo"]})

    def test_exact_cross_source_event_merges_and_preserves_both_originals_idempotently(self):
        rows = [event("DataJud", "Processo devolvido à Secretaria", code="12215"),
                event("PJe/TRF1", "Processo devolvido à Secretaria")]
        before = copy.deepcopy(rows)
        first = reconcile_movements(rows)
        second = reconcile_movements(rows)
        self.assertEqual(len(first), 1)
        self.assertEqual(first, second)
        self.assertEqual(first[0]["fontes"], ["DataJud", "PJe/TRF1"])
        self.assertEqual(len(first[0]["source_details"]), 2)
        self.assertEqual(rows, before)
        self.assertEqual(first[0]["source_details"][0]["original"]["source_title"],
                         "Processo devolvido à Secretaria")

    def test_generic_description_enriched_by_specific_official_complement(self):
        complement = {"nome": "tipo de documento", "valor": "Certidão de intimação"}
        datajud = event("DataJud", "Juntada de certidão", code="581", complements=[complement],
                        tpu={"name": "Documento"})
        datajud["titulo"] = "Juntada de certidão — Certidão de intimação"
        rows = [datajud,
                event("PJe/TRF1", "Juntada de certidão de intimação")]
        result = reconcile_movements(rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["titulo"], "Juntada de certidão de intimação")
        # The tribunal's original wording is selected by default; the richer
        # DataJud representation remains independently available by source.
        self.assertIn(complement, result[0]["complements"])
        self.assertEqual(result[0]["source_details"][1]["original_title"],
                         "Juntada de certidão de intimação")
        self.assertEqual(result[0]["source_details"][0]["title"],
                         "Juntada de certidão — Certidão de intimação")
        self.assertEqual(result[0]["equivalence"]["evidence"],
                         "timestamp_and_explicit_specific_complement")

    def test_same_minute_but_different_seconds_remains_distinct(self):
        rows = [event("DataJud", "Juntada de certidão", "2026-07-01T10:15:01+00:00"),
                event("PJe/TRF1", "Juntada de certidão", "2026-07-01T10:15:58+00:00")]
        self.assertEqual(len(reconcile_movements(rows)), 2)

    def test_same_timestamp_and_text_from_different_cnj_processes_never_merge(self):
        left = event("DataJud", "Juntada de certidão")
        right = event("PJe/TRF1", "Juntada de certidão")
        right["number"] = "10002366920254013306"
        self.assertEqual(len(reconcile_movements([left, right])), 2)

    def test_minute_precision_without_seconds_is_not_enough_to_merge(self):
        rows = [event("DataJud", "Juntada de certidão", "2026-07-01T10:15+00:00"),
                event("PJe/TRF1", "Juntada de certidão", "2026-07-01T10:15:00+00:00")]
        self.assertEqual(len(reconcile_movements(rows)), 2)

    def test_same_title_with_conflicting_document_complements_stays_separate(self):
        rows = [event("DataJud", "Juntada de certidão", complements=[
                    {"nome": "tipo de documento", "valor": "Certidão de intimação"}]),
                event("PJe/TRF1", "Juntada de certidão", complements=[
                    {"nome": "tipo de documento", "valor": "Certidão de publicação"}])]
        self.assertEqual(len(reconcile_movements(rows)), 2)

    def test_same_time_and_title_with_different_document_ids_and_types_stays_separate(self):
        left = event("DataJud", "Juntada de certidão", documents=[{
            "document_id": "doc-publicacao", "type": "Certidão de publicação"}])
        right = event("PJe/TRF1", "Juntada de certidão", documents=[{
            "document_id": "doc-intimacao", "type": "Certidão de intimação"}])
        self.assertEqual(len(reconcile_movements([left, right])), 2)

    def test_conflicting_official_codes_are_not_merged(self):
        left = event("DataJud", "Juntada", code="581")
        right = event("PJe/TRF1", "Juntada", code="85")
        self.assertEqual(len(reconcile_movements([left, right])), 2)

    def test_reconciliation_does_not_mutate_originals_even_when_events_stay_separate(self):
        rows = [event("DataJud", "Ato 1"), event("PJe/TRF1", "Ato 2")]
        rows[0]["related_to"] = [rows[1]["id"]]
        original = copy.deepcopy(rows)
        reconcile_movements(rows)
        self.assertEqual(rows, original)

    def test_similar_but_different_publication_and_service_certificates_do_not_merge(self):
        rows = [event("DataJud", "Juntada de certidão de publicação"),
                event("PJe/TRF1", "Juntada de certidão de intimação")]
        self.assertEqual(len(reconcile_movements(rows)), 2)

    def test_ambiguous_generic_to_two_specific_records_is_not_merged(self):
        when = "2026-07-01T10:15:30+00:00"
        generic = event("PJe/TRF1", "Juntada de certidão", when)
        intimation = event("DataJud", "Juntada de certidão de intimação", when,
                           complements=[{"nome": "tipo", "valor": "Certidão de intimação"}])
        publication = event("DataJud", "Juntada de certidão de publicação", when,
                            complements=[{"nome": "tipo", "valor": "Certidão de publicação"}])
        self.assertEqual(len(reconcile_movements([generic, intimation, publication])), 3)

    def test_events_unique_to_one_source_remain_visible(self):
        rows = [event("DataJud", "Distribuição", "2026-07-01T08:00:00+00:00"),
                event("PJe/TRF1", "Audiência realizada", "2026-07-01T09:00:00+00:00")]
        self.assertEqual(len(reconcile_movements(rows)), 2)

    def test_tie_prefers_direct_tribunal_and_information_score_prefers_details(self):
        tie = reconcile_movements([event("DataJud", "Juntada de certidão"),
                                   event("PJe/TRF1", "Juntada de certidão")])[0]
        self.assertEqual(tie["original"]["source"], "PJe/TRF1")
        specific = event("DataJud", "Juntada de certidão", code="581",
                         complements=[{"nome": "tipo", "valor": "Certidão de intimação"}],
                         tpu={"name": "Documento"})
        specific["titulo"] = "Juntada de certidão — Certidão de intimação"
        chosen = reconcile_movements([specific, event("PJe/TRF1", "Juntada de certidão")])[0]
        self.assertEqual(chosen["titulo"], "Juntada de certidão")
        self.assertEqual(chosen["preferred_source"], "PJe/TRF1")
        self.assertEqual(chosen["code"], "581")
        self.assertEqual(chosen["source_details"][0]["title"], "Juntada de certidão — Certidão de intimação")

    def test_source_filter_can_match_one_member_of_consolidated_event(self):
        merged = reconcile_movements([event("DataJud", "Distribuição"),
                                      event("PJe/TRF1", "Distribuição")])[0]
        for source in ("DataJud", "PJe/TRF1"):
            self.assertIn(source, merged["fontes"])
        with open("src/processwatch/web/index.html", encoding="utf-8") as page:
            html = page.read()
        with open("src/processwatch/web/app.js", encoding="utf-8") as script:
            html += script.read()
        self.assertIn("sourcesOf(e).includes(source)", html)
        self.assertIn("for(const sourceName of sourceNames)", html)
        self.assertNotIn("sourcesOf(event).join(' + ')", html)

    def test_confirmed_manual_relation_can_reconcile_without_mutating_originals(self):
        rows = [event("DataJud", "Ato lançado", "2026-07-01T10:00:00+00:00"),
                event("PJe/TRF1", "Ato juntado", "2026-07-01T10:02:00+00:00")]
        relation = (rows[0]["id"], rows[1]["id"])
        merged = reconcile_movements(rows, confirmed_pairs=[relation])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["equivalence"]["evidence"], "reviewed_confirmed_relation")

    def test_document_metadata_never_turns_null_or_unvalidated_url_into_a_link(self):
        safe = _safe_documents([
            {"source_id": "public-reference", "description": "Certidão", "type": "function abrirPopUp()", "url": None},
            {"source_id": "other-host", "description": "Arquivo", "url": "https://example.invalid/doc",
             "public_link_validated": True},
            {"source_id": "not-validated", "description": "Documento", "url": "https://pje1g-consultapublica.trf1.jus.br/doc"},
            {"source_id": "candidate", "description": "Certidão", "url": None,
             "public_link_candidate_available": True},
        ])
        self.assertEqual(len(safe), 4)
        self.assertTrue(all(item["url"] is None for item in safe))
        self.assertTrue(all(item["link_status"] == "Link público não disponível" for item in safe[:3]))
        self.assertEqual(safe[3]["link_status"], "Link localizado, mas acesso público não validado")
        self.assertIsNone(safe[0]["type"])

    def test_only_explicitly_validated_official_pje_url_is_openable(self):
        safe = _safe_documents([{"source": "PJe/TRF1", "source_id": "verified", "description": "Documento",
            "url": "https://pje1g-consultapublica.trf1.jus.br/consultapublica/documento?id=1",
            "public_link_validated": True}])
        self.assertEqual(safe[0]["url"], "https://pje1g-consultapublica.trf1.jus.br/consultapublica/documento?id=1")
        self.assertTrue(safe[0]["public_link_validated"])

    def test_sqlite_api_timeline_and_movements_return_one_auditable_cross_source_event(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "phase-g.sqlite3")
                repo = SQLiteRepository(path)
                try:
                    repo.register("local", NUMBER, "trf1", 3600, 100)
                    raw = {"hits": {"hits": [{"_source": {
                        "id": "datajud-case", "numeroProcesso": NUMBER, "tribunal": "TRF1", "grau": "JE",
                        "sistema": {"codigo": 1, "nome": "PJe"}, "classe": {"codigo": 436, "nome": "Classe teste"},
                        "movimentos": [{"id": "dj-move-1", "codigo": 12215,
                            "nome": "Processo devolvido à Secretaria",
                            "dataHora": "2026-07-01T10:15:30Z", "complementosTabelados": []}],
                    }}]}}
                    snapshot = normalize_response(raw, NUMBER, "trf1")
                    run_id = repo.start_run("local", NUMBER)
                    repo.apply("local", NUMBER, run_id, snapshot)
                    repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", {
                        "source": "PJe/TRF1", "movements": [
                            {"source_id": "pje-move-1", "occurred_at": "2026-07-01T10:15:30+00:00",
                             "name": "Processo devolvido à Secretaria", "documents": []},
                            {"source_id": "pje-exclusive", "occurred_at": "2026-07-01T10:18:30+00:00",
                             "name": "Audiência realizada", "documents": []}],
                        "participants": [], "documents": []}, status="consulta concluída")
                    app = create_app(repo, Engine(repo, type("Offline", (), {"fetch": lambda *args: {}})()),
                                     config=Config(db=path, access_token="phase-g-test", djen_initial_date=date.today()))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization": "Bearer phase-g-test"}) as client:
                        timeline = (await client.get(f"/api/processos/{NUMBER}/timeline")).json()
                        movements = (await client.get(f"/api/processos/{NUMBER}/movimentacoes")).json()
                        page = await client.get("/")
                        script = await client.get("/assets/app.js")
                    items = [item for item in timeline["items"] if item["tipo"] == "movimentacao"]
                    self.assertEqual(len(items), 2)
                    merged = next(item for item in items if item.get("equivalence"))
                    self.assertEqual(merged["fontes"], ["DataJud", "PJe/TRF1"])
                    self.assertEqual(len(merged["source_details"]), 2)
                    self.assertIsNone(merged["original_url"])
                    self.assertTrue(all(item["original_url"] is None for item in merged["source_details"]))
                    self.assertEqual(len(movements["items"]), 2)
                    self.assertIn("PJe/TRF1", next(item for item in movements["items"]
                        if item["name"] == "Audiência realizada")["sources"])
                    self.assertIn("/assets/app.js", page.text)
                    self.assertIn("sourcesOf(e).includes(source)", script.text)
                finally:
                    repo.close()
        asyncio.run(scenario())

    def test_api_timeline_exact_cross_source_title_keeps_pje_document_metadata_and_badges(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "exact-publication.sqlite3")
                repo = SQLiteRepository(path)
                try:
                    repo.register("local", NUMBER, "trf1", 3600, 100)
                    raw = {"hits": {"hits": [{"_source": {
                        "id": "datajud-publication", "numeroProcesso": NUMBER, "tribunal": "TRF1", "grau": "JE",
                        "sistema": {"codigo": 1, "nome": "PJe"},
                        "classe": {"codigo": 436, "nome": "Classe teste"},
                        "movimentos": [{"id": "datajud-publication-move", "codigo": 12282,
                            "nome": "Expedição de publicação ao Diário de Justiça Eletrônico Nacional",
                            "dataHora": "2025-06-09T22:27:07Z", "complementosTabelados": []}],
                    }}]}}
                    snapshot = normalize_response(raw, NUMBER, "trf1")
                    run_id = repo.start_run("local", NUMBER)
                    repo.apply("local", NUMBER, run_id, snapshot)
                    document = {"source": "PJe/TRF1", "source_id": "document-reference",
                                "description": "Sentença Tipo A", "url": None}
                    repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", {
                        "source": "PJe/TRF1", "movements": [{
                            "source_id": "pje-publication-move", "occurred_at": "2025-06-09T22:27:07+00:00",
                            "name": "Expedição de publicação ao Diário de Justiça Eletrônico Nacional",
                            "documents": [document]}], "participants": [], "documents": []},
                        status="consulta concluída")
                    app = create_app(repo, Engine(repo, type("Offline", (), {"fetch": lambda *args: {}})()),
                        config=Config(db=path, access_token="publication-g2", djen_initial_date=date.today()))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization": "Bearer publication-g2"}) as client:
                        response = await client.get(f"/api/processos/{NUMBER}/timeline")
                        page = await client.get("/")
                        script = await client.get("/assets/app.js")
                    self.assertEqual(response.status_code, 200)
                    items = [row for row in response.json()["items"] if row["tipo"] == "movimentacao"]
                    self.assertEqual(len(items), 1)
                    merged = items[0]
                    self.assertEqual(merged["titulo"], "Expedição de publicação ao Diário de Justiça Eletrônico Nacional")
                    self.assertEqual(merged["fontes"], ["DataJud", "PJe/TRF1"])
                    self.assertEqual(merged["documents"][0]["description"], "Sentença Tipo A")
                    self.assertIsNone(merged["documents"][0]["url"])
                    self.assertEqual(len(merged["source_details"]), 2)
                    self.assertEqual([row["source"] for row in merged["source_details"]],
                                     ["DataJud", "PJe/TRF1"])
                    self.assertIn("/assets/app.js", page.text)
                    self.assertIn("for(const sourceName of sourceNames)", script.text)
                finally:
                    repo.close()
        asyncio.run(scenario())

    def test_exact_title_to_two_pje_rows_with_different_evidence_stays_ambiguous(self):
        when = "2025-06-09T22:27:07+00:00"
        datajud = event("DataJud", "Expedição de publicação ao Diário de Justiça Eletrônico Nacional", when)
        pje_without_document = event("PJe/TRF1", datajud["titulo"], when)
        pje_with_document = event("PJe/TRF1", datajud["titulo"], when, documents=[{
            "source_id": "sentenca-a", "description": "Sentença Tipo A", "url": None}])
        pje_without_document["id"] = "pje-publication-without-document"
        pje_with_document["id"] = "pje-publication-with-document"
        result = reconcile_movements([datajud, pje_without_document, pje_with_document])
        self.assertEqual(len(result), 3)
        self.assertTrue(all(not row.get("equivalence") for row in result))

    def test_peticao_outros_matches_unique_pje_manifestation_and_prefers_tribunal(self):
        when = "2026-02-03T13:22:11-03:00"
        dj = event("DataJud", "Petição — Outras", when, code="85",
                   complements=[{"codigo": 1, "nome": "Tipo de petição", "valor": "Outras"}],
                   tpu=official_tpu("85", "Petição", "Registra a juntada aos autos de petição.", []))
        pje = event("PJe/TRF1", "Juntada de manifestação", when)
        result = reconcile_movements([dj, pje])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["titulo"], pje["titulo"])
        self.assertEqual(result[0]["preferred_source"], "PJe/TRF1")
        self.assertEqual(result[0]["source_details"][0]["source"], "DataJud")
        self.assertEqual(result[0]["source_details"][1]["source"], "PJe/TRF1")

    def test_peticao_outros_is_not_arbitrarily_paired_with_two_pje_candidates(self):
        when = "2026-02-03T13:22:11-03:00"
        dj = event("DataJud", "Petição — Outras", when,
                   complements=[{"nome": "Tipo de petição", "valor": "Outras"}])
        rows = [dj, event("PJe/TRF1", "Juntada de manifestação", when),
                event("PJe/TRF1", "Juntada de contestação", when)]
        result = reconcile_movements(rows)
        self.assertEqual(len(result), 3)
        self.assertTrue(all(not row.get("equivalence") for row in result))

    def test_generic_deadline_matches_single_specific_court_record(self):
        when = "2025-11-06T23:59:00-03:00"
        dj = event("DataJud", "Decurso de prazo", when, code="1051",
                   tpu=official_tpu("1051", "Decurso de Prazo", "Decorrido prazo de parte.", []))
        pje = event("PJe/TRF1", "Decorrido prazo de Parte A em 06/11/2025", when)
        result = reconcile_movements([dj, pje])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["titulo"], pje["titulo"])

    def test_remessa_and_publication_families_match_across_descriptions(self):
        when = "2026-02-03T13:22:11-03:00"
        remessa = reconcile_movements([
            event("DataJud", "Remessa em grau de recurso", when),
            event("PJe/TRF1", "Remetidos os autos em grau de recurso para Turma Recursal", when),
        ])
        self.assertEqual(len(remessa), 1)
        publication = reconcile_movements([
            event("DataJud", "Expedição de publicação ao DJEN", when),
            event("PJe/TRF1", "Expedição de publicação ao Diário de Justiça Eletrônico Nacional", when),
        ])
        self.assertEqual(len(publication), 1)

    def test_same_second_different_document_types_remain_separate(self):
        when = "2026-02-03T13:22:11-03:00"
        rows = [event("DataJud", "Juntada de certidão", when, documents=[{"document_id": "a", "type": "intimação"}]),
                event("PJe/TRF1", "Juntada de certidão", when, documents=[{"document_id": "b", "type": "publicação"}])]
        self.assertEqual(len(reconcile_movements(rows)), 2)

    def test_same_second_structured_recipient_conflict_remains_separate(self):
        when = "2026-02-03T13:22:11-03:00"
        rows = [event("DataJud", "Expedição de intimação", when, original={"source_title": "Expedição"}),
                event("PJe/TRF1", "Expedição de intimação", when, original={"source_title": "Expedição"})]
        rows[0]["recipient"] = "Parte A"
        rows[1]["recipient"] = "Parte B"
        self.assertEqual(len(reconcile_movements(rows)), 2)

    def test_source_details_are_coherent_and_preferred_source_has_original_metadata(self):
        when = "2026-02-03T13:22:11-03:00"
        dj = event("DataJud", "Documento — Certidão", when, code="581",
                   complements=[{"nome": "tipo", "valor": "Certidão"}],
                   documents=[{"source": "DataJud", "description": "arquivo de origem", "url": None}],
                   original={"original_url": None, "source_title": "Documento"})
        pje = event("PJe/TRF1", "Juntada de certidão", when,
                    documents=[{"source": "PJe/TRF1", "description": "certidão judicial", "url": None}],
                    original={"source_title": "Juntada de certidão", "original_url": None})
        result = reconcile_movements([dj, pje])[0]
        self.assertEqual(result["preferred_source"], "PJe/TRF1")
        details = {item["source"]: item for item in result["source_details"]}
        self.assertEqual(details["DataJud"]["original"]["source_title"], "Documento")
        self.assertEqual(details["PJe/TRF1"]["documents"][0]["description"], "certidão judicial")
        self.assertIsNone(details["DataJud"]["original_url"])
        self.assertEqual(result["id"], reconcile_movements([dj, pje])[0]["id"])

    def test_api_contract_and_source_switch_controls_are_available_without_duplicate_timeline_row(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "g3-api.sqlite3")
                repo = SQLiteRepository(path)
                try:
                    repo.register("local", NUMBER, "trf1", 3600, 100)
                    raw = {"hits": {"hits": [{"_source": {"id": "g3", "numeroProcesso": NUMBER,
                        "tribunal": "TRF1", "grau": "JE", "sistema": {"nome": "PJe"},
                        "classe": {"nome": "Classe teste"}, "movimentos": [{"id": "dj-publicacao",
                            "codigo": 12282, "nome": "Expedição de publicação ao DJEN",
                            "dataHora": "2026-02-03T16:22:11Z", "complementosTabelados": []}]}}]}}
                    snapshot = normalize_response(raw, NUMBER, "trf1")
                    repo.apply("local", NUMBER, repo.start_run("local", NUMBER), snapshot)
                    repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", {"source": "PJe/TRF1",
                        "movements": [{"source_id": "pje-publication", "occurred_at": "2026-02-03T13:22:11-03:00",
                            "name": "Expedição de publicação ao Diário de Justiça Eletrônico Nacional",
                            "documents": [{"source": "PJe/TRF1", "description": "Sentença Tipo A", "url": None}]}]},
                        status="consulta concluída")
                    app = create_app(repo, Engine(repo, type("Offline", (), {"fetch": lambda *args: {}})()),
                        config=Config(db=path, access_token="g3-api", djen_initial_date=date.today()))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                            headers={"Authorization": "Bearer g3-api"}) as client:
                        response = await client.get(f"/api/processos/{NUMBER}/timeline")
                        page = await client.get("/")
                        script = await client.get("/assets/app.js")
                    self.assertEqual(response.status_code, 200)
                    items = [item for item in response.json()["items"] if item["tipo"] == "movimentacao"]
                    self.assertEqual(len(items), 1)
                    self.assertEqual(items[0]["preferred_source"], "PJe/TRF1")
                    self.assertEqual(items[0]["fontes"], ["DataJud", "PJe/TRF1"])
                    self.assertEqual({item["source"] for item in items[0]["source_details"]}, {"DataJud", "PJe/TRF1"})
                    self.assertIn("/assets/app.js", page.text)
                    self.assertIn("aria-pressed", script.text)
                    self.assertIn("renderSourceVersion(body,event,sourceName)", script.text)
                    self.assertIn("type='button'", script.text)
                finally:
                    repo.close()
        asyncio.run(scenario())

    def test_timeline_api_returns_complete_newest_first_results_and_source_filter_contract(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / "g3-order.sqlite3")
                repo = SQLiteRepository(path)
                try:
                    repo.register("local", NUMBER, "trf1", 3600, 100)
                    raw = {"hits": {"hits": [{"_source": {"id": "g3-order", "numeroProcesso": NUMBER,
                        "tribunal": "TRF1", "grau": "JE", "sistema": {"nome": "PJe"},
                        "classe": {"nome": "Classe teste"}, "movimentos": [
                            {"id": "older", "codigo": 581, "nome": "Documento",
                             "dataHora": "2026-02-02T12:00:00Z", "complementosTabelados": []},
                            {"id": "newer", "codigo": 12282, "nome": "Expedição de publicação ao DJEN",
                             "dataHora": "2026-02-03T12:00:00Z", "complementosTabelados": []}]}}]}}
                    repo.apply("local", NUMBER, repo.start_run("local", NUMBER), normalize_response(raw, NUMBER, "trf1"))
                    repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", {"source": "PJe/TRF1",
                        "movements": [{"source_id": "pje-newer", "occurred_at": "2026-02-03T12:00:00+00:00",
                            "name": "Expedição de publicação ao DJEN", "documents": []}]},
                        status="consulta concluída")
                    app = create_app(repo, Engine(repo, type("Offline", (), {"fetch": lambda *args: {}})()),
                        config=Config(db=path, access_token="g3-order", djen_initial_date=date.today()))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                            headers={"Authorization": "Bearer g3-order"}) as client:
                        response = await client.get(f"/api/processos/{NUMBER}/timeline")
                        page = await client.get("/")
                        script = await client.get("/assets/app.js")
                    items = [item for item in response.json()["items"] if item["tipo"] == "movimentacao"]
                    self.assertEqual(len(items), 2)  # API returns the complete current timeline (no page cap).
                    self.assertGreater(items[0]["occurred_at"], items[1]["occurred_at"])
                    self.assertEqual(items[0]["fontes"], ["DataJud", "PJe/TRF1"])
                    self.assertIn("/assets/app.js", page.text)
                    self.assertIn("sourcesOf(e).includes(source)", script.text)
                    self.assertIn("movement-source", script.text)
                finally:
                    repo.close()
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

