import json
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from labs.challenge_lab.server import Handler, GOOGLE_TEST_SECRET, HCAPTCHA_TEST_SECRET
from http.server import ThreadingHTTPServer


class ChallengeLabServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_local_demo_page_is_served(self):
        with urlopen(self.base + "/") as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("não contêm CAPTCHA real", body)
        self.assertIn("Validação correta", body)

    def test_interactive_test_page_uses_only_official_dummy_sitekey(self):
        with urlopen(self.base + "/turnstile-interactive-test") as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("3x00000000000000000000FF", body)
        self.assertNotIn("data-sitekey=\"your-real", body)

    def test_vendor_test_forms_use_documented_dummy_sitekeys_and_server_validation(self):
        with urlopen(self.base + "/recaptcha-test") as response:
            google = response.read().decode("utf-8")
        with urlopen(self.base + "/hcaptcha-test") as response:
            hcaptcha = response.read().decode("utf-8")
        self.assertIn("6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI", google)
        self.assertIn("grecaptcha.execute(window.testState.widgetId)", google)
        self.assertIn("/api/recaptcha-test-verify", google)
        self.assertNotIn(GOOGLE_TEST_SECRET, google)
        self.assertIn("10000000-ffff-ffff-ffff-000000000001", hcaptcha)
        self.assertIn("hcaptcha.execute(window.testState.widgetId)", hcaptcha)
        self.assertIn("/api/hcaptcha-test-verify", hcaptcha)
        self.assertNotIn(HCAPTCHA_TEST_SECRET, hcaptcha)
        self.assertIn("window.testState.result?.success!==true", google)

    def test_vendor_success_route_is_limited_to_known_test_providers(self):
        with urlopen(self.base + "/provider-success?provider=hcaptcha") as response:
            body = response.read().decode("utf-8")
        self.assertEqual(response.status, 200)
        self.assertIn("Formulário aceito após validação oficial de teste", body)
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.base + "/provider-success?provider=unknown")
        self.assertEqual(raised.exception.code, 400)

    def test_test_siteverify_rejects_non_dummy_token_without_forwarding(self):
        request = Request(self.base + "/api/turnstile-test-verify",
                          data=json.dumps({"token": "not-a-dummy-token"}).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code, 400)
        body = json.loads(raised.exception.read())
        self.assertEqual(body["error"], "not_a_documented_dummy_token")

    def test_form_validation_and_simulated_failure_states(self):
        expected = {"valid": (200, "accepted"), "network": (503, "simulated_network_failure"),
                    "expired": (410, "simulated_expiration"), "challenge": (423, "awaiting_access")}
        for mode, (status, state) in expected.items():
            with self.subTest(mode=mode):
                try:
                    response = urlopen(f"{self.base}/api/demo?mode={mode}")
                    actual_status, body = response.status, response.read()
                except HTTPError as error:
                    actual_status, body = error.code, error.read()
                self.assertEqual(actual_status, status)
                self.assertEqual(json.loads(body)["status"], state)

    def test_unknown_demo_mode_is_rejected(self):
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.base + "/api/demo?mode=unknown")
        self.assertEqual(raised.exception.code, 400)


if __name__ == "__main__":
    unittest.main()

