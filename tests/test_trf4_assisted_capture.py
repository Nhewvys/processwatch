import json
import unittest

from scripts.trf4_captura_assistida import safe_evidence


class TRF4AssistedCaptureTests(unittest.TestCase):
    def test_capture_reports_page_structure_without_personal_content_or_tokens(self):
        record = safe_evidence(
            title="Consulta Processual Unificada",
            final_host="consulta.trf4.jus.br",
            body_text="0000000-54.0000.4.04.0000 Parte Nome da Pessoa",
            html='<input name="hdnInfraCaptcha" value="secret-token">',
            tables=[{"id": "movimentos", "class": "lista", "headers": ["Data", "Movimento"],
                     "row_count": 41, "body": "Parte Nome da Pessoa"}],
            controls=["Carregar mais movimentos"],
            fields=["form1:input:txtValor"],
            responses=[{"host": "consulta.trf4.jus.br", "method": "GET", "status": 200,
                        "resource_type": "document", "url": "https://host/path?token=secret-token"}],
        )
        serialized = json.dumps(record, ensure_ascii=False)
        self.assertTrue(record["reference_number_visible"])
        self.assertTrue(record["human_verification_detected"])
        self.assertEqual(record["tables"][0]["row_count"], 41)
        self.assertIn("Carregar mais movimentos", record["pagination_controls"])
        self.assertNotIn("Nome da Pessoa", serialized)
        self.assertNotIn("secret-token", serialized)
        self.assertNotIn("url", serialized)

    def test_case_absence_and_no_challenge_are_distinguished(self):
        record = safe_evidence(title="Consulta", final_host="consulta.trf4.jus.br",
                               body_text="Sem resultados", html="<html></html>", tables=[],
                               controls=[], fields=[], responses=[])
        self.assertFalse(record["reference_number_visible"])
        self.assertFalse(record["human_verification_detected"])
        self.assertEqual(record["tables"], [])


if __name__ == "__main__":
    unittest.main()

