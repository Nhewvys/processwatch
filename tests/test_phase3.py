"""Offline regression against a captured official SGT export; no HTTP in tests."""
import json
import tempfile
import unittest
from pathlib import Path

from processwatch.consolidation import consolidate
from processwatch.normalizer import normalize_response
from processwatch.providers.djen import parse_communication
from processwatch.reconciliation import propose_explicit_links
from processwatch.repository import SQLiteRepository
from processwatch.sgt_catalog import import_export
from processwatch.tpu import enrich_movement
from test_api import NUMBER


CATALOG = Path(__file__).resolve().parents[1] / "data" / "tpu" / "SGT-2026-10-06.json"


class Phase3Tests(unittest.TestCase):
    def test_official_export_hierarchy_complements_unknown_and_versions(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "catalog.sqlite3"))
            try:
                version, count = import_export(repo, CATALOG)
                self.assertEqual((version, count), ("SGT-2026-10-06", 13))
                self.assertEqual(repo.tpu_item("246")["name"], "Definitivo")
                self.assertIn("861", repo.tpu_item("246")["parents"])
                self.assertIsNotNone(repo.tpu_item("861"))
                self.assertEqual(repo.tpu_hierarchy("246")[-1], {"code": "246", "name": "Definitivo"})
                self.assertIsNone(repo.tpu_item("999999"))
                self.assertEqual(repo.tpu_item("12750")["parent_code"], "970")
                item = repo.tpu_item("51")
                result = enrich_movement({"complements": json.dumps([{"codigo": 3, "valor": 6}])}, item)
                self.assertEqual(result["complements"][0]["official_value"], "para decisão")
                self.assertTrue(result["complements"][0]["validated"])
                repo.save_tpu_catalog("SGT-2026-10-07", "2026-10-07", "test-only", [
                    {**repo.tpu_item("246"), "version": "SGT-2026-10-07"}])
                self.assertEqual(len(repo.tpu_versions()), 2)
                self.assertEqual(repo.tpu_item("246", version)["name"], "Definitivo")
            finally:
                repo.close()

    def test_reprocess_and_reconcile_keeps_distinct_events(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "case.sqlite3"))
            try:
                import_export(repo, CATALOG)
                repo.register("local", NUMBER, "trf1", 3600, 100)
                source = {"id": "doc", "numeroProcesso": NUMBER, "tribunal": "TRF1", "grau": "G1",
                          "movimentos": [
                              {"id": "m1", "codigo": 1051, "nome": "Decurso de Prazo", "dataHora": "2026-03-30T10:00:00Z"},
                              {"id": "m2", "codigo": 1051, "nome": "Decurso de Prazo", "dataHora": "2026-03-30T11:00:00Z"}]}
                snapshot = normalize_response({"hits": {"hits": [{"_source": source}]}}, NUMBER, "trf1")
                run = repo.start_run("local", NUMBER)
                repo.apply("local", NUMBER, run, snapshot)
                self.assertEqual(repo.reprocess_tpu("local", NUMBER), 2)
                self.assertEqual(repo.reprocess_tpu("local", NUMBER), 2)
                self.assertEqual(repo.db.execute("SELECT count(*) FROM tpu_enrichments").fetchone()[0], 2)
                communication = parse_communication({"id": 900, "numeroProcesso": NUMBER,
                                                     "dataDisponibilizacao": "2026-03-30",
                                                     "idMovimento": "m1", "texto": "Decurso de Prazo"}, NUMBER)
                repo.save_communications("local", NUMBER, [communication.model_dump()], status="período consultado")
                self.assertEqual(propose_explicit_links(repo, "local", NUMBER), 1)
                self.assertEqual(propose_explicit_links(repo, "local", NUMBER), 1)
                links = repo.event_relations("local", NUMBER)
                self.assertEqual(len(links), 1)
                self.assertEqual(links[0]["status"], "possible")
                events = consolidate(NUMBER, repo.timeline("local", NUMBER), repo.communications("local", NUMBER), repo)
                self.assertEqual(len(events), 3)
                self.assertTrue(all(not event["related_to"] for event in events))
                repo.review_relation("local", NUMBER, links[0]["left_id"], links[0]["right_id"],
                                     "confirmed", "test-reviewer")
                events = consolidate(NUMBER, repo.timeline("local", NUMBER), repo.communications("local", NUMBER), repo)
                self.assertEqual(len(events), 3)
                self.assertEqual(sum(bool(event["related_to"]) for event in events), 2)
                self.assertTrue(all(event["original"] for event in events))
            finally:
                repo.close()

