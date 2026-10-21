import io
import json
import unittest
from urllib.error import HTTPError, URLError
from processwatch.config import Config
from processwatch.providers.datajud import DataJud, ProviderError


class Reply(io.BytesIO):
    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode())


def payload(hits=None, total=None):
    hits = hits or []
    return {'hits': {'hits': hits, 'total': {'value': len(hits) if total is None else total, 'relation': 'eq'}}}


class Transport:
    def __init__(self, sequence):
        self.sequence, self.calls = list(sequence), []

    def __call__(self, request, timeout):
        self.calls.append((request, timeout))
        value = self.sequence.pop(0)
        if isinstance(value, Exception):
            raise value
        return Reply(value)


def http(code, headers=None):
    return HTTPError('https://example.invalid', code, 'simulated', headers or {}, io.BytesIO(b'body'))


class ProviderTests(unittest.TestCase):
    def provider(self, sequence, **kwargs):
        transport = Transport(sequence)
        waits = []
        provider = DataJud(Config(api_key='FAKE_TEST_KEY', min_interval=0, **kwargs), opener=transport, sleep=waits.append)
        return provider, transport, waits

    def test_auth_endpoint_and_timeout(self):
        provider, transport, _ = self.provider([payload()], timeout=91)
        provider.fetch('123', 'trf1')
        request, timeout = transport.calls[0]
        self.assertEqual(request.full_url, 'https://api-publica.datajud.cnj.jus.br/api_publica_trf1/_search')
        self.assertEqual(request.get_header('Authorization'), 'APIKey FAKE_TEST_KEY')
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(timeout, 91)

    def test_temporary_failure_recovers(self):
        for failure in (http(500), http(503), URLError('simulated'), TimeoutError()):
            with self.subTest(failure=failure):
                provider, transport, waits = self.provider([failure, payload()])
                self.assertEqual(provider.fetch('123', 'trf1')['hits']['hits'], [])
                self.assertEqual(len(transport.calls), 2)
                self.assertTrue(any(wait >= 1 for wait in waits))

    def test_401_403_no_retry(self):
        for status in (401, 403, 400):
            provider, transport, _ = self.provider([http(status)])
            with self.assertRaisesRegex(ProviderError, str(status)):
                provider.fetch('123', 'trf1')
            self.assertEqual(len(transport.calls), 1)

    def test_429_retry_after(self):
        provider, _, waits = self.provider([http(429, {'Retry-After': '5'}), payload()])
        provider.fetch('123', 'trf1')
        self.assertIn(5, waits)

    def test_long_retry_after_stops_without_early_request(self):
        provider, transport, _ = self.provider([http(429, {'Retry-After': '600'})])
        with self.assertRaisesRegex(ProviderError, 'Retry-After'):
            provider.fetch('123', 'trf1')
        self.assertEqual(len(transport.calls), 1)

    def test_exhausted_retries(self):
        provider, transport, _ = self.provider([http(503)] * 3)
        with self.assertRaisesRegex(ProviderError, '3 tentativas'):
            provider.fetch('123', 'trf1')
        self.assertEqual(len(transport.calls), 3)

    def test_pagination(self):
        provider, transport, _ = self.provider([payload([{'_id': str(i)} for i in range(100)], 101), payload([{'_id': '100'}], 101)])
        result = provider.fetch('123', 'trf1')
        self.assertEqual(len(result['hits']['hits']), 101)
        self.assertEqual(json.loads(transport.calls[1][0].data)['from'], 100)

    def test_partial_search_rejected(self):
        provider, _, _ = self.provider([dict(payload(), timed_out=True)])
        with self.assertRaises(ProviderError):
            provider.fetch('123', 'trf1')

    def test_missing_key_no_request(self):
        transport = Transport([])
        with self.assertRaises(ProviderError):
            DataJud(Config(), opener=transport).fetch('123', 'trf1')
        self.assertEqual(transport.calls, [])

    def test_request_spacing(self):
        transport = Transport([payload(), payload()])
        clock = iter([10, 10.2, 12])
        waits = []
        provider = DataJud(Config(api_key='FAKE', min_interval=2), opener=transport, sleep=waits.append, clock=lambda: next(clock))
        provider.fetch('123', 'trf1')
        provider.fetch('123', 'trf1')
        self.assertAlmostEqual(waits[0], 1.8)


if __name__ == '__main__':
    unittest.main()

