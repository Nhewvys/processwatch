"""Demonstração OFFLINE: dados sintéticos, sem credenciais ou acesso ao CNJ."""
import json
import tempfile
from pathlib import Path
from processwatch.engine import Engine
from processwatch.repository import SQLiteRepository
from processwatch.cli import main

NUMBER = '0000000-38.0000.4.01.0000'


class SimulatedDataJud:
    def __init__(self):
        self.moves = [
            {'codigo': 26, 'nome': 'Distribuição simulada', 'dataHora': '2025-01-15T12:00:00Z'},
            {'codigo': 85, 'nome': 'Petição simulada', 'dataHora': '2025-01-16T12:00:00Z',
             'complementosTabelados': [{'codigo': 1, 'nome': 'tipo', 'valor': 'petição inicial simulada'}]},
        ]

    def fetch(self, number, court):
        return {'timed_out': False, 'hits': {'total': {'value': 1, 'relation': 'eq'}, 'hits': [{'_id': 'demo', '_source': {
            'id': 'demo', 'numeroProcesso': number, 'tribunal': 'TRF1', 'grau': 'JE',
            'classe': {'nome': 'Classe simulada'}, 'sistema': {'codigo': 1}, 'movimentos': self.moves,
        }}]}}


def demo():
    with tempfile.TemporaryDirectory() as folder:
        db = str(Path(folder) / 'demo.sqlite3')
        repository = SQLiteRepository(db)
        try:
            provider = SimulatedDataJud()
            engine = Engine(repository, provider)
            number = engine.register(NUMBER)
            for label in ['1. Importação histórica', '2. Segunda consulta sem novidades']:
                print(label, json.dumps(engine.check(number).__dict__, ensure_ascii=False))
            provider.moves.append({'codigo': 85, 'nome': 'Nova petição simulada', 'dataHora': '2025-02-01T14:00:00Z',
                                   'complementosTabelados': [{'codigo': 1, 'nome': 'tipo', 'valor': 'documento novo simulado'}]})
            print('3. Chegada de movimento', json.dumps(engine.check(number).__dict__))
            print('4. Repetição idempotente', json.dumps(engine.check(number).__dict__))
            assert len(repository.timeline('local', number)) == 3
            assert len(repository.events('local', number)) == 1
            print('TOTAL: 3 movimentos, 1 evento novo, 0 duplicações.')
        finally:
            repository.close()
        main(['--db', db, 'timeline', NUMBER])


if __name__ == '__main__':
    demo()
