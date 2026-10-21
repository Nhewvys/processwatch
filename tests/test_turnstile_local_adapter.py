import unittest

from labs.challenge_lab.turnstile_local_interaction import (
    TEST_SITEKEY,
    classify_test_result,
    validate_test_target,
)


class TurnstileLocalAdapterTests(unittest.TestCase):
    def test_accepts_only_localhost_and_official_interactive_test_sitekey(self):
        validate_test_target("http://127.0.0.1:8765/turnstile-interactive-test", TEST_SITEKEY)
        validate_test_target("http://localhost:8765/turnstile-interactive-test", TEST_SITEKEY)

    def test_rejects_remote_page(self):
        with self.assertRaises(ValueError):
            validate_test_target("https://consulta.trf4.jus.br/consulta", TEST_SITEKEY)

    def test_rejects_auto_pass_and_production_sitekeys(self):
        for sitekey in ("1x00000000000000000000AA", "0x4a-production-key"):
            with self.subTest(sitekey=sitekey), self.assertRaises(ValueError):
                validate_test_target("http://127.0.0.1:8765/test", sitekey)

    def test_only_interaction_callback_and_server_testverify_count_as_d(self):
        self.assertEqual(classify_test_result(widget=True, interaction=True, callback=True,
            http_status=200, success=True, test_mode=True),
            "D_interacao_e_validacao_confirmadas_em_teste_oficial")
        self.assertEqual(classify_test_result(widget=True, interaction=False, callback=True,
            http_status=200, success=True, test_mode=True),
            "B_chave_de_teste_sem_interacao_confirmada")
        self.assertEqual(classify_test_result(widget=True, interaction=True, callback=True,
            http_status=503, success=False, test_mode=True),
            "C_interacao_sem_validacao_confirmada")

    def test_missing_widget_is_not_reported_as_solved(self):
        self.assertEqual(classify_test_result(widget=False, interaction=False, callback=False,
            http_status=None, success=False, test_mode=False), "A_widget_ausente")


if __name__ == "__main__":
    unittest.main()

