import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pydoll_diagnostico.py"
spec = importlib.util.spec_from_file_location("pydoll_diagnostic", SCRIPT)
pydoll_diagnostic = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(pydoll_diagnostic)


class PydollDiagnosticTests(unittest.TestCase):
    def test_detects_security_challenge_without_attempting_resolution(self):
        self.assertTrue(pydoll_diagnostic.detect_challenge("Verificação", "Cloudflare check"))
        self.assertFalse(pydoll_diagnostic.detect_challenge("Página de consulta", "Processo público"))

    def test_classifies_timeout_and_network_failures_without_message_content(self):
        self.assertEqual(pydoll_diagnostic.classify_error(TimeoutError("detail is omitted")), "timeout")
        self.assertEqual(pydoll_diagnostic.classify_error(ConnectionError("private detail is omitted")), "network_error")

    def test_table_summary_contains_structure_but_never_cell_text(self):
        summary = pydoll_diagnostic.summarize_tables(
            "<table><tr><th>header-private</th></tr><tr><td>movement-sensitive</td></tr></table>"
        )
        self.assertEqual(summary, {"table_count": 1, "row_counts": [2]})
        self.assertNotIn("movement-sensitive", str(summary))
        self.assertNotIn("header-private", str(summary))

    def test_process_number_format_is_checked_before_browser_launch(self):
        self.assertTrue(pydoll_diagnostic.valid_trf4_cnj_format("0000000-54.0000.4.04.0000"))
        self.assertFalse(pydoll_diagnostic.valid_trf4_cnj_format("not-a-cnj"))
        self.assertFalse(pydoll_diagnostic.valid_trf4_cnj_format("5008047-14.2026.4.04.0000"))


if __name__ == "__main__":
    unittest.main()

