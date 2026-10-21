import json
import tempfile
import unittest
from pathlib import Path

from processwatch.consolidation import derive_situation, process_metadata
from processwatch.participants import canonical_participants
from processwatch.providers.djen import parse_communication
from processwatch.providers.pje_trf1 import _clean_participant_text
from processwatch.repository import SQLiteRepository


NUMBER = "10068509020254013305"


class StructuralParticipantTests(unittest.TestCase):
    def test_pje_qualifications_are_structured_and_not_attached_to_names(self):
        for source_text, expected_kind, expected_function in (
            ("Pessoa Exemplo (AUTOR)", "parte", "autor"),
            ("Pessoa Exemplo (RÉU)", "parte", "réu"),
            ("Pessoa Exemplo (REPRESENTANTE)", "representante", "representante legal"),
            ("Pessoa Exemplo (PERITO)", "perito", "perito"),
            ("Pessoa Exemplo (RELATORA)", "ator_judicial", "relator"),
            ("Pessoa Exemplo (AGRAVANTE)", "parte", "agravante"),
        ):
            with self.subTest(source_text=source_text):
                item = _clean_participant_text(source_text, "ativo")
                self.assertEqual(item["name"], "Pessoa Exemplo")
                self.assertEqual(item["source_text_original"], source_text)
                self.assertEqual(item["kind"], expected_kind)
                self.assertEqual(item["procedural_function"], expected_function)
                self.assertEqual(item["role_original"], source_text[source_text.rfind("(") + 1:-1])
                if expected_kind != "parte":
                    self.assertIsNone(item["polo"])

    def test_old_persisted_person_name_documents_are_redacted_in_canonical_api_view(self):
        class Repo:
            def participants(self, org, number):
                return [{"original_name": "Pessoa Exemplo - CPF: 123.456.789-00 (AUTOR)",
                         "role": "AUTOR", "kind": "parte", "source": "PJe/TRF1",
                         "evidence_id": "snapshot-1"}]
        people = canonical_participants(Repo(), "office", NUMBER)
        self.assertEqual(people[0]["original_name"], "Pessoa Exemplo")
        self.assertNotIn("123.456.789-00", json.dumps(people))
        self.assertEqual(people[0]["procedural_function"], "autor")

    def test_historical_non_object_evidence_is_preserved_without_breaking_consolidation(self):
        class Repo:
            def participants(self, org, number):
                return [{"original_name": "Pessoa Exemplo", "kind": "parte", "source": "PJe/TRF1",
                         "role": "AUTOR", "evidence": ["rótulo antigo", "AUTOR"], "evidence_id": "snapshot-2"}]
        people = canonical_participants(Repo(), "office", NUMBER)
        self.assertEqual(people[0]["evidence"]["source_value"], ["rótulo antigo", "AUTOR"])
        self.assertEqual(people[0]["procedural_function"], "autor")

    def test_same_name_across_sources_is_a_candidate_not_a_merged_identity(self):
        class Repo:
            def participants(self, org, number):
                return [{"original_name": "Maria Exemplo", "kind": "destinatário",
                         "source": "DJEN_PUBLICACAO", "evidence_id": "pub-1"}]
        result = canonical_participants(Repo(), "office", NUMBER, court_snapshots=[{
            "source": "PJe/TRF1", "source_id": "pje-1", "data": {"participants": [
                {"name": "Maria Exemplo", "kind": "parte", "polo": "ativo", "role": "AUTOR"}]}}])
        self.assertEqual(len(result), 2)
        self.assertTrue(all(item["identity_status"] == "possible_match_requires_confirmation" for item in result))
        self.assertTrue(all(item["identity_candidates"] for item in result))

    def test_oab_and_exact_name_reconcile_across_sources_even_if_one_has_source_id(self):
        class Repo:
            def participants(self, org, number):
                return [{"original_name": "Advogada Exemplo", "normalized_name": "advogada exemplo",
                         "kind": "advogado", "source": "DJEN_PUBLICACAO", "oab": "12345", "uf": "SP",
                         "source_identity_id": "adv-9", "evidence_id": "pub-1"}]
        result = canonical_participants(Repo(), "office", NUMBER, court_snapshots=[{
            "source": "PJe/TRF1", "source_id": "snapshot-1", "data": {"participants": [{
                "name": "Advogada Exemplo", "kind": "advogado", "source": "PJe/TRF1",
                "oab": "12345", "uf": "SP", "evidence_id": "snapshot-1"}]}}])
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]["sources"]), {"DJEN_PUBLICACAO", "PJe/TRF1"})
        self.assertEqual(result[0]["identity_status"], "confirmed_oab_uf")

    def test_same_oab_but_divergent_names_are_not_merged_and_are_flagged(self):
        class Repo:
            def participants(self, org, number):
                return []
        result = canonical_participants(Repo(), "office", NUMBER, court_snapshots=[
            {"source": "PJe/TRF1", "data": {"participants": [{"name": "Nome A", "kind": "advogado",
                "oab": "12345", "uf": "SP"}]}},
            {"source": "TRF4", "data": {"participants": [{"name": "Nome B", "kind": "advogado",
                "oab": "12345", "uf": "SP"}]}}
        ])
        self.assertEqual(len(result), 2)
        self.assertTrue(all(item["identity_status"] == "identifier_name_conflict" for item in result))

    def test_stable_source_identity_deduplicates_repeated_snapshot_mentions(self):
        class Repo:
            def participants(self, org, number):
                return []
        result = canonical_participants(Repo(), "office", NUMBER, court_snapshots=[
            {"source": "PJe/TRF1", "data": {"participants": [{"name": "Advogada Exemplo",
                "kind": "advogado", "source_identity_id": "pessoa-9", "role": "ADVOGADA",
                "evidence_id": "snap-1"}]}},
            {"source": "PJe/TRF1", "data": {"participants": [{"name": "Advogada Exemplo",
                "kind": "advogado", "source_identity_id": "pessoa-9", "role": "ADVOGADA",
                "evidence_id": "snap-2"}]}}
        ])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["identity_status"], "confirmed_source_id")
        self.assertEqual(set(result[0]["evidence_ids"]), {"snap-1", "snap-2"})

    def test_same_source_identifier_with_different_names_is_conflict_not_merge(self):
        class Repo:
            def participants(self, org, number):
                return []
        result = canonical_participants(Repo(), "office", NUMBER, court_snapshots=[
            {"source": "PJe/TRF1", "data": {"participants": [{"name": "Nome A",
                "kind": "parte", "source_identity_id": "pessoa-9"}]}},
            {"source": "PJe/TRF1", "data": {"participants": [{"name": "Nome B",
                "kind": "parte", "source_identity_id": "pessoa-9"}]}}
        ])
        self.assertEqual(len(result), 2)
        self.assertTrue(all(item["identity_status"] == "source_identifier_name_conflict" for item in result))

    def test_fifteen_same_process_recipients_deduplicate_and_keep_all_communications(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            db_path = str(Path(folder) / "identity.sqlite3")
            repo = SQLiteRepository(db_path)
            try:
                repo.register("local", NUMBER, "trf1", 3600, 100)
                items = []
                for index in range(15):
                    source_id = f"pub-{index + 1}"
                    raw = {"id": source_id, "numero_processo": NUMBER,
                           "destinatarios": [{"nome": "Pessoa Exemplo", "polo": "A"}],
                           "destinatarioadvogados": [{"id": source_id + "-relation", "advogado_id": 888,
                               "advogado": {"id": 888, "nome": "Advogado Exemplo", "numero_oab": "12345", "uf_oab": "SP"}}]}
                    items.append(parse_communication(raw, NUMBER).model_dump())
                repo.save_communications("local", NUMBER, items, status="período consultado")
                people = repo.participants("local", NUMBER)
                self.assertEqual(len([p for p in people if p["kind"] == "advogado"]), 1)
                self.assertEqual(len([p for p in people if p["kind"] == "destinatário"]), 1)
                lawyer = next(p for p in people if p["kind"] == "advogado")
                self.assertEqual(lawyer["identity_status"], "confirmed_source_id")
                self.assertEqual(lawyer["mention_count"], 15)
                recipient = next(p for p in people if p["kind"] == "destinatário")
                self.assertEqual(recipient["identity_status"], "probable_case_name_kind")
                self.assertEqual(recipient["mention_count"], 15)
                self.assertEqual(len(recipient["evidence_ids"]), 15)
                self.assertEqual(len(repo.communications("local", NUMBER)), 15)
                canonical = canonical_participants(repo, "local", NUMBER)
                self.assertEqual(len(canonical), 2)
                self.assertEqual(sum(person["mention_count"] for person in canonical), 30)

            finally:
                repo.close()

    def test_same_name_with_distinct_stable_source_ids_remains_separate(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            repo = SQLiteRepository(str(Path(folder) / "identity.sqlite3"))
            try:
                repo.register("office", NUMBER, "trf1", 3600, 100)
                rows = []
                for source_id, identity_id in (("pub-1", "person-a"), ("pub-2", "person-b")):
                    rows.append(parse_communication({
                        "id": source_id, "numero_processo": NUMBER,
                        "destinatarios": [{"idParte": identity_id, "nome": "Pessoa Homônima"}],
                    }, NUMBER).model_dump())
                repo.save_communications("office", NUMBER, rows, status="período consultado")
                people = repo.participants("office", NUMBER)
                self.assertEqual(len([p for p in people if p["kind"] == "destinatário"]), 2)
            finally:
                repo.close()


class SituationConsolidationTests(unittest.TestCase):
    def test_archive_and_baixa_remain_distinct(self):
        archived = derive_situation([{"code": "246", "when_at": "2025-01-01T00:00:00+00:00", "scope": "1G"}])
        baixa = derive_situation([{"code": "22", "when_at": "2025-01-01T00:00:00+00:00", "scope": "2G"}])
        self.assertEqual(archived["situation"], "Arquivamento definitivo")
        self.assertEqual(baixa["situation"], "Baixa definitiva")
        self.assertNotEqual(archived["situation"], baixa["situation"])

    def test_reactivation_after_archive_resumes_but_unarchive_does_not(self):
        archive = {"code": "246", "when_at": "2024-01-01T00:00:00+00:00", "scope": "1G"}
        reactivate = {"code": "849", "when_at": "2024-02-01T00:00:00+00:00", "scope": "1G"}
        unarchive = {"code": "893", "when_at": "2024-02-01T00:00:00+00:00", "scope": "1G"}
        self.assertEqual(derive_situation([archive, reactivate])["situation"], "Reativado")
        self.assertIn("retomada", derive_situation([archive, unarchive])["situation"])

    def test_unknown_suspension_code_is_not_guessed_and_scopes_can_conflict(self):
        self.assertEqual(derive_situation([{"code": "99999", "when_at": "2025-01-01"}])["status"], "unknown")
        rows = [{"code": "246", "when_at": "2025-01-01", "scope": "1G"},
                {"code": "22", "when_at": "2025-01-01", "scope": "2G"}]
        self.assertEqual(derive_situation(rows)["status"], "conflict")


class ContextualAttributeConsolidationTests(unittest.TestCase):
    def test_same_class_and_subject_codes_across_datajud_and_pje_are_not_conflicts(self):
        watch = {"number": NUMBER, "court": "TRF1", "metadata": json.dumps([
            {"_scope": "opaque-je-id", "grau": "JE",
             "classe": {"codigo": 436, "nome": "Procedimento do Juizado Especial Cível"},
             "assuntos": [{"codigo": 11946, "nome": "Deficiente"}],
             "dataHoraUltimaAtualizacao": "2026-10-07T13:49:39Z"},
            {"_scope": "opaque-tr-id", "grau": "TR",
             "classe": {"codigo": 460, "nome": "Recurso Inominado Cível"},
             "assuntos": [{"codigo": 11946, "nome": "Deficiente"}],
             "dataHoraUltimaAtualizacao": "2026-10-07T14:06:41Z"},
        ])}
        result = process_metadata(watch, [], source_data={
            "degree": "JE", "class": "PROCEDIMENTO DO JUIZADO ESPECIAL CÍVEL (436)",
            "subjects": "DIREITO ASSISTENCIAL (12734) - Benefício Assistencial (6114) - Pessoa com Deficiência (11946)",
            "collected_at": "2026-10-08T10:00:00Z",
        }, source_label="PJe/TRF1")
        self.assertEqual(result["classe"], "PROCEDIMENTO DO JUIZADO ESPECIAL CÍVEL (436)")
        self.assertEqual(result["proveniencia_atributos"]["classe"]["status"], "confirmed_source_value")
        self.assertFalse(result["proveniencia_atributos"]["classe"]["conflict"])
        self.assertEqual(len(result["proveniencia_atributos"]["classe"]["evidence"]), 3)
        self.assertFalse(result["proveniencia_atributos"]["assuntos"]["conflict"])
        self.assertNotIn("classe", result["conflitos_atributos"])
        self.assertNotIn("assuntos", result["conflitos_atributos"])

    def test_truncated_pje_subject_leaf_code_matches_datajud_code(self):
        watch = {"number": NUMBER, "court": "TRF1", "metadata": json.dumps([{
            "grau": "JE", "assuntos": [{"codigo": 11946, "nome": "Deficiente"}],
            "dataHoraUltimaAtualizacao": "2026-10-07T13:49:39Z",
        }])}
        result = process_metadata(watch, [], source_data={
            "degree": "JE",
            "subjects": ["DIREITO ASSISTENCIAL (12734) - Benefício (6114) - Pessoa com Deficiência (11946"],
        }, source_label="PJe/TRF1")
        provenance = result["proveniencia_atributos"]["assuntos"]
        self.assertFalse(provenance["conflict"])
        self.assertEqual(provenance["rule"], "same_scope_values_agree")
        self.assertEqual(result["assuntos"],
                         ["DIREITO ASSISTENCIAL (12734) - Benefício (6114) - Pessoa com Deficiência (11946"])

    def test_same_distribution_date_formats_agree_and_empty_organ_is_not_a_conflict(self):
        watch = {"number": NUMBER, "court": "TRF1", "metadata": json.dumps([{
            "grau": "JE", "dataAjuizamento": "20250205102815",
            "orgaoJulgador": {"codigo": 12367, "nome": "Vara Única de Juazeiro"},
            "dataHoraUltimaAtualizacao": "2026-10-07T13:49:39Z",
        }])}
        result = process_metadata(watch, [], source_data={
            "degree": "JE", "distribution_date": "2025-02-05",
            "court_organ": {"address": None, "name": None},
        }, source_label="PJe/TRF1")
        self.assertFalse(result["proveniencia_atributos"]["distribuicao"]["conflict"])
        self.assertEqual(result["distribuicao"], "2025-02-05")
        self.assertFalse(result["proveniencia_atributos"]["orgao"]["conflict"])
        self.assertEqual(result["orgao"].get("nome"), "Vara Única de Juazeiro")

    def test_real_class_conflict_within_same_grade_keeps_both_values(self):
        watch = {"number": NUMBER, "court": "TRF1", "metadata": json.dumps([
            {"grau": "JE", "classe": {"codigo": 436, "nome": "Procedimento do Juizado Especial Cível"},
             "dataHoraUltimaAtualizacao": "2026-10-07T13:49:39Z"},
        ])}
        result = process_metadata(watch, [], source_data={
            "degree": "JE", "class": "Ação de Procedimento Especial (999)",
            "collected_at": "2026-10-08T10:00:00Z",
        }, source_label="PJe/TRF1")
        # The newest explicit value remains useful to callers, while the API
        # retains both candidates and marks the attribute as conflicted.
        self.assertEqual(result["classe"], "Ação de Procedimento Especial (999)")
        self.assertEqual(result["proveniencia_atributos"]["classe"]["status"], "conflict")
        self.assertEqual(len(result["conflitos_atributos"]["classe"]), 2)
        self.assertEqual({item["value"] for item in result["conflitos_atributos"]["classe"]},
                         {"Procedimento do Juizado Especial Cível", "Ação de Procedimento Especial (999)"})

    def test_different_subject_leaf_codes_in_same_grade_remain_a_conflict(self):
        watch = {"number": NUMBER, "court": "TRF1", "metadata": json.dumps([
            {"grau": "JE", "assuntos": [{"codigo": 11946, "nome": "Deficiente"}],
             "dataHoraUltimaAtualizacao": "2026-10-07T13:49:39Z"},
        ])}
        result = process_metadata(watch, [], source_data={
            "degree": "JE", "subjects": "DIREITO ASSISTENCIAL (12734) - Benefício (6114) - Pessoa (11947)",
            "collected_at": "2026-10-08T10:00:00Z",
        }, source_label="PJe/TRF1")
        self.assertTrue(result["proveniencia_atributos"]["assuntos"]["conflict"])
        self.assertEqual(len(result["conflitos_atributos"]["assuntos"]), 2)

    def test_common_state_across_scopes_can_be_reported_without_claiming_closed_case(self):
        watch = {"number": NUMBER, "court": "TRF1", "metadata": "[]"}
        rows = [{"code": "246", "when_at": "2025-01-01T00:00:00+00:00", "scope": "1G"},
                {"code": "246", "when_at": "2025-02-01T00:00:00+00:00", "scope": "2G"}]
        result = process_metadata(watch, rows)
        self.assertEqual(result["situacao_estado"], "arquivado")
        self.assertEqual(result["situacao"], "Arquivamento definitivo")

    def test_direct_source_label_and_collection_time_are_preserved_for_trf4(self):
        watch = {"number": NUMBER, "court": "TRF4", "metadata": "[]"}
        result = process_metadata(watch, [], source_data={"class": "Agravo de Instrumento"},
                                  source_label="eproc/TRF4", source_collected_at="2026-10-01T12:00:00Z")
        evidence = result["proveniencia_atributos"]["classe"]["evidence"]
        self.assertEqual(evidence[0]["source"], "eproc/TRF4")
        self.assertEqual(evidence[0]["reference_at"], "2026-10-01T12:00:00Z")

    def test_explicit_situation_and_derived_state_conflict_are_both_preserved(self):
        watch = {"number": NUMBER, "court": "TRF1", "metadata": json.dumps([{
            "situacao": "Em tramitação", "dataHoraUltimaAtualizacao": "2025-01-01"}])}
        result = process_metadata(watch, [{"code": "246", "when_at": "2025-02-01", "scope": "1G"}])
        self.assertEqual(result["situacao"], "Arquivamento definitivo")
        self.assertEqual(result["situacao_explicita"], "Em tramitação")
        self.assertEqual(result["situacao_status"], "conflict")
        self.assertIn("situacao", result["conflitos_atributos"])

    def test_reactivated_event_agrees_with_explicit_in_tramitation_status(self):
        watch = {"number": NUMBER, "court": "TRF1", "metadata": json.dumps([{
            "situacao": "Em tramitação", "dataHoraUltimaAtualizacao": "2025-02-01T00:00:00Z"}])}
        result = process_metadata(watch, [{"code": "849", "when_at": "2025-01-01", "scope": "1G"}])
        self.assertEqual(result["situacao_status"], "explicit")
        self.assertEqual(result["situacao"], "Em tramitação")

    def test_attribute_conflict_exposes_both_source_evidence_and_rule(self):
        watch = {"number": NUMBER, "court": "TRF1", "metadata": json.dumps([{
            "classe": {"nome": "Classe DataJud"}, "dataHoraUltimaAtualizacao": "2025-01-01"}])}
        result = process_metadata(watch, [], source_data={"class": "Classe PJe"},
                                  source_label="PJe/TRF1")
        self.assertIsNone(result["classe"])
        self.assertTrue(result["proveniencia_atributos"]["classe"]["conflict"])
        self.assertEqual(len(result["proveniencia_atributos"]["classe"]["evidence"]), 2)
        self.assertEqual(result["proveniencia_atributos"]["classe"]["rule"],
                         "unresolved_conflict_without_unique_latest_evidence")

    def test_unique_newest_timestamp_selects_value_but_keeps_conflict(self):
        watch = {"number": NUMBER, "court": "TRF1", "metadata": json.dumps([{
            "classe": {"nome": "Classe DataJud"}, "dataHoraUltimaAtualizacao": "2025-01-01T00:00:00Z"}])}
        result = process_metadata(watch, [], source_data={"class": "Classe PJe"},
                                  source_label="PJe/TRF1", source_collected_at="2025-02-01T00:00:00Z")
        provenance = result["proveniencia_atributos"]["classe"]
        self.assertEqual(result["classe"], "Classe PJe")
        self.assertTrue(provenance["conflict"])
        self.assertEqual(provenance["rule"], "latest_comparable_reference_with_conflict_retained")

    def test_conflicting_secrecy_flags_are_not_flattened_to_yes_or_no(self):
        watch = {"number": NUMBER, "court": "TRF1", "metadata": json.dumps([{
            "segredoJustica": True, "dataHoraUltimaAtualizacao": "2025-01-01T00:00:00Z"}])}
        result = process_metadata(watch, [], source_data={"judicial_secrecy": False},
                                  source_label="PJe/TRF1")
        self.assertEqual(result["segredo_justica"], "Divergência entre fontes")
        self.assertTrue(result["proveniencia_atributos"]["segredo_justica"]["conflict"])


if __name__ == "__main__":
    unittest.main()
