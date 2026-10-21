import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from processwatch.api import create_app
from processwatch.config import Config
from processwatch.consolidation import process_metadata
from processwatch.engine import Engine
from processwatch.event_reconciliation import reconcile_movements
from processwatch.participants import canonical_participants, presentation_participants, normalized_name
from processwatch.process_phases import consolidate_phase, PHASES
from processwatch.providers.pje_trf1 import _participant_identity
from processwatch.repository import SQLiteRepository

NUMBER = "10008143220254013305"


def person(name, kind="parte", source="PJe/TRF1", **extra):
    return dict(original_name=name, normalized_name=normalized_name(name), kind=kind,
                kinds=[kind], source=source, sources=[source], **extra)


class PresentationIdentityTests(unittest.TestCase):
    def test_initials_full_name_and_caption_suffix_make_one_author(self):
        rows = [person("I. V. V. D. S.", procedural_function="autor", procedural_pole="ativo"),
                person("I. V. V. D. S. Advogados do(a)", source="DJEN_PUBLICACAO", procedural_function="autor"),
                person("IAN VALENTIM VASCONCELOS DE SA", "destinatário", "DJEN_PUBLICACAO"),
                person("LIA EXEMPLO", "advogado", "DJEN_PUBLICACAO", oab="12345", uf="BA")]
        result = presentation_participants(rows)
        parties = [p for p in result if "parte" in p["kinds"]]
        self.assertEqual(len(parties), 1)
        self.assertEqual(parties[0]["original_name"], "IAN VALENTIM VASCONCELOS DE SA")
        self.assertEqual(parties[0]["procedural_function"], "autor")
        self.assertEqual(parties[0]["procedural_pole"], "ativo")
        self.assertEqual(set(parties[0]["sources"]), {"PJe/TRF1", "DJEN_PUBLICACAO"})
        self.assertEqual(len(parties[0]["presentation_members"]), 3)
        self.assertNotIn("destinatário", parties[0]["kinds"])

    def test_exact_names_and_extensions_share_presentation_without_losing_evidence(self):
        result = presentation_participants([person("João da Silva", evidence_ids=["a"]),
            person("JOAO DA SILVA", source="DJEN_PUBLICACAO", evidence_ids=["b"]),
            person("João da Silva Santos", "destinatário", "DJEN_PUBLICACAO", evidence_ids=["c"])])
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]["evidence_ids"]), {"a", "b", "c"})
        self.assertEqual(result[0]["presentation_identity_status"], "possible_correspondence")

    def test_proven_distinct_documents_stay_separate(self):
        self.assertEqual(len(presentation_participants([
            person("ANA EXEMPLO", cpf="11111111111"), person("ANA EXEMPLO", cpf="22222222222") ])), 2)

    def test_oab_or_professional_kinds_or_firm_never_remain_parties(self):
        for extra in ({"oab":"12345", "uf":"SP"}, {"kinds":["parte", "procurador"]},
                      {"kinds":["parte", "procurador_federal"]}, {}):
            name = "EXEMPLO ADVOCACIA" if not extra else "ANA EXEMPLO"
            row = person(name)
            row.update(extra)
            result = presentation_participants([row])[0]
            self.assertNotIn("parte", result["kinds"])
            self.assertIn(result["kind"], {"advogado", "procurador"})

    def test_pje_oab_without_role_is_professional(self):
        for text in ("ANA EXEMPLO OAB/SP 12345", "ANA EXEMPLO (AUTOR) OAB/SP 12345"):
            row = _participant_identity(text, "ativo")
            self.assertEqual((row["kind"], row["oab"], row["uf"]), ("advogado", "12345", "SP"))
            self.assertIsNone(row["polo"])

    def test_titles_are_not_part_of_judicial_actor_name(self):
        class Repo:
            def participants(self, *_): return [person("Federal André Exemplo", "ator_judicial", role="magistrado")]
        result = canonical_participants(Repo(), "local", NUMBER)
        self.assertEqual(result[0]["original_name"], "André Exemplo")
        self.assertNotIn("parte", result[0]["kinds"])


