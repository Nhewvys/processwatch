from pathlib import Path
import unittest


class RedesignedInterfaceTests(unittest.TestCase):
    def test_approved_nine_screen_navigation_and_existing_account_controls_remain(self):
        web = Path(__file__).parents[1] / "src/processwatch/web"
        html = (web / "index.html").read_text(encoding="utf-8") + (web / "app.js").read_text(encoding="utf-8")
        for marker in (
            "Visão Geral", "Meus Processos", "Detalhes do Processo", "Monitoramentos",
            "Comunicações e Intimações", "Agenda Jurídica", "Prazos Processuais",
            "Relatórios", "Configurações", "/assets/logo.png", "mobile-menu",
            "data-tab=\"prazos\"", "data-tab=\"monitoramento\"",
            "/api/calendar", "/api/reports/summary", "/api/auth/me", "/api/admin/users",
        ):
            self.assertIn(marker, html)

    def test_manual_deadlines_are_explicitly_not_legal_calculations(self):
        web = Path(__file__).parents[1] / "src/processwatch/web"
        html = (web / "index.html").read_text(encoding="utf-8") + (web / "app.js").read_text(encoding="utf-8")
        self.assertIn("Prazos manuais", html)
        self.assertIn("data não calculada pelo ProcessWatch", html)
        self.assertIn("Leitura interna não representa ciência jurídica oficial", html)


if __name__ == "__main__":
    unittest.main()
