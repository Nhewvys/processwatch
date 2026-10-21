#!/usr/bin/env python3
"""Diagnóstico isolado do Pydoll; não grava no banco nem na carteira ProcessWatch."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import html.parser
import os
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
TRF4_URL = "https://consulta.trf4.jus.br/trf4/controlador.php?acao=consulta_processual_pesquisa"
REFERENCE_CNJ = "0000000-54.0000.4.04.0000"
MAX_RUNTIME_SECONDS = 120


def detect_challenge(title: str, body: str) -> bool:
    text = f"{title} {body}".casefold()
    return any(
        marker in text
        for marker in (
            "cloudflare",
            "captcha",
            "verify you are human",
            "verifique se você é humano",
            "verificação de segurança",
            "security check",
        )
    )


def valid_trf4_cnj_format(cnj: str) -> bool:
    if not re.fullmatch(r"\d{7}-\d{2}\.\d{4}\.4\.04\.\d{4}", cnj):
        return False
    digits = re.sub(r"\D", "", cnj)
    remainder = 0
    for digit in f"{digits[:7]}{digits[9:]}00":
        remainder = (remainder * 10 + int(digit)) % 97
    return int(digits[7:9]) == 98 - remainder


def classify_error(exc: BaseException) -> str:
    name = type(exc).__name__.casefold()
    message = str(exc).casefold()
    if "timeout" in name or "timeout" in message:
        return "timeout"
    if any(word in name + " " + message for word in ("connection", "network", "dns", "socket")):
        return "network_error"
    if "notfound" in name or "elementnotfound" in name:
        return "element_not_found"
    return "browser_error"


class _TableSummaryParser(html.parser.HTMLParser):
    """Conta estrutura de tabela sem incluir texto de células no resultado."""

    def __init__(self) -> None:
        super().__init__()
        self.tables: list[int] = []
        self._row_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self.tables.append(0)
        elif tag == "tr" and self.tables:
            self.tables[-1] += 1
            self._row_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr" and self._row_depth:
            self._row_depth -= 1
        elif tag == "table" and self.tables:
            self._row_depth = 0


def summarize_tables(source: str) -> dict[str, Any]:
    parser = _TableSummaryParser()
    parser.feed(source)
    return {"table_count": len(parser.tables), "row_counts": parser.tables[:20]}


def browser_main_process_working_set_mb(browser: Any) -> float | None:
    """Best-effort Windows working set do processo principal, sem dependência extra."""
    if os.name != "nt":
        return None
    try:
        manager = browser._browser_process_manager
        pid = manager._process.pid
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)

        class _MemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        psapi.GetProcessMemoryInfo.argtypes = (ctypes.c_void_p, ctypes.POINTER(_MemoryCounters), ctypes.c_ulong)
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
        if not handle:
            return None
        try:
            counters = _MemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
            return round(counters.WorkingSetSize / (1024 * 1024), 1) if ok else None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


class _ControlHandler(BaseHTTPRequestHandler):
    """Servidor efêmero local para testar formulário e redirecionamento sem terceiros."""

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - API padrão do http.server
        if self.path == "/form":
            body = b"""<!doctype html><html><head><title>Controle Pydoll</title></head><body>
              <h1>Controle Pydoll</h1><form action='/submit' method='post'>
              <label for='sample'>Valor de teste</label><input id='sample' name='sample'>
              <button id='submit' type='submit'>Enviar</button></form></body></html>"""
        elif self.path == "/done":
            body = b"""<!doctype html><html><head><title>Resultado local</title></head><body>
              <h1>Formul\xc3\xa1rio recebido</h1><table id='events'><thead><tr><th>Data</th><th>Evento</th></tr></thead>
              <tbody><tr><td>12/10/2026</td><td>Evento de controle A</td></tr>
              <tr><td>11/10/2026</td><td>Evento de controle B</td></tr></tbody></table></body></html>"""
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - API padrão do http.server
        if self.path != "/submit":
            self.send_error(404)
            return
        # Lê o campo apenas para concluir o POST; nunca imprime nem persiste seu valor.
        length = min(int(self.headers.get("Content-Length", "0")), 4096)
        self.rfile.read(length)
        self.send_response(303)
        self.send_header("Location", "/done")
        self.send_header("Content-Length", "0")
        self.end_headers()


def _load_pydoll() -> tuple[Any, Any, Any]:
    try:
        from pydoll import Chrome, ChromiumOptions
        from pydoll.constants import PageLoadState
    except ImportError as exc:
        raise RuntimeError(
            "Pydoll não está instalado neste Python. Consulte docs/fontes-e-condicoes.md antes de usar um conector experimental."
        ) from exc
    return Chrome, ChromiumOptions, PageLoadState


async def run_control() -> dict[str, Any]:
    Chrome, ChromiumOptions, PageLoadState = _load_pydoll()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ControlHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    options = ChromiumOptions()
    options.headless = False
    options.start_timeout = 15
    options.page_load_state = PageLoadState.INTERACTIVE
    started = time.perf_counter()
    browser = Chrome(options=options)
    result: dict[str, Any] = {"mode": "control", "browser_started": False}
    try:
        async with browser:
            tab = await browser.start()
            result["browser_started"] = True
            result["startup_ms"] = round((time.perf_counter() - started) * 1000)
            result["browser_main_working_set_start_mb"] = browser_main_process_working_set_mb(browser)

            # Primeiro contato público sem pesquisa ou autenticação.
            await tab.go_to("https://quotes.toscrape.com/", timeout=25)
            result["public_control"] = {
                "host": urlsplit(await tab.current_url()).hostname,
                "title": await tab.title(),
                "h1_found": bool(await tab.find(tag_name="h1", timeout=8, raise_exc=False)),
            }

            await tab.go_to(f"{base}/form", timeout=10)
            field = await tab.find(id="sample", timeout=5, raise_exc=False)
            submit = await tab.find(id="submit", timeout=5, raise_exc=False)
            if not field or not submit:
                raise RuntimeError("A página local de controle não apresentou o formulário esperado.")
            await field.click()
            await field.type_text("pydoll-control")
            async with tab.expect_navigation(url=f"{base}/done", timeout=10):
                await submit.click()
            landed = urlsplit(await tab.current_url())
            result["form_submit_redirect"] = landed.path == "/done"

            source = await tab.page_source()
            result["table"] = summarize_tables(source)

            absent = await tab.find(id="not-present-in-control-page", timeout=1, raise_exc=False)
            result["missing_element_timeout"] = absent is None
            result["browser_main_working_set_end_mb"] = browser_main_process_working_set_mb(browser)
            result["elapsed_seconds"] = round(time.perf_counter() - started, 2)
            result["result"] = "passed" if all(
                (
                    result["public_control"]["h1_found"],
                    result["form_submit_redirect"],
                    result["table"]["table_count"] == 1,
                    result["table"]["row_counts"] == [3],
                    result["missing_element_timeout"],
                )
            ) else "partial"
    except Exception as exc:  # O diagnóstico deve reportar falha sem esconder a classe.
        result.update({"result": classify_error(exc), "error_type": type(exc).__name__})
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)
        try:
            manager = getattr(browser, "_browser_process_manager", None)
            result["browser_process_stopped"] = bool(manager and manager.exit_code() is not None)
        except Exception:
            result["browser_process_stopped"] = None
    return result


async def run_trf4(cnj: str) -> dict[str, Any]:
    if not valid_trf4_cnj_format(cnj):
        return {"mode": "trf4", "result": "invalid_cnj_format"}
    Chrome, ChromiumOptions, PageLoadState = _load_pydoll()
    options = ChromiumOptions()
    options.headless = False
    options.start_timeout = 15
    options.page_load_state = PageLoadState.INTERACTIVE
    browser = Chrome(options=options)
    result: dict[str, Any] = {"mode": "trf4", "process_number": cnj, "browser_started": False}
    try:
        async with browser:
            tab = await browser.start()
            result["browser_started"] = True
            await tab.go_to(TRF4_URL, timeout=30)
            result["form_page"] = {
                "host": urlsplit(await tab.current_url()).hostname,
                "title": await tab.title(),
            }
            source = await tab.page_source()
            if detect_challenge(result["form_page"]["title"], source):
                result.update({"result": "human_verification", "stage": "form"})
                return result

            # Estes IDs e a opção NU foram observados no formulário oficial em 2026-10-12.
            # A verificação abaixo falha fechada se a estrutura do tribunal mudar.
            form_type = await tab.find(id="selFormaI", timeout=8, raise_exc=False)
            number_field = await tab.find(id="txtValorI", timeout=8, raise_exc=False)
            submit = await tab.find(id="botaoEnviar", timeout=8, raise_exc=False)
            if not form_type or not number_field or not submit:
                result.update({"result": "layout_incompatible", "stage": "form"})
                return result

            await form_type.click()
            number_option = await tab.find(tag_name="option", value="NU", timeout=5, raise_exc=False)
            if not number_option:
                result.update({"result": "layout_incompatible", "stage": "number_search_option"})
                return result
            await number_option.click()
            await number_field.click()
            await number_field.type_text(cnj)
            await submit.click()
            await asyncio.sleep(2)

            current = urlsplit(await tab.current_url())
            source = await tab.page_source()
            if current.hostname != "consulta.trf4.jus.br":
                result.update({"result": "unexpected_redirect", "stage": "search"})
                return result
            title = await tab.title()
            if detect_challenge(title, source):
                result.update({"result": "human_verification", "stage": "search"})
                return result

            # Reutiliza o extrator da extensão, sem importar no backend ou na carteira.
            identity_script = ROOT / "browser-extension" / "processwatch-chrome" / "content" / "trf4_identity.js"
            capture_script = ROOT / "browser-extension" / "processwatch-chrome" / "content" / "trf4.js"
            await tab.execute_script(identity_script.read_text(encoding="utf-8"))
            await tab.execute_script(capture_script.read_text(encoding="utf-8"))
            response = await tab.execute_script(
                "JSON.stringify(globalThis.__processWatchCapture(null))", return_by_value=True
            )
            raw = response.get("result", {}).get("result", {}).get("value")
            capture = json.loads(raw) if isinstance(raw, str) else None
            if not capture or not capture.get("ok"):
                result.update({"result": "extraction_failed", "stage": "result_page"})
                return result
            data = capture.get("data", {})
            movement_dates = [item.get("occurred_at") for item in data.get("movements", []) if item.get("occurred_at")]
            result.update(
                {
                    "result": "process_result",
                    "process_match": re.sub(r"\D", "", data.get("process_number", "")) == re.sub(r"\D", "", cnj),
                    "movement_count": len(data.get("movements", [])),
                    "date_range": [min(movement_dates), max(movement_dates)] if movement_dates else None,
                    "document_metadata_count": sum(len(item.get("documents", [])) for item in data.get("movements", [])),
                    "pagination_status": data.get("pagination_status", "unknown"),
                    "load_more_control_visible": capture.get("diagnostics", {}).get("load_more_control_visible"),
                }
            )
    except Exception as exc:
        result.update({"result": classify_error(exc), "error_type": type(exc).__name__})
    finally:
        try:
            manager = getattr(browser, "_browser_process_manager", None)
            result["browser_process_stopped"] = bool(manager and manager.exit_code() is not None)
        except Exception:
            result["browser_process_stopped"] = None
    return result


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.mode == "control":
        return await run_control()
    return await run_trf4(args.cnj)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("control", "trf4"), default="control")
    parser.add_argument("--cnj", default=REFERENCE_CNJ, help="CNJ público de referência; usado apenas em --mode trf4")
    args = parser.parse_args()
    try:
        result = asyncio.run(asyncio.wait_for(_run(args), timeout=MAX_RUNTIME_SECONDS))
    except TimeoutError:
        result = {"mode": args.mode, "result": "timeout", "limit_seconds": MAX_RUNTIME_SECONDS}
    except Exception as exc:
        result = {"mode": args.mode, "result": classify_error(exc), "error_type": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("result") in ("passed", "human_verification", "process_result") else 1


if __name__ == "__main__":
    raise SystemExit(main())
