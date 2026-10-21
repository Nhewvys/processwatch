import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from processwatch.cnj import normalize, tribunal
from processwatch.engine import Engine
from processwatch.repository import SQLiteRepository
from processwatch.worker import run_due
from processwatch.cli import main

NUMBER = "00000003800004010000"
FORMATTED = "0000000-38.0000.4.01.0000"


def movement(code=26, date="2025-01-15T12:00:00.000Z", name="Distribuição", complements=None):
    return {"codigo": code, "dataHora": date, "nome": name, "complementosTabelados": complements or []}


def response(moves=None):
    source = {"id": "fixture-trf1-je", "numeroProcesso": NUMBER, "tribunal": "TRF1", "grau": "JE",
              "nivelSigilo": 0, "sistema": {"codigo": 1, "nome": "PJe"},
              "classe": {"codigo": 436, "nome": "Procedimento do Juizado Especial Cível"},
              "orgaoJulgador": {"codigo": 1, "nome": "Vara simulada"}, "assuntos": [],
              "dataAjuizamento": "20250115000000", "dataHoraUltimaAtualizacao": "2025-02-01T12:00:00Z",
              "movimentos": moves if moves is not None else [movement(), movement(85, "2025-01-16T12:00:00Z", "Petição", [{"codigo": 1, "nome": "tipo", "valor": "inicial"}])]}
    return {"timed_out": False, "hits": {"total": {"value": 1, "relation": "eq"}, "hits": [{"_id": "fixture", "_source": source}]}}


