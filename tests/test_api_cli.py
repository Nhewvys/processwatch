import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import httpx

from processwatch import api_cli, cli
from processwatch.api import create_app
from processwatch.config import Config
from processwatch.engine import Engine
from processwatch.repository import SQLiteRepository


class NoopProvider:
    def fetch(self, number, court):
        return {"timed_out": False, "hits": {"total": {"value": 0}, "hits": []}}


class SyncASGITransport(httpx.BaseTransport):
    def __init__(self, app):
        self.app = app

    def handle_request(self, request):
        async def dispatch():
            response = await httpx.ASGITransport(app=self.app).handle_async_request(request)
            return response.status_code, response.headers, await response.aread()
        status, headers, content = asyncio.run(dispatch())
        return httpx.Response(status, headers=headers, content=content, request=request)


class ApiCliTests(unittest.TestCase):
    def test_url_config_is_nonsecret_and_rejects_remote_http(self):
        test_root = Path(__file__).parents[1] / ".test-temp"
        test_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_root) as root, patch.dict("os.environ", {"APPDATA": root}):
            api_cli.save_url("http://127.0.0.1:8000")
            self.assertEqual(api_cli.read_url(), "http://127.0.0.1:8000")
            self.assertEqual(json.loads(api_cli.config_path().read_text())["api_url"], "http://127.0.0.1:8000")
            with self.assertRaises(api_cli.CLIError):
                api_cli.read_url("http://example.test:8000")

    def test_config_set_url_remembers_https_server_without_database(self):
        test_root = Path(__file__).parents[1] / ".test-temp"
        test_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_root) as root, \
             patch.dict("os.environ", {"APPDATA": root}), \
             patch("processwatch.cli.SQLiteRepository") as repository:
            self.assertEqual(cli.main(["config", "set-url", "https://processwatch.example.test"]), 0)
            self.assertEqual(api_cli.read_url(), "https://processwatch.example.test")
            self.assertEqual(cli.main(["config", "show"]), 0)
            repository.assert_not_called()
            self.assertEqual(cli.main(["config", "set-url", "http://remote.example.test"]), 1)
            self.assertEqual(api_cli.read_url(), "https://processwatch.example.test")

    def test_login_uses_password_prompt_and_never_opens_repository(self):
        with patch("processwatch.api_cli.read_url", return_value="http://127.0.0.1:8000"), \
             patch("builtins.input", return_value="admin@example.test"), \
             patch("processwatch.api_cli.getpass.getpass", return_value="secret-password"), \
             patch("processwatch.api_cli.request", return_value=(
                 {"active_office_id": "office-a"}, {api_cli.COOKIE_NAME: "session-value"})) as http, \
             patch("processwatch.api_cli.store_session") as store, \
             patch("processwatch.api_cli.save_url"), \
             patch("processwatch.cli.SQLiteRepository") as repository:
            self.assertEqual(cli.main(["login"]), 0)
            self.assertEqual(http.call_args.kwargs["payload"]["password"], "secret-password")
            store.assert_called_once_with("http://127.0.0.1:8000", "session-value")
            repository.assert_not_called()

    def test_office_update_fetches_existing_values_and_sends_origin_via_request(self):
        with patch("processwatch.api_cli.read_url", return_value="http://127.0.0.1:8000"), \
             patch("processwatch.api_cli.load_session", return_value="session-value"), \
             patch("processwatch.api_cli.request", side_effect=[
                 ({"name": "Antigo", "email": "a@example.test", "phone": "123", "address": "Rua A"}, {}),
                 ({"name": "Novo", "email": "a@example.test", "phone": "123", "address": "Rua A"}, {}),
             ]) as http, patch("processwatch.cli.SQLiteRepository") as repository:
            self.assertEqual(cli.main(["office", "update", "--name", "Novo"]), 0)
            self.assertEqual(http.call_args.args[1:3], ("PUT", "/api/office"))
            self.assertEqual(http.call_args.kwargs["payload"]["address"], "Rua A")
            repository.assert_not_called()

    def test_credentials_are_only_displayed_on_creation_or_rotation(self):
        with patch("processwatch.api_cli.read_url", return_value="http://127.0.0.1:8000"), \
             patch("processwatch.api_cli.load_session", return_value="session-value"), \
             patch("processwatch.api_cli.request", return_value=({"items": [{"name": "svc", "token": "must-not-print"}]}, {})) as http, \
             patch("processwatch.api_cli._print_json") as output:
            self.assertEqual(cli.main(["api-key", "list"]), 0)
            self.assertEqual(http.call_args.args[1:3], ("GET", "/api/service-credentials"))
            self.assertNotIn("token", json.dumps(output.call_args.args[0]))

    def test_invalid_permissions_block_create_before_network(self):
        with patch("processwatch.api_cli.read_url", return_value="http://127.0.0.1:8000"), \
             patch("processwatch.api_cli.load_session", return_value="session-value"), \
             patch("processwatch.api_cli.request") as http:
            self.assertEqual(cli.main(["api-key", "create", "--name", "svc",
                                       "--permissions", "processes:write", "--expires-in-days", "30"]), 1)
            http.assert_not_called()

    def test_whoami_and_users_list_use_existing_routes(self):
        with patch("processwatch.api_cli.read_url", return_value="http://127.0.0.1:8000"), \
             patch("processwatch.api_cli.load_session", return_value="session-value"), \
             patch("processwatch.api_cli.request", return_value=({"items": []}, {})) as http:
            self.assertEqual(cli.main(["whoami"]), 0)
            self.assertEqual(http.call_args.args[1:3], ("GET", "/api/auth/me"))
            self.assertEqual(cli.main(["users", "list"]), 0)
            self.assertEqual(http.call_args.args[1:3], ("GET", "/api/admin/users"))

    def test_user_commands_use_scoped_api_and_partial_updates(self):
        with patch("processwatch.api_cli.read_url", return_value="http://127.0.0.1:8000"), \
             patch("processwatch.api_cli.load_session", return_value="session-value"), \
             patch("processwatch.api_cli.request", return_value=({"id": 2}, {})) as http, \
             patch("processwatch.api_cli._print_json"), \
             patch("processwatch.api_cli.getpass.getpass", return_value="temporary-password-123"), \
             patch("builtins.input", return_value="2"):
            self.assertEqual(cli.main(["users", "show", "2"]), 0)
            self.assertEqual(http.call_args.args[1:3], ("GET", "/api/admin/users/2"))
            self.assertEqual(cli.main(["users", "create", "--email", "new@example.test", "--name", "Nova"]), 0)
            self.assertEqual(http.call_args.kwargs["payload"]["full_name"], "Nova")
            self.assertEqual(cli.main(["users", "update", "2", "--name", "Nome novo"]), 0)
            self.assertEqual(http.call_args.kwargs["payload"], {"display_name": "Nome novo"})
            self.assertEqual(cli.main(["users", "deactivate", "2"]), 0)
            self.assertEqual(http.call_args.kwargs["payload"], {"active": False})
            self.assertEqual(cli.main(["users", "activate", "2"]), 0)
            self.assertEqual(http.call_args.kwargs["payload"], {"active": True})
            self.assertEqual(cli.main(["users", "delete", "2"]), 0)
            self.assertEqual(http.call_args.args[1:3], ("DELETE", "/api/admin/users/2"))

    def test_account_commands_prompt_for_password_and_delete_local_session(self):
        with patch("processwatch.api_cli.read_url", return_value="http://127.0.0.1:8000"), \
             patch("processwatch.api_cli.load_session", return_value="session-value"), \
             patch("processwatch.api_cli.request", return_value=({"user": {"email": "me@example.test"}}, {})) as http, \
             patch("processwatch.api_cli._print_json"), \
             patch("processwatch.api_cli.delete_session") as delete, \
             patch("processwatch.api_cli.getpass.getpass", side_effect=["old-password", "new-password-123", "new-password-123", "old-password"]), \
             patch("builtins.input", return_value="me@example.test"):
            self.assertEqual(cli.main(["account", "show"]), 0)
            self.assertEqual(http.call_args.args[1:3], ("GET", "/api/profile"))
            self.assertEqual(cli.main(["account", "update", "--name", "Meu Nome"]), 0)
            self.assertEqual(http.call_args.kwargs["payload"], {"display_name": "Meu Nome"})
            self.assertEqual(cli.main(["account", "change-password"]), 0)
            self.assertEqual(http.call_args.kwargs["payload"]["current_password"], "old-password")
            delete.assert_called_once()
            self.assertEqual(cli.main(["account", "delete"]), 0)
            self.assertEqual(http.call_args.args[1:3], ("DELETE", "/api/profile"))
            self.assertEqual(delete.call_count, 2)

    def test_office_lifecycle_requires_exact_id_confirmation(self):
        with patch("processwatch.api_cli.read_url", return_value="http://127.0.0.1:8000"), \
             patch("processwatch.api_cli.load_session", return_value="session-value"), \
             patch("processwatch.api_cli.request", return_value=({"id": "office-a"}, {})) as http, \
             patch("processwatch.api_cli._print_json"), \
             patch("processwatch.api_cli.delete_session") as delete, \
             patch("builtins.input", side_effect=["wrong", "office-a", "office-a", "office-a"]):
            self.assertEqual(cli.main(["office", "delete"]), 0)
            self.assertEqual(http.call_count, 1)  # only GET; confirmation failed
            for action, method, path in (("deactivate", "POST", "/api/office/deactivate"),
                                         ("activate", "POST", "/api/office/activate"),
                                         ("delete", "DELETE", "/api/office")):
                self.assertEqual(cli.main(["office", action]), 0)
                self.assertEqual(http.call_args.args[1:3], (method, path))
                self.assertEqual(http.call_args.kwargs["payload"], {"office_id": "office-a"})
            delete.assert_called_once()

    def test_logout_revokes_server_session_and_removes_local_credential(self):
        with patch("processwatch.api_cli.read_url", return_value="http://127.0.0.1:8000"), \
             patch("processwatch.api_cli.load_session", return_value="session-value"), \
             patch("processwatch.api_cli.request", return_value=({"status": "ok"}, {})) as http, \
             patch("processwatch.api_cli.delete_session") as delete:
            self.assertEqual(cli.main(["logout"]), 0)
            self.assertEqual(http.call_args.args[1:3], ("POST", "/api/auth/logout"))
            delete.assert_called_once()

    def test_key_create_rotation_and_revocation_use_api(self):
        with patch("processwatch.api_cli.read_url", return_value="http://127.0.0.1:8000"), \
             patch("processwatch.api_cli.load_session", return_value="session-value"), \
             patch("processwatch.api_cli.request", return_value=({"credential": {"id": "key-1"}, "token": "once"}, {})) as http, \
             patch("builtins.input", return_value="s"):
            self.assertEqual(cli.main(["api-key", "create", "--name", "svc", "--permissions",
                                       "processes:read,jobs:read", "--expires-in-days", "30"]), 0)
            self.assertEqual(http.call_args.kwargs["payload"]["permissions"], ["processes:read", "jobs:read"])
            self.assertEqual(cli.main(["api-key", "rotate", "key-1", "--expires-in-days", "10"]), 0)
            self.assertEqual(http.call_args.args[2], "/api/service-credentials/key-1/rotate")
            self.assertEqual(cli.main(["api-key", "revoke", "key-1"]), 0)
            self.assertEqual(http.call_args.args[2], "/api/service-credentials/key-1/revoke")

    def test_http_client_sends_session_cookie_and_csrf_origin(self):
        def handler(received):
            self.assertEqual(received.headers["origin"], "http://127.0.0.1:8000")
            self.assertIn("processwatch_session=session-value", received.headers["cookie"])
            return httpx.Response(200, json={"status": "ok"})

        client_type = httpx.Client
        with patch("httpx.Client", side_effect=lambda **kwargs: client_type(
            transport=httpx.MockTransport(handler), **kwargs)):
            result, _ = api_cli.request("http://127.0.0.1:8000", "PUT", "/api/office",
                                        payload={"name": "Novo"}, session="session-value")
        self.assertEqual(result["status"], "ok")

    def test_admin_commands_against_isolated_api(self):
        root = Path(__file__).parents[1] / ".test-temp"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as temp:
            db = str(Path(temp) / "cli-api.sqlite3")
            repository = SQLiteRepository(db, environment="test")
            try:
                repository.create_office("office-a", "Escritório A")
                repository.create_user("admin@example.test", "safe-admin-password-123",
                                       "office-a", "office_admin")
                app = create_app(repository, Engine(repository, NoopProvider()),
                                 config=Config(db=db, environment="test", auth_mode="accounts"))
                client_type = httpx.Client
                vault = {}
                with patch("httpx.Client", side_effect=lambda **kwargs: client_type(
                    transport=SyncASGITransport(app), **kwargs)), \
                     patch("processwatch.api_cli.read_url", return_value="http://127.0.0.1:8000"), \
                     patch("processwatch.api_cli.save_url"), \
                     patch("processwatch.api_cli.store_session", side_effect=lambda url, token: vault.update({url: token})), \
                     patch("processwatch.api_cli.load_session", side_effect=lambda url: vault.get(url)), \
                     patch("processwatch.api_cli.delete_session", side_effect=lambda url: vault.pop(url, None)), \
                     patch("builtins.input", side_effect=["admin@example.test", "2", "s", "s"]), \
                     patch("processwatch.api_cli.getpass.getpass", return_value="safe-admin-password-123"), \
                     patch("processwatch.api_cli._print_json") as printed:
                    self.assertEqual(cli.main(["login"]), 0)
                    self.assertTrue(vault)
                    self.assertEqual(cli.main(["whoami"]), 0)
                    self.assertEqual(printed.call_args.args[0]["active_office_id"], "office-a")
                    self.assertEqual(cli.main(["office", "update", "--name", "Novo escritório"]), 0)
                    self.assertEqual(repository.office("office-a")["name"], "Novo escritório")
                    self.assertEqual(cli.main(["users", "list"]), 0)
                    self.assertEqual(len(printed.call_args.args[0]["items"]), 1)
                    self.assertEqual(cli.main(["account", "show"]), 0)
                    self.assertEqual(printed.call_args.args[0]["user"]["email"], "admin@example.test")
                    self.assertEqual(cli.main(["account", "update", "--name", "Administrador A"]), 0)
                    self.assertEqual(repository.office_user("office-a", 1)["display_name"], "Administrador A")
                    self.assertEqual(cli.main(["users", "create", "--email", "novo@example.test",
                                                "--name", "Nova Pessoa"]), 0)
                    uid = printed.call_args.args[0]["id"]
                    self.assertEqual(uid, 2)
                    self.assertEqual(cli.main(["users", "show", str(uid)]), 0)
                    self.assertEqual(printed.call_args.args[0]["display_name"], "Nova Pessoa")
                    self.assertEqual(cli.main(["users", "update", str(uid), "--name", "Nome Corrigido"]), 0)
                    self.assertEqual(repository.office_user("office-a", uid)["display_name"], "Nome Corrigido")
                    self.assertEqual(cli.main(["users", "deactivate", str(uid)]), 0)
                    self.assertFalse(repository.office_user("office-a", uid)["active"])
                    self.assertEqual(cli.main(["users", "activate", str(uid)]), 0)
                    self.assertTrue(repository.office_user("office-a", uid)["active"])
                    self.assertEqual(cli.main(["users", "delete", str(uid)]), 0)
                    self.assertIsNone(repository.find_user("novo@example.test"))
                    self.assertEqual(cli.main(["api-key", "create", "--name", "Integração",
                                               "--permissions", "processes:read", "--expires-in-days", "7"]), 0)
                    self.assertIn("token", printed.call_args.args[0])
                    credential_id = printed.call_args.args[0]["credential"]["id"]
                    self.assertEqual(cli.main(["api-key", "list"]), 0)
                    self.assertNotIn("token", json.dumps(printed.call_args.args[0]))
                    self.assertEqual(cli.main(["api-key", "rotate", credential_id,
                                               "--expires-in-days", "10"]), 0)
                    new_id = printed.call_args.args[0]["credential"]["id"]
                    self.assertIn("token", printed.call_args.args[0])
                    self.assertEqual(cli.main(["api-key", "revoke", new_id]), 0)
                    self.assertNotIn("token", json.dumps(printed.call_args.args[0]))
                    self.assertEqual(cli.main(["logout"]), 0)
                    self.assertFalse(vault)
            finally:
                repository.close()


if __name__ == "__main__":
    unittest.main()
