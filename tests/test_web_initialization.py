import re
import unittest
from pathlib import Path


WEB = Path(__file__).parents[1] / "src" / "processwatch" / "web" / "index.html"


class WebInitializationRegressionTests(unittest.TestCase):
    @staticmethod
    def app_source():
        return WEB.read_text(encoding="utf-8") + "\n" + WEB.with_name("app.js").read_text(encoding="utf-8")

    def test_main_page_loads_local_deferred_script_without_inline_code_or_handlers(self):
        source = WEB.read_text(encoding="utf-8")

        scripts = re.findall(r"(?is)<script\b([^>]*)>(.*?)</script\s*>", source)
        self.assertEqual(len(scripts), 1)
        attributes, body = scripts[0]
        self.assertRegex(attributes, r"\bsrc=['\"]?/assets/app\.js['\"]?")
        self.assertRegex(attributes, r"\bdefer\b")
        self.assertEqual(body.strip(), "")
        self.assertNotRegex(source, r"(?i)\bon(?:click|submit|change|input|keydown|keyup|focus|blur)\s*=")

        app_script = WEB.with_name("app.js")
        self.assertTrue(app_script.is_file())
        self.assertIn("initializeAccess();", app_script.read_text(encoding="utf-8"))
        package_config = WEB.parents[3] / "pyproject.toml"
        self.assertIn('"web/*.js"', package_config.read_text(encoding="utf-8"))

    def test_portfolio_filter_listener_targets_have_matching_dom_ids(self):
        source = self.app_source()

        for variable, element_id in (
            ("clearFilters", "portfolio-clear"),
            ("applyFilters", "portfolio-apply"),
        ):
            with self.subTest(element_id=element_id):
                self.assertRegex(
                    source,
                    rf"const {variable}=node\('button'.*?{variable}\.id=['\"]{element_id}['\"]",
                )
                self.assertRegex(
                    source,
                    rf"\$\(['\"]{element_id}['\"]\)\.addEventListener",
                )

    def test_all_direct_id_listener_targets_exist_or_are_created_by_the_shell(self):
        source = self.app_source()
        static_ids = set(re.findall(r"\bid=['\"]([^'\"]+)['\"]", source))
        assigned_ids = set(re.findall(r"\.id=['\"]([^'\"]+)['\"]", source))
        generated_by_factory = {
            "portfolio-lawyer", "portfolio-court", "portfolio-situation",
            "portfolio-monitoring", "portfolio-state", "portfolio-unread-only",
            "portfolio-sort",
        }
        generated_by_shell = {
            "case-open-schedule", "case-add-deadline", "case-export", "case-more",
        }
        known = static_ids | assigned_ids | generated_by_factory | generated_by_shell
        listener_ids = set(re.findall(
            r"\$\(['\"]([^'\"]+)['\"]\)\.addEventListener", source
        ))
        self.assertEqual(listener_ids - known, set())

    def test_api_request_has_a_bounded_timeout_and_user_readable_failure(self):
        source = self.app_source()

        self.assertIn("new AbortController()", source)
        self.assertRegex(source, r"setTimeout\(\(\)=>controller\.abort\(\),15000\)")
        self.assertIn("clearTimeout(timeout)", source)
        self.assertIn("A solicitação excedeu 15 segundos", source)


if __name__ == "__main__":
    unittest.main()
