import tempfile
import unittest
from pathlib import Path

from processwatch.participants import canonical_participants, normalize_pole
from processwatch.providers.djen import parse_communication
from processwatch.repository import SQLiteRepository


NUMBER = "10068509020254013305"


class EmptyParticipantRepository:
    def participants(self, org, number):
        return []


class DjenPoleNormalizationTests(unittest.TestCase):
    def test_djen_a_and_p_are_exposed_as_unconfirmed_legacy_mni_compatibility(self):
        for code in ("A", "P"):
            result = normalize_pole("DJEN_PUBLICACAO", "destinatarios[].polo", code)
            self.assertEqual(result["polo_code_original"], code)
            self.assertIsNone(result["polo_normalized"])
            self.assertEqual(result["polo_mapping_status"],
                             "mni_2_0_compatibility_unconfirmed_for_djen")
            self.assertIn("intercomunicacao-2_0.pdf", result["polo_mapping_reference"])

    def test_b_unknown_and_missing_codes_are_never_guessed(self):
        unknown = normalize_pole("DJEN_PUBLICACAO", "destinatarios[].polo", "B")
        self.assertEqual(unknown["polo_code_original"], "B")
        self.assertIsNone(unknown["polo_normalized"])
        self.assertEqual(unknown["polo_mapping_status"], "unmapped_code")
        missing = normalize_pole("DJEN_PUBLICACAO", "destinatarios[].polo", None)
        self.assertIsNone(missing["polo_code_original"])
        self.assertIsNone(missing["polo_normalized"])

    def test_raw_parser_preserves_recipient_pole_and_distinct_communication_role(self):
        raw = {"id": 51, "numero_processo": NUMBER,
               "destinatarios": [{"nome": "Pessoa Anonimizada", "polo": "A"}],
               "destinatarioadvogados": [{"advogado": {"nome": "Advogado Anonimizado",
                    "numero_oab": "12345", "uf_oab": "SP"}}]}
        communication = parse_communication(raw, NUMBER)
        recipient = next(x for x in communication.participants if x.kind == "destinatário")
        lawyer = next(x for x in communication.participants if x.kind == "advogado")
        self.assertEqual(recipient.polo_code_original, "A")
        self.assertEqual(recipient.polo_source_field, "destinatarios[].polo")
        self.assertEqual(recipient.communication_role, "destinatário da comunicação")
        self.assertIsNone(recipient.polo_normalized)
        self.assertEqual(lawyer.communication_role, "advogado destinatário")
        self.assertEqual(lawyer.oab, "12345")
        self.assertEqual(communication.original, raw)

    def test_canonical_view_maps_legacy_records_without_destroying_original_code(self):
        repo = EmptyParticipantRepository()
        result = canonical_participants(repo, "office", NUMBER, court_snapshots=[{
            "source": "DJEN_PUBLICACAO", "source_id": "publication-1", "data": {
                "participants": [{"original_name": "Pessoa Anonimizada", "kind": "destinatário",
                    "polo": "P", "source": "DJEN_PUBLICACAO", "evidence_id": "publication-1"}]}}])
        person = result[0]
        self.assertEqual(person["polo"], "P")
        self.assertEqual(person["polo_code_original"], "P")
        self.assertIsNone(person["polo_normalized"])
        self.assertEqual(person["communication_roles"], ["destinatário da comunicação"])
        self.assertEqual(person["polo_evidence"][0]["field"], "destinatarios[].polo")

    def test_unknown_code_stays_visible_and_same_name_with_different_poles_is_not_merged(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        path = str(Path(temp.name) / "participants.sqlite3")
        repo = SQLiteRepository(path)
        try:
            repo.register("office", NUMBER, "trf1", 86400, 100)
            for source_id, code in (("pub-a", "A"), ("pub-b", "B")):
                raw = {"id": source_id, "numero_processo": NUMBER,
                       "destinatarios": [{"nome": "Pessoa Homônima", "polo": code}]}
                item = parse_communication(raw, NUMBER)
                repo.save_communications("office", NUMBER, [item.model_dump()], status="consulta concluída")
            records = canonical_participants(repo, "office", NUMBER)
            same_names = [x for x in records if x["original_name"] == "Pessoa Homônima"]
            self.assertEqual(len(same_names), 2)
            by_code = {x["polo_code_original"]: x for x in same_names}
            self.assertIsNone(by_code["A"]["polo_normalized"])
            self.assertIsNone(by_code["B"]["polo_normalized"])
            self.assertEqual(by_code["B"]["polo_mapping_status"], "unmapped_code")
        finally:
            repo.close()
            temp.cleanup()

    def test_cross_source_pole_conflict_is_explicit_and_keeps_each_evidence(self):
        repo = EmptyParticipantRepository()
        people = canonical_participants(repo, "office", NUMBER, court_snapshots=[
            {"source": "DJEN_PUBLICACAO", "data": {"participants": [{
                "original_name": "Advogado Anonimizado", "kind": "advogado", "oab": "12345", "uf": "SP",
                "polo": "B", "source": "DJEN_PUBLICACAO", "evidence_id": "djen-1"}]}},
            {"source": "PJe/TRF1", "data": {"participants": [{
                "original_name": "Advogado Anonimizado", "kind": "advogado", "oab": "12345", "uf": "SP",
                "polo": "ativo", "source": "PJe/TRF1", "evidence_id": "pje-1"}]}}
        ])
        person = next(x for x in people if x["oab"] == "12345")
        self.assertFalse(person["polo_conflict"])
        self.assertIsNone(person["polo_normalized"])
        self.assertNotIn("parte", person["kinds"])
        self.assertEqual(set(person["polo_codes"]), {"B", "ativo"})
        self.assertEqual({x["source"] for x in person["polo_evidence"]}, {"DJEN_PUBLICACAO", "PJe/TRF1"})
        self.assertIn("djen-1", person["evidence_ids"])
        self.assertIn("pje-1", person["evidence_ids"])

    def test_reprocessing_keeps_raw_payload_and_is_idempotent(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        path = str(Path(temp.name) / "reprocess.sqlite3")
        repo = SQLiteRepository(path)
        raw = {"id": "publication-1", "numero_processo": NUMBER,
               "destinatarios": [{"nome": "Pessoa Anonimizada", "polo": "P"}]}
        try:
            repo.register("office", NUMBER, "trf1", 86400, 100)
            item = parse_communication(raw, NUMBER)
            repo.save_communications("office", NUMBER, [item.model_dump()], status="consulta concluída")
            before = repo.db.execute("SELECT payload FROM raw_records WHERE source='DJEN'").fetchone()[0]
            self.assertEqual(repo.reprocess_djen("office", NUMBER), 1)
            self.assertEqual(repo.reprocess_djen("office", NUMBER), 1)
            after = repo.db.execute("SELECT payload FROM raw_records WHERE source='DJEN'").fetchone()[0]
            self.assertEqual(before, after)
            self.assertEqual(repo.db.execute("SELECT count(*) FROM communications").fetchone()[0], 1)
            stored = repo.communications("office", NUMBER)[0]
            recipient = next(x for x in stored["participants"] if x["kind"] == "destinatário")
            self.assertEqual(recipient["polo_code_original"], "P")
        finally:
            repo.close()
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
