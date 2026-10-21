"""Exercise the TRF4 job path with a synthetic CNJ and external providers disabled."""
import asyncio
import json
import tempfile
from pathlib import Path
import httpx
from processwatch.api import create_app
from processwatch.config import Config
from processwatch.cnj import normalize
from processwatch.engine import Engine
from processwatch.jobs import execute_collection_job
from processwatch.repository import SQLiteRepository
from processwatch.providers.eproc_trf4 import SOURCE_ID, SOURCE_LABEL

NUMBER = normalize('0000000-54.0000.4.04.0000')

class ForbiddenExternalProvider:
    def fetch(self, *args, **kwargs):
        raise AssertionError('Other sources are not permitted in this diagnostic')

async def api_result(repo, engine, config):
    app = create_app(repo, engine, config=config)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url='http://test', headers={'Authorization': 'Bearer diagnostic-local'}) as client:
        response = await client.get(f'/api/processes/{NUMBER}/sync')
        response.raise_for_status()
        job = next(j for j in response.json()['jobs'] if j['source'] == SOURCE_ID)
        return {'http_status': response.status_code, 'job_status': job['status'],
                'coverage': job['coverage'], 'error': job['error']}

def main():
    with tempfile.TemporaryDirectory(dir='.test-temp') as folder:
        path = str(Path(folder) / 'trf4-j8.sqlite3')
        repo = SQLiteRepository(path)
        try:
            engine = Engine(repo, ForbiddenExternalProvider())
            engine.register(NUMBER)
            repo.enqueue_collection_group('local', NUMBER, 'manual_refresh',
                include_datajud=False, include_djen=False, additional_sources=(SOURCE_ID,))
            job = repo.claim_collection_job('local', 'j8-diagnostic')
            assert job['source'] == SOURCE_ID
            config = Config(db=path, timeout=45, access_token='diagnostic-local')
            execute_collection_job(job, engine, repo, ForbiddenExternalProvider(), config)
            saved = next(j for j in repo.latest_collection_group('local', NUMBER) if j['source'] == SOURCE_ID)
            result = {'source': SOURCE_LABEL, 'job_id': job['id'], 'worker_status': saved['status'],
                      'result': json.loads(saved['result'] or '{}'),
                      'snapshot_created': repo.latest_court_snapshot('local', NUMBER, SOURCE_LABEL) is not None,
                      'api': asyncio.run(api_result(repo, engine, config))}
            output = Path('docs/evidencia-j8.json')
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(output.read_text(encoding='utf-8'))
        finally:
            repo.close()

if __name__ == '__main__':
    main()

