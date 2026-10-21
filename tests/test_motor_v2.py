import asyncio
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from processwatch.domain import mask_document
from processwatch.engine import Engine
from processwatch.config import Config
from processwatch.monitoring import first_uncovered, import_numbers, sync_djen
from processwatch.providers.djen import DjenError, DjenFetch, parse_communication
from processwatch.repository import SQLiteRepository
from processwatch.sources import require_capability
from processwatch.worker import run_djen_due
from processwatch.discovery import by_number, by_oab
from test_api import FakeProvider, NUMBER, FORMATTED


class MotorV2Tests(unittest.TestCase):
    def test_observed_djen_nested_lawyer_shape_anonymized(self):
        raw = {"id": 1, "numero_processo": NUMBER, "data_disponibilizacao": "2026-03-30",
               "destinatarios": [{"nome": "Pessoa Exemplo", "polo": "P"}],
               "destinatarioadvogados": [{"advogado": {"nome": "Advogado Exemplo",
                                                        "numero_oab": "12345", "uf_oab": "BA"}}]}
        item = parse_communication(raw, NUMBER)
        self.assertEqual(item.available_at, "2026-03-30")
        recipient = next(value for value in item.participants if value.kind == "destinatário")
        lawyer = next(value for value in item.participants if value.kind == "advogado")
        self.assertEqual(recipient.polo, "P")
        self.assertEqual(lawyer.oab, "12345")
        self.assertEqual(lawyer.uf, "BA")

    def test_structured_participants_and_idempotent_storage(self):
        raw = {"id": 901, "numeroProcesso": FORMATTED, "dataDisponibilizacao": "2026-03-23",
               "partes": [{"nome": "Ana Exemplo", "papel": "autora", "polo": "ativo"}],
               "advogados": [{"nome": "Dr. Silva", "oab": "12345", "uf": "BA"}],
               "destinatarios": [{"nome": "INSS"}], "texto": "<p>Comunicação</p>"}
        item = parse_communication(raw, NUMBER)
        self.assertEqual(len(item.participants), 3)
        self.assertEqual(item.participants[0].source, "DJEN_PUBLICACAO")
        self.assertEqual(item.participants[1].oab, "12345")
        self.assertEqual(item.participants[0].normalized_name, "ana exemplo")
        self.assertEqual(item.original, raw)
        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "v2.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 3600, 100)
                payload = item.model_dump()
                for _ in range(2):
                    repo.save_communications("local", NUMBER, [payload], status="período consultado",
                                             period_start="2026-03-01", period_end="2026-03-31")
                self.assertEqual(len(repo.communications("local", NUMBER)), 1)
                self.assertEqual(len(repo.participants("local", NUMBER)), 3)
                self.assertEqual(repo.db.execute("SELECT count(*) FROM raw_records").fetchone()[0], 1)
            finally:
                repo.close()

    def test_old_schema_preserves_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "old.sqlite3")
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE watches(org TEXT, number TEXT, court TEXT, interval INTEGER, created_at TEXT, last_checked TEXT, next_due TEXT, last_error TEXT, metadata TEXT, PRIMARY KEY(org,number))")
            db.execute("INSERT INTO watches VALUES(?,?,?,?,?,?,?,?,?)", ("local", NUMBER, "trf1", 3600, "2026-01-01", None, "2026-01-01", None, None))
            db.execute("PRAGMA user_version=2")
            db.commit(); db.close()
            repo = SQLiteRepository(path)
            try:
                self.assertEqual(repo.get("local", NUMBER)["court"], "trf1")
                self.assertEqual(repo.db.execute("PRAGMA user_version").fetchone()[0], 18)
            finally:
                repo.close()

    def test_batch_portfolios_and_capabilities(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "batch.sqlite3"))
            try:
                engine = Engine(repo, FakeProvider())
                a = import_numbers(engine, repo, "local", [FORMATTED, NUMBER, "invalid"],
                                   portfolio="Equipe A", interval=3600, limit=100)
                b = import_numbers(engine, repo, "local", [NUMBER], portfolio="Equipe B", interval=3600, limit=100)
                self.assertEqual([x["status"] for x in a], ["cadastrado", "duplicado", "erro"])
                self.assertEqual(b[0]["status"], "cadastrado")
                self.assertEqual(repo.portfolio_numbers("local", "Equipe A"), [NUMBER])
                self.assertEqual(repo.portfolio_numbers("local", "Equipe B"), [NUMBER])
                self.assertEqual(len(repo.list_watches("local")), 1)
                self.assertEqual(repo.db.execute("SELECT count(*) FROM collection_jobs").fetchone()[0], 3)
                with self.assertRaises(ValueError):
                    require_capability("PJe/TRF1", "participant_mentions")
                self.assertEqual(by_number(FORMATTED).numbers, (NUMBER,))
                self.assertEqual(by_oab("12345", "ba", "DJEN").uf, "BA")
                with self.assertRaises(ValueError):
                    by_oab("12345", "BA", "PJe/TRF1")
            finally:
                repo.close()

    def test_mask_and_cursor_gap(self):
        self.assertNotIn("1234567", mask_document("123.456.789-01"))
        coverage = [{"status": "período consultado", "period_start": "2024-10-01", "period_end": "2024-12-29"},
                    {"status": "erro", "period_start": "2024-12-30", "period_end": "2025-03-29"},
                    {"status": "período consultado", "period_start": "2025-03-30", "period_end": "2025-04-01"}]
        self.assertEqual(first_uncovered(date(2024, 10, 1), coverage), date(2024, 12, 23))


class HistoricalSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_historical_windows_retry_and_coverage(self):
        class FakeDjen:
            def __init__(self):
                self.calls = []
                self.fail = True

            async def fetch(self, number, start, end):
                self.calls.append((start, end))
                if self.fail and start > date(2024, 10, 1):
                    raise DjenError("HTTP 403")
                item = parse_communication({"id": 901, "numeroProcesso": NUMBER,
                                            "dataDisponibilizacao": "2026-03-23",
                                            "partes": [{"nome": "Ana Exemplo"}]}, NUMBER)
                return DjenFetch([item], start.isoformat(), end.isoformat(), True)

        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "sync.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 3600, 100)
                source = FakeDjen()
                first = await sync_djen(repo, source, "local", NUMBER, initial=date(2024, 10, 1),
                                        end=date(2025, 2, 1), window_days=90)
                self.assertEqual((first.status, first.windows_completed), ("parcial", 1))
                source.fail = False
                second = await sync_djen(repo, source, "local", NUMBER, initial=date(2024, 10, 1),
                                         end=date(2025, 2, 1), window_days=90)
                self.assertEqual(second.status, "ok")
                self.assertEqual(source.calls[-1][1], date(2025, 2, 1))
                self.assertEqual(len(repo.communications("local", NUMBER)), 1)
                self.assertEqual(len(repo.participants("local", NUMBER)), 1)
                self.assertGreaterEqual(len(repo.source_coverage("local", NUMBER, "DJEN")), 3)
            finally:
                repo.close()


class WorkerDjenTests(unittest.TestCase):
    def test_due_run_skips_recent_success(self):
        class EmptyDjen:
            def __init__(self):
                self.calls = 0

            async def fetch(self, number, start, end):
                self.calls += 1
                return DjenFetch([], start.isoformat(), end.isoformat(), True)

        with tempfile.TemporaryDirectory() as folder:
            repo = SQLiteRepository(str(Path(folder) / "worker.sqlite3"))
            try:
                repo.register("local", NUMBER, "trf1", 3600, 100)
                client = EmptyDjen()
                config = Config(djen_initial_date=date.today(), djen_sync_interval=86400)
                self.assertEqual(len(run_djen_due(repo, client, config)), 1)
                self.assertEqual(run_djen_due(repo, client, config), [])
                self.assertEqual(client.calls, 1)
            finally:
                repo.close()

