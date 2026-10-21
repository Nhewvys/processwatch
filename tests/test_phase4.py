"""Offline tests: official captured sheet and mocked DJEN HTTP are distinct from live checks."""
import asyncio
from datetime import date
import httpx
import json
from pathlib import Path
import tempfile
import unittest

from processwatch.discovery import discover_oab
from processwatch.providers.djen import DjenClient, DjenError
from processwatch.repository import SQLiteRepository
from processwatch.sgt_catalog import import_export, import_official_spreadsheet
from processwatch.source_matrix import get_source, load_matrix
from test_api import NUMBER


ROOT = Path(__file__).resolve().parents[1]
SHEET = ROOT / "data/tpu/SGT-2026-10-06-Justica-Federal-1G.xls"
SOAP = ROOT / "data/tpu/SGT-2026-10-06.json"


class Phase4CatalogTests(unittest.TestCase):
    def test_official_bulk_import_preserves_soap_complements_and_idempotence(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "db.sqlite3"))
            try:
                import_export(repo, SOAP)
                previous = repo.tpu_item("51")
                version, total, added, digest = import_official_spreadsheet(repo, SHEET)
                self.assertEqual(version, "SGT-2026-10-06")
                self.assertGreater(total, 400)
                self.assertGreater(added, 400)
                self.assertEqual(len(digest), 64)
                self.assertEqual(repo.tpu_item("51"), previous)
                self.assertIsNone(repo.tpu_item("999999"))
                self.assertEqual(import_official_spreadsheet(repo, SHEET)[2], 0)
                self.assertEqual(len(repo.tpu_versions()), 1)
            finally:
                repo.close()

    def test_reject_truncated_or_wrong_version_sheet(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "db.sqlite3"))
            broken = Path(folder) / "broken.xls"
            broken.write_bytes(b"<table><tr><td>Inventado</td></tr></table>")
            try:
                with self.assertRaises(ValueError):
                    import_official_spreadsheet(repo, broken)
                with self.assertRaises(ValueError):
                    import_official_spreadsheet(repo, SHEET, version="SGT-2027-01-01")
            finally:
                repo.close()

    def test_matrix_requires_real_validation_for_operational_status(self):
        sources = load_matrix()
        self.assertEqual(len(sources), 192)
        self.assertEqual(sum(row["integration_state"] == "investigated_not_operational" for row in sources), 15)
        self.assertEqual(get_source("TJPR-PROJUDI")["integration_state"], "investigated_not_operational")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "matrix.json"
            from processwatch.source_matrix import load_document
            document = load_document()
            bad = {**document, "sources": [{**sources[0], "integration_state": "operational"}]}
            path.write_text(json.dumps(bad), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_matrix(path)


def communication(identifier, number=NUMBER, oab="12345", uf="BA"):
    return {"id": identifier, "numero_processo": number, "data_disponibilizacao": "2026-03-23",
            "destinatarioadvogados": [{"nome": "Advogado Exemplo", "numero_oab": oab, "uf_oab": uf}],
            "texto": "Publicação de teste"}


class Phase4DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_oab_pagination_exact_evidence_and_idempotence(self):
        calls = []

        def respond(request):
            page = int(request.url.params["pagina"])
            calls.append(page)
            self.assertEqual(request.url.params["numeroOab"], "12345")
            self.assertEqual(request.url.params["ufOab"], "BA")
            self.assertEqual(request.url.params["itensPorPagina"], "100")
            first = [communication(i) for i in range(1, 100)] + [communication(100, oab="99999")]
            return httpx.Response(200, json={"count": 101, "items": first if page == 1 else [communication(101)]})

        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "db.sqlite3"))
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                client = DjenClient(http)
                day = date(2026, 3, 23)
                result = await discover_oab(repo, client, "local", "12345", "ba", day, day)
                self.assertEqual((result["pages"], result["reported"], result["matched"]), (2, 101, 100))
                self.assertEqual(result["processes"][0]["monitored"], 0)
                self.assertEqual(repo.db.execute("SELECT count(*) FROM watches").fetchone()[0], 0)
                self.assertEqual(repo.db.execute("SELECT count(*) FROM discovered_processes").fetchone()[0], 100)
                self.assertEqual(repo.db.execute("SELECT count(*) FROM raw_records").fetchone()[0], 100)
                await discover_oab(repo, client, "local", "12345", "BA", day, day)
                self.assertEqual(repo.db.execute("SELECT count(*) FROM discovered_processes").fetchone()[0], 100)
                self.assertEqual(calls, [1, 2, 1, 2])
            repo.close()

    async def test_failure_records_unknown_coverage_without_discovery(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "db.sqlite3"))
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as http:
                with self.assertRaises(DjenError):
                    day = date(2026, 3, 23)
                    await discover_oab(repo, DjenClient(http), "local", "12345", "BA", day, day)
            self.assertEqual(repo.db.execute("SELECT status FROM discovery_runs").fetchone()[0], "error")
            self.assertEqual(repo.db.execute("SELECT count(*) FROM discovered_processes").fetchone()[0], 0)
            repo.close()

