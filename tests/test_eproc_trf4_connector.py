import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
import asyncio

import httpx

from processwatch.providers.eproc_trf4 import (
    EprocTRF4Connector, SOURCE_ID, SOURCE_LABEL, _challenge, _decode_capture,
    _safe_capture_diagnostic, _observe_search_submission, _safe_destination,
)
from processwatch.providers.contracts import CollectionRequest, ConnectorState
from processwatch.cnj import normalize
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.orchestrator import schedule_sync
from processwatch.repository import SQLiteRepository
from processwatch.api import create_app
from unittest.mock import patch
from processwatch.providers.contracts import ConnectorError
from processwatch.jobs import execute_collection_job


CNJ = "0000000-54.0000.4.04.0000"
CNJ_DIGITS = normalize(CNJ)
OTHER_CNJ = "0000001-39.0000.4.04.0000"


class EprocTRF4ConnectorTests(unittest.TestCase):
    def test_challenge_is_detected_without_solver(self):
        self.assertTrue(_challenge("Portal", "Verificação de segurança"))
        self.assertFalse(_challenge("Portal", "Consulta processual"))

    def test_validates_process_identity_and_normalizes_movements(self):
        capture = {"ok": True, "data": {"process_number": CNJ, "pagination_status": "complete",
            "movements": [{"occurred_at": "2026-10-12T10:00:00-03:00", "date_original": "12/10/2026 10:00",
                           "name": "Distribuído por sorteio", "source_id": "evt-1", "documents": []}]}}
        data, error = _decode_capture(json.dumps(capture), normalize(CNJ))
        self.assertIsNone(error)
        self.assertEqual(data["source"], SOURCE_LABEL)
        self.assertEqual(data["movements"][0]["source_id"], "evt-1")
        self.assertEqual(data["coverage"]["movement_count"], 1)

    def test_rejects_wrong_process_and_incomplete_pagination(self):
        capture = {"ok": True, "data": {"process_number": OTHER_CNJ,
            "pagination_status": "complete", "movements": [{"occurred_at": "2026-10-12T10:00:00Z", "name": "A"}]}}
        self.assertEqual(_decode_capture(json.dumps(capture), normalize(CNJ))[1], "process_identity_mismatch")
        capture["data"]["process_number"] = CNJ
        capture["data"]["pagination_status"] = "possibly_incomplete"
        data, error = _decode_capture(json.dumps(capture), normalize(CNJ))
        self.assertEqual(error, "pagination_incomplete")
        self.assertEqual(data["coverage"]["complete"], False)

    def test_failed_live_extraction_can_keep_only_sanitized_structural_diagnostic(self):
        diagnostic = {"domain": "consulta.trf4.jus.br", "page_type": "trf4_result",
            "cnj_candidate_count": 2, "header_candidate_count": 0,
            "result_region_candidate_count": 1, "table_count": 1,
            "movement_region_count": 1, "rejection": {"code": "main_process_not_identified",
                "message": "O cabeçalho principal não foi identificado com segurança."},
            "candidates": [{"number": CNJ, "element_types": ["DIV", "SPAN", "SCRIPT"],
                "labels_near_candidate": ["Classe", "Agravo de Instrumento", "Nome confidencial"],
                "label_relation": "same_field", "in_header": False,
                "in_result_region": True, "related": False}],
            "tables": [{"element_type": "TABLE", "row_count": 40, "movement_region": True,
                "has_thead": False, "has_tbody": True, "html": "texto privado"}]}
        raw = json.dumps({"ok": False, "code": "main_process_not_identified",
                          "diagnostic": diagnostic, "text": "conteúdo privado"})
        safe = _safe_capture_diagnostic(raw)
        encoded = json.dumps(safe, ensure_ascii=False)
        self.assertEqual(safe["rejection"]["code"], "main_process_not_identified")
        self.assertEqual(safe["result_region_candidate_count"], 1)
        self.assertEqual(safe["candidates"][0]["element_types"], ["DIV", "SPAN"])
        self.assertIn("Agravo de Instrumento", encoded)
        for secret in (CNJ, "Nome confidencial", "texto privado", "conteúdo privado", "html"):
            self.assertNotIn(secret, encoded)

    def test_invalid_extractor_diagnostic_is_discarded(self):
        self.assertIsNone(_safe_capture_diagnostic("not json"))

    def test_search_observation_keeps_only_safe_submission_flags(self):
        class FakeTab:
            async def execute_script(self, script, **kwargs):
                return {"result": {"result": {"value": json.dumps({
                    "search_form_present": True, "search_value_matches": True,
                    "requested_number_visible": False, "cnj_candidate_count": 0,
                    "table_count": 0, "form_count": 2,
                    "body": "personal data that must not be returned"})}}}
        observed = asyncio.run(_observe_search_submission(FakeTab(), CNJ_DIGITS))
        self.assertEqual(observed, {"state": "observed", "search_form_present": True,
            "search_value_matches": True, "requested_number_visible": False,
            "cnj_candidate_count": 0, "table_count": 0, "form_count": 2})

    def test_submission_destination_redacts_query_values_and_sensitive_url_parts(self):
        value = _safe_destination(
            "https://user:secret@consulta.trf4.jus.br/trf4/controlador.php/12345678901234567890"
            "?acao=consulta&txtValor=0000000-54.0000.4.04.0000&token=private#fragment")
        self.assertEqual(value,
            "https://consulta.trf4.jus.br/trf4/controlador.php/[redacted]"
            "?acao=<redacted>&token=<redacted>&txtValor=<redacted>")
        for secret in ("user", "secret", "5008047", "private", "fragment"):
            self.assertNotIn(secret, value)

    def test_submission_trace_records_request_and_response_without_payloads(self):
        from processwatch.providers.eproc_trf4 import _SubmissionTrace
        trace = _SubmissionTrace()
        trace.mark_submit()
        trace._append_network({"event": "request", "method": "GET",
            "destination": "https://consulta.trf4.jus.br/trf4/controlador.php?txtValor=<redacted>",
            "resource_type": "Document"})
        trace._append_network({"event": "response", "destination": "https://consulta.trf4.jus.br/trf4/controlador.php",
            "status": 403, "resource_type": "Document"})
        self.assertEqual(trace.network_events[0]["method"], "GET")
        self.assertEqual(trace.network_events[1]["status"], 403)
        self.assertNotIn(CNJ, json.dumps(trace.network_events))

    def test_invalid_process_is_rejected_before_browser_start(self):
        from processwatch.cnj import normalize
        with self.assertRaises(ValueError):
            normalize("not-cnj")

    def test_direct_trf4_job_is_manual_only_not_scheduled(self):
        class NoopProvider:
            def fetch(self, number, court):
                raise AssertionError("Este teste só registra jobs; não consulta DataJud.")
        with tempfile.TemporaryDirectory(dir=".test-temp") as folder:
            path = str(Path(folder) / "routing.sqlite3")
            repo = SQLiteRepository(path)
            try:
                engine = Engine(repo, NoopProvider())
                engine.register(CNJ)
                config = Config(db=path, api_key="test", djen_initial_date=date(2024, 10, 1))
                automatic = schedule_sync(repo, "local", CNJ_DIGITS, config,
                    collection_type="scheduled_refresh", force=True)
                self.assertNotIn(SOURCE_ID, [job["source"] for job in automatic["jobs"]])
                repo.db.execute("DELETE FROM collection_jobs")
                repo.db.commit()
                manual = schedule_sync(repo, "local", CNJ_DIGITS, config,
                    collection_type="manual_refresh", force=True)
                self.assertIn(SOURCE_ID, [job["source"] for job in manual["jobs"]])
            finally:
                repo.close()

    def test_authenticated_update_endpoint_enqueues_direct_trf4_job(self):
        async def scenario():
            with tempfile.TemporaryDirectory(dir=".test-temp") as folder:
                path = str(Path(folder) / "api-trf4.sqlite3")
                repo = SQLiteRepository(path)
                try:
                    engine = Engine(repo, type("Noop", (), {"fetch": lambda *args: {}})())
                    config = Config(db=path, api_key="test", access_token="token", djen_initial_date=date(2024, 10, 1))
                    app = create_app(repo, engine, config=config)
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                            base_url="http://test", headers={"Authorization": "Bearer token"}) as client:
                        created = await client.post("/api/processes", json={"number": CNJ})
                        self.assertEqual(created.status_code, 201)
                        refreshed = await client.post(f"/api/processos/{CNJ_DIGITS}/atualizar", json={})
                        self.assertEqual(refreshed.status_code, 202)
                        sources = [job["source"] for job in refreshed.json()["synchronization"]["jobs"]]
                        self.assertIn(SOURCE_ID, sources)
                        self.assertLess(sources.index(SOURCE_ID), sources.index("DataJud"))
                        self.assertEqual(refreshed.json()["sources"]["Consulta direta ao tribunal"]["status"], "pending")
                finally:
                    repo.close()
        asyncio.run(scenario())

    def test_worker_records_security_challenge_without_empty_snapshot(self):
        class NoopProvider:
            def fetch(self, number, court):
                raise AssertionError("Nenhuma consulta DataJud deve ocorrer neste teste.")
        class EmptyDjen:
            async def fetch(self, number, start, end):
                raise AssertionError("Nenhuma consulta DJEN deve ocorrer neste teste.")
        class BlockedConnector:
            async def collect(self, request):
                return __import__("processwatch.providers.contracts", fromlist=["ConnectorResult"]).ConnectorResult(
                    SOURCE_ID, ConnectorState.CAPTCHA, found=None,
                    coverage={"status": "captcha_identified", "complete": False, "stage": "search"},
                    error=ConnectorError(SOURCE_ID, "captcha_identified",
                        "O portal solicitou verificação humana após o envio da pesquisa.",
                        ConnectorState.CAPTCHA, False, "search"))
        with tempfile.TemporaryDirectory(dir=".test-temp") as folder:
            path = str(Path(folder) / "worker-trf4.sqlite3")
            repo = SQLiteRepository(path)
            try:
                engine = Engine(repo, NoopProvider())
                engine.register(CNJ)
                repo.enqueue_collection_group("local", CNJ_DIGITS, "manual_refresh",
                    include_datajud=False, include_djen=False, additional_sources=(SOURCE_ID,))
                job = repo.claim_collection_job("local", "test-worker")
                config = Config(db=path, api_key="test")
                with patch("processwatch.jobs.EprocTRF4Connector", return_value=BlockedConnector()):
                    execute_collection_job(job, engine, repo, EmptyDjen(), config)
                saved = next(row for row in repo.latest_collection_group("local", CNJ_DIGITS)
                             if row["source"] == SOURCE_ID)
                self.assertEqual(saved["status"], "awaiting_access")
                self.assertEqual(json.loads(saved["error"])["code"], "captcha_identified")
                self.assertIsNone(repo.latest_court_snapshot("local", CNJ_DIGITS, SOURCE_LABEL))
                self.assertEqual(repo.latest_source_check("local", CNJ_DIGITS, SOURCE_LABEL)["status"],
                                 "captcha_identified")
            finally:
                repo.close()

    def test_direct_snapshot_is_exposed_with_eproc_source_in_authenticated_api(self):
        async def scenario():
            with tempfile.TemporaryDirectory(dir=".test-temp") as folder:
                path = str(Path(folder) / "timeline-trf4.sqlite3")
                repo = SQLiteRepository(path)
                try:
                    engine = Engine(repo, type("Noop", (), {"fetch": lambda *args: {}})())
                    engine.register(CNJ)
                    repo.save_court_snapshot("local", CNJ_DIGITS, SOURCE_LABEL,
                        {"number": CNJ_DIGITS, "source": SOURCE_LABEL,
                         "movements": [{"source": SOURCE_LABEL, "source_id": "eproc-1",
                             "occurred_at": "2026-10-12T10:00:00-03:00",
                             "name": "Distribuído por sorteio", "documents": []}],
                         "coverage": {"complete": True, "pagination_status": "complete", "movement_count": 1}},
                        status="consulta concluída", baseline_if_first=True)
                    app = create_app(repo, engine, config=Config(db=path, access_token="token"))
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                            base_url="http://test", headers={"Authorization": "Bearer token"}) as client:
                        response = await client.get(f"/api/processos/{CNJ_DIGITS}/movimentacoes")
                        self.assertEqual(response.status_code, 200)
                        rows = [item for item in response.json()["items"] if item["source"] == SOURCE_LABEL]
                        self.assertEqual(len(rows), 1)
                        self.assertEqual(rows[0]["name"], "Distribuído por sorteio")
                finally:
                    repo.close()
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

