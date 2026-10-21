import json
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IDENTITY_JS = ROOT / "browser-extension" / "processwatch-chrome" / "content" / "trf4_identity.js"
MAIN = "0000000-54.0000.4.04.0000"
ORIGIN = "5001451-75.2025.4.04.7110"


@unittest.skipUnless(shutil.which("node"), "Node.js é necessário para testar o extrator JavaScript da extensão")
class Trf4ExtensionIdentityTests(unittest.TestCase):
    def _run_js(self, body):
        script = (
            "const id=require(process.argv[1]);"
            "let input='';process.stdin.setEncoding('utf8');"
            "process.stdin.on('data',c=>input+=c);"
            "process.stdin.on('end',()=>{const x=JSON.parse(input);"
            "const out=x.action==='sanitize'?id.sanitizeDiagnostic(x.value):x.action==='classify'?"
            "id.classifyCandidate(x.value):x.action==='row'?id.parseMovementCells(x.value):"
            "x.action==='table'?id.isMovementTable(x.value):id.resolveMainProcess(x.candidates,x.requested||[],x.manual||null);"
            "process.stdout.write(JSON.stringify(out));});"
        )
        completed = subprocess.run(
            [shutil.which("node"), "-e", script, str(IDENTITY_JS)],
            input=json.dumps(body),
            text=True, encoding="utf-8", capture_output=True, check=True, timeout=10,
        )
        return json.loads(completed.stdout)

    def resolve(self, candidates, requested=None, manual=None):
        return self._run_js({"candidates": candidates, "requested": requested or [], "manual": manual})

    def sanitize(self, value):
        return self._run_js({"action": "sanitize", "value": value})

    def classify(self, value):
        return self._run_js({"action": "classify", "value": value})

    def parse_row(self, cells):
        return self._run_js({"action": "row", "value": cells})

    def classify_table(self, value):
        return self._run_js({"action": "table", "value": value})

    @staticmethod
    def main(number=MAIN):
        return {"number": number, "isHeader": True, "related": False}

    def test_main_process_with_origin_is_resolved_against_search_number(self):
        result = self.resolve([self.main(), {"number": ORIGIN, "isHeader": False, "related": True}], [MAIN])
        self.assertTrue(result["ok"])
        self.assertEqual(result["processNumber"], "00000005400004040000")

    def test_main_with_several_related_numbers_selects_only_structured_main(self):
        related = ["5001451-75.2025.4.04.7110", "5001111-22.2024.4.04.7000"]
        candidates = [self.main()] + [{"number": n, "isHeader": False, "related": True} for n in related]
        result = self.resolve(candidates, [MAIN])
        self.assertTrue(result["ok"])
        self.assertEqual(result["processNumber"], "00000005400004040000")

    def test_repeated_main_number_in_header_and_other_structured_regions_is_deduplicated(self):
        result = self.resolve([self.main(), self.main()])
        self.assertTrue(result["ok"])

    def test_multiple_main_results_are_blocked(self):
        other = "0000001-39.0000.4.04.0000"
        result = self.resolve([self.main(), self.main(other)])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ambiguous_main_process")

    def test_missing_structured_header_is_blocked_even_when_search_number_exists(self):
        result = self.resolve([], [MAIN])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "main_process_not_identified")

    def test_header_must_match_searched_process(self):
        result = self.resolve([self.main("0000001-39.0000.4.04.0000")], [MAIN])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "process_mismatch")

    def test_invalid_cnj_in_main_header_is_blocked(self):
        result = self.resolve([self.main("5008047-14.2026.4.04.0000")])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "invalid_cnj")

    def test_reference_cnj_is_valid_and_resolves_to_expected_process(self):
        result = self.resolve([self.main()], [MAIN])
        self.assertTrue(result["ok"])
        self.assertEqual(result["processNumber"], "00000005400004040000")

    def test_manual_selection_from_structured_result_region_is_allowed(self):
        candidate = {"number": MAIN, "isHeader": False, "isResultRegion": True, "related": False}
        result = self.resolve([candidate], [MAIN], MAIN)
        self.assertTrue(result["ok"])
        self.assertTrue(result["selectedManually"])

    def test_manual_selection_outside_result_region_is_rejected(self):
        candidate = {"number": MAIN, "isHeader": False, "isResultRegion": False, "related": False}
        result = self.resolve([candidate], [MAIN], MAIN)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "manual_selection_invalid")

    def test_manual_selection_rejects_origin_or_related_process(self):
        candidate = {"number": MAIN, "isHeader": False, "isResultRegion": True, "related": True}
        result = self.resolve([candidate], [MAIN], MAIN)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "manual_selection_invalid")

    def test_manual_selection_must_match_consulted_number(self):
        other = "0000001-39.0000.4.04.0000"
        result = self.resolve([{"number": other, "isResultRegion": True}], [MAIN], other)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "process_mismatch")

    def test_diagnostic_reports_multiple_cnj_candidates_without_selecting_first(self):
        candidates = [{"number": MAIN, "isHeader": False, "isResultRegion": True},
                      {"number": ORIGIN, "isResultRegion": True, "related": True}]
        result = self.resolve(candidates, [MAIN])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "main_process_not_identified")

    def test_sanitized_diagnostic_keeps_bounded_structure_and_drops_sensitive_fields(self):
        diagnostic = self.sanitize({
            "domain": "consulta.trf4.jus.br", "pageType": "trf4_result", "candidateCount": 3,
            "rejectionCode": "main_process_not_identified", "rejectionMessage": "CPF 12345678901 token secret",
            "cookies": "cookie-secret", "url": "https://consulta.trf4.jus.br/?token=secret",
            "candidates": [{"number": MAIN, "elementTypes": ["H2", "SCRIPT"],
                            "labels": ["Processo principal Agravo de Instrumento Maria da Silva CPF 123"],
                            "isHeader": False, "isResultRegion": True, "related": False,
                            "text": "movimento sensível", "url": "https://example.invalid"}],
            "tables": [{"rowCount": 8, "movementRegion": True, "html": "texto privado"}],
        })
        encoded = json.dumps(diagnostic, ensure_ascii=False)
        for secret in ("12345678901", "cookie-secret", "token", "Maria", "movimento sensível", "example.invalid", "secret"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(diagnostic["cnj_candidate_count"], 3)
        self.assertEqual(diagnostic["candidates"][0]["element_types"], ["H2"])
        self.assertIn("Agravo de Instrumento", diagnostic["candidates"][0]["labels_near_candidate"])

    def test_header_not_recognized_does_not_use_search_number_as_authority(self):
        result = self.resolve([], [MAIN], MAIN)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "manual_selection_invalid")

    def test_originario_in_shared_ancestor_does_not_relabel_main_cnj(self):
        candidate = self.classify({"number": MAIN, "localText": MAIN, "fieldText": f"Processo: {MAIN}",
                                   "siblingLabels": ["Agravo de Instrumento"],
                                   "headerText": f"Originário {ORIGIN} — Processo {MAIN} — Agravo de Instrumento",
                                   "elementTypes": ["DIV", "SECTION"]})
        self.assertFalse(candidate["related"])
        self.assertTrue(candidate["isHeader"])
        self.assertTrue(candidate["isResultRegion"])
        self.assertNotIn("Originário", candidate["labels"])

    def test_originario_label_in_same_structured_field_is_related(self):
        candidate = self.classify({"number": ORIGIN, "localText": ORIGIN, "fieldText": f"Originário: {ORIGIN}",
                                   "elementTypes": ["DIV", "SECTION"]})
        self.assertTrue(candidate["related"])
        self.assertFalse(candidate["isHeader"])
        self.assertFalse(candidate["isResultRegion"])

    def test_two_cnj_fields_with_identical_shared_labels_are_classified_by_local_field(self):
        main = self.classify({"number": MAIN, "fieldText": f"Processo: {MAIN}", "siblingLabels": ["Agravo de Instrumento"],
                              "headerText": f"Processo Agravo de Instrumento Originário {ORIGIN}"})
        origin = self.classify({"number": ORIGIN, "fieldText": f"Originário: {ORIGIN}", "siblingLabels": ["Agravo de Instrumento"],
                                "headerText": f"Processo Agravo de Instrumento Originário {MAIN}"})
        self.assertTrue(main["isHeader"])
        self.assertFalse(main["related"])
        self.assertTrue(origin["related"])

    def test_table_with_38_rows_and_no_thead_is_recognized_by_row_evidence(self):
        rows = [{"cells": [{"text": f"{day:02d}/09/2026"}, {"text": "Juntada de petição"}]} for day in range(1, 39)]
        self.assertTrue(self.classify_table({"headers": "", "rows": rows}))

    def test_header_in_tbody_is_enough_to_identify_movement_table(self):
        self.assertTrue(self.classify_table({"headers": "Data Movimento", "rows": []}))

    def test_colspan_single_cell_row_keeps_date_and_description(self):
        parsed = self.parse_row([{"text": "14/04/2026 10:35 Expedição de intimação"}])
        self.assertEqual(parsed["occurred_at"], "2026-04-14T10:35:00")
        self.assertEqual(parsed["name"], "Expedição de intimação")

    def test_separate_date_and_time_cells_are_combined_and_original_kept(self):
        parsed = self.parse_row([{"text": "14/04/2026"}, {"text": "09:07"}, {"text": "Juntada de certidão"}])
        self.assertEqual(parsed["occurred_at"], "2026-04-14T09:07:00")
        self.assertEqual(parsed["date_original"], "14/04/2026 09:07")

    def test_header_or_row_without_movement_description_is_rejected(self):
        self.assertIsNone(self.parse_row([{"text": "Data"}, {"text": "Movimento"}]))
        self.assertIsNone(self.parse_row([{"text": "14/04/2026"}]))

    def test_diagnostic_table_rows_expose_structure_without_cell_text(self):
        diagnostic = self.sanitize({"domain": "consulta.trf4.jus.br", "pageType": "trf4_result",
                                    "tables": [{"rowCount": 38, "movementRegion": True, "hasThead": False,
                                                "hasTbody": True, "rows": [{"cellCount": 2,
                                                "cellTypes": ["TD", "TD"], "hasColspan": True,
                                                "hasRowspan": False, "hasDate": True, "hasTime": True,
                                                "linkCount": 1, "text": "CPF 123"}]}]})
        row = diagnostic["tables"][0]["rows"][0]
        self.assertTrue(row["has_colspan"])
        self.assertEqual(row["link_count"], 1)
        self.assertNotIn("text", row)


if __name__ == "__main__":
    unittest.main()

