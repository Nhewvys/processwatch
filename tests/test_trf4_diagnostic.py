import unittest

from scripts.trf4_diagnostico import classify_response


class TRF4DiagnosticTests(unittest.TestCase):
    def test_current_public_search_form_is_not_mistaken_for_case_result(self):
        body = b"<title>Consulta Processual Unificada</title><select id='selForma'></select><input id='txtValor'>"
        self.assertEqual(classify_response(200, body),
                         "formulario_publico_disponivel_sem_consulta_de_processo")

    def test_captcha_is_reported_as_human_verification_not_missing_case(self):
        body = b"<input id='hdnInfraCaptcha'><script>Aguarde a verificacao do CAPTCHA</script>"
        self.assertEqual(classify_response(200, body), "verificacao_humana_detectada")

    def test_http_barriers_are_distinct(self):
        self.assertEqual(classify_response(403, b""), "acesso_restrito")
        self.assertEqual(classify_response(429, b""), "limite_de_requisicoes")
        self.assertEqual(classify_response(503, b""), "portal_indisponivel")

    def test_http_200_without_known_structure_is_not_declared_operational(self):
        self.assertEqual(classify_response(200, b"<html>pagina inesperada</html>"),
                         "resposta_http_valida_sem_formulario_confirmado")


if __name__ == "__main__":
    unittest.main()

