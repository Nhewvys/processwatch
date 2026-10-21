import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx
from processwatch.providers.trf4_protection import classify, OBSERVE
from processwatch.providers.eproc_trf4 import _observe_protection, _failure, SOURCE_ID, SOURCE_LABEL
from processwatch.providers.contracts import ConnectorState
from processwatch.repository import SQLiteRepository
from processwatch.engine import Engine
from processwatch.config import Config
from processwatch.jobs import execute_collection_job
from processwatch.api import create_app

URL = 'https://consulta.trf4.jus.br/trf4/controlador.php?token=SECRET#cookie'
NUMBER = '00000005400004040000'

class ProtectionTests(unittest.TestCase):
    def classify(self, signals, **kwargs):
        return classify(signals, 'search', URL, **kwargs)

    def test_turnstile_visible_iframe(self):
        self.assertEqual(self.classify({'turnstile_frame': True})['type'], 'cloudflare_turnstile')

    def test_real_observed_turnstile_signals(self):
        result = self.classify({'turnstile_container': True, 'turnstile_response_field': True})
        self.assertFalse(result['retryable'])
        self.assertEqual(result['pydoll_compatibility'], 'widget_detected_not_validated')

    def test_managed_challenge(self):
        self.assertEqual(self.classify({'cf_challenge_config': True, 'challenge_title': True})['type'],
                         'cloudflare_managed_challenge')

    def test_access_block_has_priority(self):
        result = self.classify({'access_denied': True, 'turnstile_frame': True})
        self.assertEqual(result['state'], 'unauthorized')

    def test_http_restriction(self):
        self.assertEqual(self.classify({}, http_status=403)['type'], 'access_restricted')

    def test_portal_captcha(self):
        self.assertEqual(self.classify({'portal_captcha': True})['type'], 'portal_captcha')

    def test_authentication(self):
        self.assertEqual(self.classify({'authentication_form': True})['type'], 'authentication_required')

    def test_unknown(self):
        self.assertEqual(self.classify({'verification_message': True})['type'], 'unknown_challenge')

    def test_passive_cloudflare_assets_are_not_challenge(self):
        self.assertIsNone(self.classify({'cf_challenge_script': True}))

    def test_hidden_response_field_alone_is_not_challenge(self):
        self.assertIsNone(self.classify({'turnstile_response_field': True}))

    def test_normal_form_and_no_challenge(self):
        self.assertIsNone(self.classify({'search_form': True}))
        self.assertIsNone(self.classify({}))

    def test_sanitized_diagnostics(self):
        result = self.classify({'verification_message': True, 'SECRET': True, 'text': 'CPF TOKEN'})
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('CPF', json.dumps(result))
        self.assertNotIn('?', result['url'])
        self.assertIsNone(result['http_status'])
        self.assertNotIn('document.cookie', OBSERVE)

    def test_timeout_and_network_are_not_captcha(self):
        for state, code in [(ConnectorState.TIMEOUT, 'timeout'), (ConnectorState.COURT_UNAVAILABLE, 'network_error')]:
            result = _failure(state, code, 'falha', 'search', True)
            self.assertNotIn('protection', result.coverage)
            self.assertTrue(result.error.retryable)

    def test_classifier_through_worker_persistence_and_authenticated_api(self):
        class Tab:
            async def execute_script(self, *args, **kwargs):
                return {'result': {'result': {'value': json.dumps({'turnstile_container': True, 'turnstile_response_field': True})}}}
            async def current_url(self):
                return URL
        class Connector:
            async def collect(self, request):
                return await _observe_protection(Tab(), 'search')
        class NoExternal:
            def fetch(self, *args):
                raise AssertionError('No external sources allowed')
        with tempfile.TemporaryDirectory(dir='.test-temp') as folder:
            path = str(Path(folder) / 'test.sqlite3')
            repo = SQLiteRepository(path)
            try:
                engine = Engine(repo, NoExternal())
                engine.register(NUMBER)
                repo.enqueue_collection_group('local', NUMBER, 'manual_refresh', include_datajud=False,
                    include_djen=False, additional_sources=(SOURCE_ID,))
                job = repo.claim_collection_job('local', 'test')
                config = Config(db=path, access_token='test-token')
                with patch('processwatch.jobs.EprocTRF4Connector', return_value=Connector()):
                    execute_collection_job(job, engine, repo, NoExternal(), config)
                self.assertIsNone(repo.latest_court_snapshot('local', NUMBER, SOURCE_LABEL))
                async def check():
                    app = create_app(repo, engine, config=config)
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                        self.assertEqual((await client.get(f'/api/processes/{NUMBER}/sync')).status_code, 401)
                        response = await client.get(f'/api/processes/{NUMBER}/sync', headers={'Authorization':'Bearer test-token'})
                        row = next(j for j in response.json()['jobs'] if j['source'] == SOURCE_ID)
                        self.assertEqual(row['status'], 'awaiting_access')
                        self.assertEqual(row['coverage']['protection']['type'], 'cloudflare_turnstile')
                        self.assertFalse(row['error']['retryable'])
                asyncio.run(check())
            finally:
                repo.close()