class PhaseCatalogTests(unittest.TestCase):
    def test_appeal_without_status_code_has_a_catalog_phase_and_evidence(self):
        rows = [{"name":"Remetidos os Autos em grau de recurso à Turma Recursal", "when_at":"2026-01-01"},
                {"name":"Conclusos para julgamento de mérito", "when_at":"2026-02-01"}]
        phase = consolidate_phase(rows)
        self.assertEqual(phase["code"], "aguardando_julgamento_do_recurso")
        self.assertEqual(phase["evidence"][0]["occurred_at"], "2026-02-01")
        self.assertIn(phase["code"], PHASES)

    def test_chronology_and_distinct_terminal_phases(self):
        for code, expected in (("22","baixado"),("246","arquivado"),("849","reativado"),("893","desarquivado")):
            self.assertEqual(consolidate_phase([{"code":code,"when_at":"2026-01-01"}])["code"], expected)
        rows = [{"name":"Acórdão publicado", "when_at":"2026-02-01"},
                {"name":"Interposto recurso", "when_at":"2026-01-01"}]
        self.assertEqual(consolidate_phase(rows)["code"], "recurso_julgado")

    def test_hypothetical_publication_orders_do_not_establish_phase(self):
        phase = consolidate_phase([], communications=[{"text_plain":"Após o trânsito em julgado, arquive-se.",
                                                       "published_at":"2026-01-01"}])
        self.assertEqual(phase["code"], "desconhecido")

    def test_unknown_secrecy_is_omitted_but_explicit_false_is_kept(self):
        watch = {"number":NUMBER, "court":"trf1", "metadata":"[]"}
        self.assertNotIn("segredo_justica", process_metadata(watch, []))
        self.assertEqual(process_metadata(watch, [], source_data={"judicial_secrecy":False})["segredo_justica"], "Não")


class ApiPipelineTests(unittest.TestCase):
    def test_persisted_cover_cpf_and_professional_reach_separate_api_sections(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder)/"pipeline.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 24, 100)
                party = _participant_identity("ANA EXEMPLO (AUTOR) CPF: 123.456.789-01", "ativo")
                lawyer = _participant_identity("LIA EXEMPLO OAB/BA 12345", "ativo")
                repo.save_court_snapshot("local", NUMBER, "PJe/TRF1", {"participants":[party,lawyer],
                    "movements":[],"documents":[]}, status="consulta concluída")
                saved = repo.latest_court_snapshot("local", NUMBER, "PJe/TRF1")
                self.assertEqual(saved["data"]["participants"][0]["cpf"], "12345678901")
                app = create_app(repo, Engine(repo, object()), config=Config(access_token="test-only"))
                async def check():
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                        headers={"Authorization":"Bearer test-only"}) as client:
                        response = await client.get(f"/api/processos/{NUMBER}/partes")
                        self.assertEqual(response.status_code, 200)
                        payload = response.json()
                        self.assertEqual([p["nome"] for p in payload["partes"]], ["ANA EXEMPLO"])
                        self.assertEqual(payload["partes"][0]["cpf"], "12345678901")
                        self.assertEqual([p["original_name"] for p in payload["advogados"]], ["LIA EXEMPLO"])
                asyncio.run(check())
            finally:
                repo.close()

    def test_near_timestamp_identical_event_merges_but_distinct_act_does_not(self):
        def event(identity, source, timestamp, title):
            return {"id":identity,"number":NUMBER,"tipo":"movimentacao","fonte":source,
                    "titulo":title,"occurred_at":timestamp}
        rows = [event("a","DataJud","2026-01-01T12:00:00Z","Juntada de petição inicial"),
                event("b","PJe/TRF1","2026-01-01T12:00:01Z","Juntada de petição inicial")]
        merged = reconcile_movements(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0]["fontes"]), {"DataJud","PJe/TRF1"})
        rows[1]["titulo"] = "Juntada de contestação"
        self.assertEqual(len(reconcile_movements(rows)), 2)

    def test_observed_grammatical_variants_keep_both_source_records(self):
        for left, right in (("Ato ordinatório", "Ato ordinatório praticado"),
                            ("Inclusão no Juízo 100% Digital", "Autos incluídos no Juízo 100% Digital"),
                            ("Disponibilização no Diário da Justiça Eletrônico", "Disponibilizado no DJ Eletrônico em 26/05/2025")):
            rows = [{"id":"a", "number":NUMBER, "tipo":"movimentacao", "fonte":"DataJud",
                     "titulo":left, "occurred_at":"2025-05-26T12:00:00Z"},
                    {"id":"b", "number":NUMBER, "tipo":"movimentacao", "fonte":"PJe/TRF1",
                     "titulo":right, "occurred_at":"2025-05-26T12:00:00Z"}]
            result = reconcile_movements(rows)
            self.assertEqual(len(result), 1, (left, right))
            self.assertEqual(len(result[0]["source_details"]), 2)

    def test_reviewed_relation_is_not_lost_by_time_window_index(self):
        rows = [{"id":"a", "number":NUMBER, "tipo":"movimentacao", "fonte":"DataJud",
                 "titulo":"Evento revisado", "occurred_at":"2025-01-01T12:00:00Z"},
                {"id":"b", "number":NUMBER, "tipo":"movimentacao", "fonte":"PJe/TRF1",
                 "titulo":"Evento revisado", "occurred_at":"2025-01-02T12:00:00Z"}]
        self.assertEqual(len(reconcile_movements(rows)), 2)
        self.assertEqual(len(reconcile_movements(rows, confirmed_pairs=[("a", "b")])), 1)
