"""Run a dependency-free local challenge-state demo server."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parent

# Public vendor test keys only. These are deliberately confined to this local
# lab and must never be copied into the ProcessWatch runtime configuration.
GOOGLE_TEST_SITEKEY = "6LeIxAcTAAAAAJcZVRqyHh71UMIEGNQ_MXjiZKhI"
GOOGLE_TEST_SECRET = "6LeIxAcTAAAAAGG-vFI1TnRWxMZNFuojJ4WifJWe"
HCAPTCHA_TEST_SITEKEY = "10000000-ffff-ffff-ffff-000000000001"
HCAPTCHA_TEST_SECRET = "0x0000000000000000000000000000000000000000"
HCAPTCHA_TEST_RESPONSE = "10000000-aaaa-bbbb-cccc-000000000001"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlsplit(self.path)
        if path.path == "/":
            body = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.path == "/turnstile-test":
            body = (ROOT / "turnstile_test.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.path == "/turnstile-interactive-test":
            body = (ROOT / "turnstile_interactive_test.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.path in ("/recaptcha-test", "/hcaptcha-test"):
            provider = "recaptcha" if path.path == "/recaptcha-test" else "hcaptcha"
            body = self._provider_test_page(provider)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.path == "/provider-success":
            provider = parse_qs(path.query).get("provider", [""])[0]
            if provider not in ("recaptcha", "hcaptcha"):
                self.send_error(400)
                return
            body = ("<!doctype html><html lang='pt-BR'><meta charset='utf-8'>"
                    "<title>Formulário aprovado no teste</title><h1 id='accepted'>"
                    "Formulário aceito após validação oficial de teste: " + provider +
                    "</h1></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.path == "/api/demo":
            mode = parse_qs(path.query).get("mode", [""])[0]
            responses = {
                "valid": (200, {"status": "accepted", "message": "Validação local de formulário concluída."}),
                "network": (503, {"status": "simulated_network_failure", "message": "Falha de rede simulada."}),
                "expired": (410, {"status": "simulated_expiration", "message": "Sessão de demonstração expirada."}),
                "challenge": (423, {"status": "awaiting_access", "message": "Desafio simulado: trabalhador liberado; sem retry."}),
            }
            status, payload = responses.get(mode, (400, {"status": "invalid_mode"}))
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/form-complete":
            self._json_response(200, {"status": "form_accepted", "navigation_continued": True})
            return
        if path in ("/api/recaptcha-test-verify", "/api/hcaptcha-test-verify"):
            provider = "recaptcha" if path.endswith("recaptcha-test-verify") else "hcaptcha"
            self._verify_vendor_test(provider)
            return
        if path != "/api/turnstile-test-verify":
            self.send_error(404)
            return
        length = min(int(self.headers.get("Content-Length", "0")), 4096)
        payload = json.loads(self.rfile.read(length) or b"{}")
        token = payload.get("token", "")
        # Only Cloudflare's published dummy token and test secret are used here.
        if token != "XXXX.DUMMY.TOKEN.XXXX":
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"success":false,"error":"not_a_documented_dummy_token"}')
            return
        request = Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=("secret=1x0000000000000000000000000000000AA&response=" + token).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=8) as response:
                result = json.loads(response.read(16384))
                status = 200 if result.get("success") is True else 502
        except (URLError, HTTPError, TimeoutError, json.JSONDecodeError):
            result, status = {"success": False, "error": "test_siteverify_unavailable"}, 503
        # Never echo or log the submitted token or test secret.
        safe = {"success": bool(result.get("success")), "hostname": result.get("hostname"),
                "error-codes": result.get("error-codes", []), "test_mode": True}
        body = json.dumps(safe).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _provider_test_page(self, provider: str) -> bytes:
        if provider == "recaptcha":
            sitekey = GOOGLE_TEST_SITEKEY
            script = "https://www.google.com/recaptcha/api.js?onload=renderTest&render=explicit"
            render = "window.testState.widgetId=grecaptcha.render('widget', {sitekey: SITEKEY, size:'invisible', callback: submitToken, 'expired-callback': expired, 'error-callback': failed});"
            execute = "grecaptcha.execute(window.testState.widgetId);"
            api_ready = "window.grecaptcha"
            title = "Google reCAPTCHA v2 — chave oficial de teste"
        else:
            sitekey = HCAPTCHA_TEST_SITEKEY
            script = "https://js.hcaptcha.com/1/api.js?onload=renderTest&render=explicit"
            render = "window.testState.widgetId=hcaptcha.render('widget', {sitekey: SITEKEY, size:'invisible', callback: submitToken, 'expired-callback': expired, 'error-callback': failed});"
            execute = "hcaptcha.execute(window.testState.widgetId);"
            api_ready = "window.hcaptcha"
            title = "hCaptcha — chave oficial de teste"
        html = f'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><script src="{script}" async defer></script></head><body>
<h1>{title}</h1><p>Laboratório local. Chave de teste: aprovação automática, sem desafio visual.</p>
<form id="test-form"><div id="widget"></div><button id="submit" type="submit">Enviar formulário</button></form>
<output id="status">carregando_widget</output>
<script>
const SITEKEY={json.dumps(sitekey)}; window.testState={{widget:false,callback:false,result:null,error:null,expired:false}};
function renderTest(){{window.testState.widget=true; {render} document.querySelector('#status').textContent='widget_renderizado';}}
function expired(){{window.testState.expired=true;document.querySelector('#status').textContent='token_expirado';}}
function failed(){{window.testState.error='provider_error';document.querySelector('#status').textContent='erro_do_provedor';}}
async function submitToken(token){{window.testState.callback=true;document.querySelector('#status').textContent='token_recebido_validando_no_servidor';
 try{{const response=await fetch('/api/{provider}-test-verify',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{token}})}});
 window.testState.result=await response.json();window.testState.result.http_status=response.status;
 document.querySelector('#status').textContent=response.ok?'validado_no_servidor':'rejeitado_no_servidor';
 if(window.testState.result.success===true){{const done=await fetch('/api/form-complete',{{method:'POST'}});window.testState.navigation_continued=done.ok;}}
 }}catch(_e){{window.testState.error='network_error';document.querySelector('#status').textContent='falha_de_rede';}}}}
document.querySelector('#test-form').addEventListener('submit',async event=>{{event.preventDefault(); if(!window.testState.callback||window.testState.result?.success!==true){{document.querySelector('#status').textContent='token_ausente_ou_nao_validado';return;}} const response=await fetch('/api/form-complete',{{method:'POST'}});if(response.ok)window.location.assign('/provider-success?provider={provider}');}});
window.testApiReady=()=>Boolean({api_ready});
window.requestTestToken=()=>{{if(!window.testState.widget)throw new Error('widget_not_ready'); {execute}}};
</script></body></html>'''
        return html.encode("utf-8")

    def _verify_vendor_test(self, provider: str) -> None:
        length = min(int(self.headers.get("Content-Length", "0")), 4096)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        token = payload.get("token") if isinstance(payload, dict) else None
        if provider == "recaptcha":
            endpoint = "https://www.google.com/recaptcha/api/siteverify"
            fields = {"secret": GOOGLE_TEST_SECRET, "response": token or ""}
        else:
            endpoint = "https://api.hcaptcha.com/siteverify"
            fields = {"secret": HCAPTCHA_TEST_SECRET, "response": token or ""}
        request = Request(endpoint, data=urlencode(fields).encode("ascii"),
                          headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        try:
            with urlopen(request, timeout=10) as response:
                result = json.loads(response.read(16384))
                status = response.status
            safe = {"success": result.get("success") is True,
                    "error_codes": result.get("error-codes", []),
                    "test_mode": True, "provider": provider}
            if not safe["success"]:
                status = 403
        except HTTPError as exc:
            # Do not expose response bodies, which could contain vendor diagnostics.
            status = 502
            safe = {"success": False, "error_codes": ["siteverify_http_error"],
                    "provider_status": exc.code, "test_mode": True, "provider": provider}
        except (URLError, TimeoutError, json.JSONDecodeError, OSError):
            status = 503
            safe = {"success": False, "error_codes": ["siteverify_unavailable"],
                    "test_mode": True, "provider": provider}
        body = json.dumps(safe).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_response(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        # Do not log request paths/query values in this diagnostic lab.
        return


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    print("Laboratório local em http://127.0.0.1:8765 — Ctrl+C encerra.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