class FakeProvider:
    def __init__(self, raw):
        self.raw = raw

    def fetch(self, number, court):
        if isinstance(self.raw, Exception):
            raise self.raw
        return copy.deepcopy(self.raw)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(str(Path(self.tmp.name) / 'test.sqlite3'))
        self.provider = FakeProvider(response())
        self.engine = Engine(self.repo, self.provider)
        self.engine.register(FORMATTED)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_first_import_no_news_and_repeat(self):
        result = self.engine.check(NUMBER)
        self.assertEqual((result.imported, result.new), (2, 0))
        self.assertEqual(self.repo.events('local', NUMBER), [])
        for _ in range(3):
            result = self.engine.check(NUMBER)
            self.assertEqual(result.new + result.imported + result.corrected, 0)
        self.assertEqual(len(self.repo.timeline('local', NUMBER)), 2)
        self.assertEqual(len(self.repo.runs('local', NUMBER)), 4)
        self.assertEqual(json.loads(self.repo.runs('local', NUMBER)[0]['raw']), response())

    def test_new_movement_then_repeat(self):
        self.engine.check(NUMBER)
        self.provider.raw = response(response()['hits']['hits'][0]['_source']['movimentos'] + [movement(85, '2025-02-01T12:00:00Z', 'Petição')])
        self.assertEqual(self.engine.check(NUMBER).new, 1)
        self.assertEqual(self.engine.check(NUMBER).new, 0)
        self.assertEqual(len(self.repo.timeline('local', NUMBER)), 3)
        self.assertEqual(len(self.repo.events('local', NUMBER)), 1)
        self.assertEqual(self.repo.events('local', NUMBER, after=1), [])

    def test_failed_first_query_does_not_establish_baseline(self):
        self.provider.raw = TimeoutError('segredo não deve vazar')
        with self.assertRaisesRegex(RuntimeError, 'TimeoutError'):
            self.engine.check(NUMBER)
        self.assertEqual(self.repo.runs('local', NUMBER)[0]['status'], 'error')
        self.assertNotIn('segredo', self.repo.runs('local', NUMBER)[0]['error'])
        self.provider.raw = response()
        self.assertEqual(self.engine.check(NUMBER).imported, 2)

    def test_failure_keeps_history(self):
        self.engine.check(NUMBER)
        self.provider.raw = TimeoutError()
        with self.assertRaises(RuntimeError):
            self.engine.check(NUMBER)
        self.assertEqual(len(self.repo.timeline('local', NUMBER)), 2)
        self.assertIsNotNone(self.repo.get('local', NUMBER)['last_error'])
        self.provider.raw = response()
        self.engine.check(NUMBER)
        self.assertIsNone(self.repo.get('local', NUMBER)['last_error'])

    def test_complement_correction_is_not_new(self):
        self.engine.check(NUMBER)
        self.provider.raw['hits']['hits'][0]['_source']['movimentos'][1]['complementosTabelados'][0]['valor'] = 'corrigido'
        result = self.engine.check(NUMBER)
        self.assertEqual((result.new, result.corrected), (0, 1))
        self.assertEqual(len(self.repo.timeline('local', NUMBER)), 2)
        self.assertEqual(len(self.repo.timeline('local', NUMBER, revisions=True)), 3)
        self.assertEqual(self.repo.events('local', NUMBER)[0]['kind'], 'corrected')
        self.assertEqual(self.engine.check(NUMBER).corrected, 0)

    def test_ambiguous_collision_requires_review(self):
        self.engine.check(NUMBER)
        colliding = copy.deepcopy(self.provider.raw['hits']['hits'][0]['_source']['movimentos'][1])
        colliding['complementosTabelados'][0]['valor'] = 'outra petição'
        self.provider.raw['hits']['hits'][0]['_source']['movimentos'].append(colliding)
        result = self.engine.check(NUMBER)
        self.assertEqual((result.new, result.review), (0, 1))
        self.assertEqual(self.engine.check(NUMBER).review, 0)

    def test_order_and_timezone_do_not_duplicate(self):
        complements = [{'codigo': 1, 'valor': 'A'}, {'codigo': 2, 'valor': 'B'}]
        self.provider.raw = response([movement(complements=complements)])
        self.engine.check(NUMBER)
        self.provider.raw = response([movement(date='2025-01-15T09:00:00-03:00', complements=list(reversed(complements)))])
        self.assertEqual(self.engine.check(NUMBER).corrected, 0)
        self.assertEqual(len(self.repo.timeline('local', NUMBER)), 1)

    def test_new_grade_is_historical(self):
        self.engine.check(NUMBER)
        second = copy.deepcopy(self.provider.raw['hits']['hits'][0])
        second['_source']['id'] = 'fixture-g2'
        second['_source']['grau'] = 'G2'
        self.provider.raw['hits']['hits'].append(second)
        result = self.engine.check(NUMBER)
        self.assertEqual((result.imported, result.new), (2, 0))

    def test_invalid_response_preserves_baseline_and_raw(self):
        self.provider.raw['hits']['hits'][0]['_source']['movimentos'][0]['dataHora'] = 'inválida'
        with self.assertRaises(RuntimeError):
            self.engine.check(NUMBER)
        self.assertEqual(len(self.repo.timeline('local', NUMBER)), 0)
        self.assertIsNotNone(self.repo.runs('local', NUMBER)[0]['raw'])

    def test_partial_response_rejected(self):
        self.provider.raw['timed_out'] = True
        with self.assertRaises(RuntimeError):
            self.engine.check(NUMBER)
        self.assertEqual(self.repo.timeline('local', NUMBER), [])

    def test_namespace_and_limit(self):
        self.engine.check(NUMBER)
        with self.assertRaises(ValueError):
            self.repo.get('other', NUMBER)
        self.engine.register(NUMBER, org='other', limit=1)
        self.assertEqual(self.engine.check(NUMBER, org='other').imported, 2)
        second = valid_number('0000001', '2025', '4', '01', '3305')
        with self.assertRaises(ValueError):
            self.engine.register(second, org='other', limit=1)
        self.engine.register(NUMBER, org='other', limit=1)  # repetição não consome cota
        self.assertEqual(len(self.repo.list_watches('other')), 1)

    def test_missing_movement_is_not_deleted(self):
        self.engine.check(NUMBER)
        self.provider.raw = response([movement()])
        self.engine.check(NUMBER)
        self.assertEqual(len(self.repo.timeline('local', NUMBER)), 2)

    def test_worker_isolates_failures(self):
        other = valid_number('0000001', '2025', '4', '01', '3305')
        self.engine.register(other)
        results = run_due(self.engine, self.repo)
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(error is not None for _, _, error in results), 1)
        self.assertEqual(len(self.repo.timeline('local', NUMBER)), 2)

    def test_check_all_checks_every_process_and_isolates_failure(self):
        other = valid_number('0000001', '2025', '4', '01', '3305')
        self.engine.register(other)
        class SelectiveProvider:
            def fetch(self, number, court):
                if number == NUMBER:
                    raise TimeoutError()
                raw = response()
                raw['hits']['hits'][0]['_source']['numeroProcesso'] = number
                return raw
        self.engine.provider = SelectiveProvider()
        results = self.engine.check_all()
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(error is not None for _, _, error in results), 1)
        self.assertEqual(len(self.repo.timeline('local', other)), 2)

    def test_older_document_is_rejected(self):
        self.engine.check(NUMBER)
        self.provider.raw['hits']['hits'][0]['_source']['dataHoraUltimaAtualizacao'] = '2025-01-01T12:00:00Z'
        with self.assertRaisesRegex(RuntimeError, 'versão anterior'):
            self.engine.check(NUMBER)
        self.assertEqual(len(self.repo.timeline('local', NUMBER)), 2)

    def test_out_of_order_run_is_rejected(self):
        from processwatch.normalizer import normalize_response
        old_run = self.repo.start_run('local', NUMBER)
        new_run = self.repo.start_run('local', NUMBER)
        snapshot = normalize_response(response(), NUMBER, 'trf1')
        self.repo.apply('local', NUMBER, new_run, snapshot)
        with self.assertRaisesRegex(ValueError, 'ultrapassada'):
            self.repo.apply('local', NUMBER, old_run, snapshot)
        self.assertEqual(len(self.repo.timeline('local', NUMBER)), 2)

    def test_timezone_fallback(self):
        from zoneinfo import ZoneInfoNotFoundError
        self.engine.check(NUMBER)
        out = io.StringIO()
        with patch('processwatch.cli.ZoneInfo', side_effect=ZoneInfoNotFoundError), patch('sys.stdout', out), patch('sys.stderr', io.StringIO()):
            self.assertEqual(main(['--db', str(Path(self.tmp.name) / 'test.sqlite3'), 'timeline', FORMATTED]), 0)
        self.assertIn('UTC', out.getvalue())

    def test_sql_failure_rolls_back_all_movements(self):
        self.repo.db.execute("CREATE TRIGGER reject_movements BEFORE INSERT ON movements BEGIN SELECT RAISE(ABORT, 'simulated'); END")
        with self.assertRaises(RuntimeError):
            self.engine.check(NUMBER)
        self.assertEqual(self.repo.timeline('local', NUMBER), [])
        self.assertEqual(self.repo.events('local', NUMBER), [])
        self.repo.db.execute('DROP TRIGGER reject_movements')
        self.assertEqual(self.engine.check(NUMBER).imported, 2)

    def test_cli_uses_same_engine(self):
        db = str(Path(self.tmp.name) / 'cli.sqlite3')
        out = io.StringIO()
        with patch('processwatch.cli.DataJud', return_value=self.provider), patch('sys.stdout', out):
            self.assertEqual(main(['--db', db, 'add', FORMATTED]), 0)
            self.assertEqual(main(['--db', db, 'check', FORMATTED]), 0)
            self.assertEqual(main(['--db', db, 'check-all']), 0)
            self.assertEqual(main(['--db', db, 'timeline', FORMATTED]), 0)
        self.assertIn('Histórico importado: 2', out.getvalue())
        self.assertIn('Novas: 0', out.getvalue())
        self.assertIn('tipo', out.getvalue())


def valid_number(seq, year, segment, court, origin):
    tail = year + segment + court + origin
    dd = 98 - int(seq + tail + '00') % 97
    return seq + f'{dd:02d}' + tail


class NumberTests(unittest.TestCase):
    def test_real_number(self):
        self.assertEqual(normalize(FORMATTED), NUMBER)
        self.assertEqual(tribunal(NUMBER), 'trf1')

    def test_bad_numbers(self):
        for number in ('1000236-68.2025.4.01.3305', 'texto' + NUMBER, '123', NUMBER + '0'):
            with self.assertRaises(ValueError):
                normalize(number)

    def test_routing(self):
        for segment, court, expected in [('8', '05', 'tjba'), ('8', '07', 'tjdft'), ('4', '06', 'trf6'), ('5', '01', 'trt1'), ('6', '05', 'tre-ba'), ('9', '26', 'tjmsp')]:
            self.assertEqual(tribunal(normalize(valid_number('0000001', '2025', segment, court, '0001'))), expected)
        self.assertEqual(tribunal(valid_number('0000001', '2025', '1', '00', '0001')), 'stf')


if __name__ == '__main__':
    unittest.main()

