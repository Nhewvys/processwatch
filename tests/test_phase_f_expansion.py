"""Regression checks for the investigated-but-not-integrated second-source candidates."""
import unittest

from processwatch.routing import route_process
from processwatch.source_matrix import load_matrix
from processwatch.providers.pje_trf1 import registered_connector_ids


class PhaseFInvestigationTests(unittest.TestCase):
    def test_trf5_captcha_candidate_never_routes_to_trf1_or_becomes_operational(self):
        number = "00039117220254058308"
        result = route_process(
            number,
            {"tribunal": "TRF5", "sistema": {"nome": "PJe"}, "grau": "JE"},
            connector_ids={"TRF1-PJE-JE"},
        )
        self.assertEqual(result["tribunal_id"], "TRF5")
        self.assertIsNone(result["selected_connector"])
        self.assertNotEqual(result["selected_connector"], "TRF1-PJE-JE")
        self.assertEqual(result["status"], "captcha_identified")

    def test_second_source_candidates_are_documented_but_not_registered(self):
        rows = {item["id"]: item for item in load_matrix()}
        candidates = {
            "TRF5-PJE-2X-1G",
            "TRF5-PJE-2X-JE",
            "TRF5-PJE-2X-TR",
            "TRF5-PJE-2X-TRU",
            "TRF5-PJE-2X-2G",
            "TJSP-ESAJ-1G",
            "TJPR-PROJUDI-1G",
        }
        self.assertTrue(candidates.issubset(rows))
        for source in candidates:
            self.assertNotEqual(rows[source]["integration_state"], "operational")
            self.assertFalse(rows[source]["real_validation"])
        registered = registered_connector_ids()
        self.assertTrue(registered)
        self.assertEqual(registered, {"TRF1-PJE-1G", "TRF1-PJE-JE"})


if __name__ == "__main__":
    unittest.main()

