"""Capture only non-sensitive structure from a manual TRF4 browser query.

The user operates the official page and completes any verification themselves.
This helper does not submit the search, solve challenges, save HTML, or persist
cookies. It records table shapes and pagination controls, never movement text.
"""
from __future__ import annotations

import argparse
import json
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


ENTRY_URL = "https://consulta.trf4.jus.br/trf4/controlador.php?acao=consulta_processual_pesquisa"
REFERENCE_NUMBER = "0000000-54.0000.4.04.0000"
OUTPUT_DEFAULT = Path(tempfile.gettempdir()) / "processwatch-trf4-evidencia.json"


def safe_evidence(*, title: str, final_host: str, body_text: str, html: str,
                  tables: list[dict], controls: list[str], fields: list[str],
                  responses: list[dict], expected_number: str = REFERENCE_NUMBER) -> dict:
    """Build an evidence record without page text, URLs, cookies, or query values."""
    expected_digits = re.sub(r"\D", "", expected_number)
    visible_digits = re.sub(r"\D", "", body_text)
    lower_text, lower_html = body_text.casefold(), html.casefold()
    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "Consulta Processual Unificada TRF4",
        "final_host": final_host,
        "page_title": title[:160],
        "reference_number_visible": bool(expected_digits and expected_digits in visible_digits),
        "human_verification_detected": (
            "hdninfracaptcha" in lower_html
            or any(word in lower_text for word in ("captcha", "verificação humana", "aguarde a verificação"))
        ),
        "forms": fields[:40],
        "tables": [{
            "id": str(table.get("id") or "")[:100],
            "class": str(table.get("class") or "")[:160],
            "headers": [str(item)[:100] for item in table.get("headers", [])[:20]],
            "row_count": max(0, int(table.get("row_count") or 0)),
        } for table in tables[:30]],
        "pagination_controls": [str(item)[:100] for item in controls[:30]],
        "responses": [{
            "host": str(item.get("host") or "")[:120],
            "method": str(item.get("method") or "")[:12],
            "status": item.get("status"),
            "resource_type": str(item.get("resource_type") or "")[:24],
        } for item in responses[-100:]],
        "privacy_note": "Sem texto das movimentações, conteúdo de página, URL completa, cookies ou tokens.",
    }


def _capture(page, expected_number: str, responses: list[dict]) -> dict:
    body = page.locator("body").inner_text(timeout=5000)
    tables = page.locator("table").evaluate_all("""els => els.map(e => ({
        id:e.id, class:e.className,
        headers:[...e.querySelectorAll('thead th')].map(h => h.innerText.trim()),
        row_count:e.querySelectorAll('tbody tr').length
    }))""")
    controls = page.locator("button, a, input[type=button], input[type=submit]").evaluate_all("""els => els
        .filter(e => e.offsetParent !== null)
        .map(e => (e.innerText || e.value || e.getAttribute('aria-label') || '').trim())
        .filter(t => /carregar|mais|pr[oó]xima|anterior|movimenta/i.test(t))""")
    fields = page.locator("form").evaluate_all("""forms => forms.flatMap(f =>
        [...f.querySelectorAll('input,select,textarea')].map(e =>
          `${f.id || 'form'}:${e.tagName.toLowerCase()}:${e.name || e.id || 'sem-nome'}`))""")
    final = urlsplit(page.url)
    return safe_evidence(title=page.title(), final_host=final.hostname or "",
                         body_text=body, html=page.content(), tables=tables,
                         controls=controls, fields=fields, responses=responses,
                         expected_number=expected_number)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Captura estrutura não sensível da consulta pública TRF4.")
    parser.add_argument("--cnj", default=REFERENCE_NUMBER, help="CNJ informado pelo operador para verificar a presença na página; não é armazenado no código.")
    parser.add_argument("--output", type=Path, default=OUTPUT_DEFAULT,
                        help="Arquivo JSON de evidência (padrão: pasta temporária do Windows).")
    args = parser.parse_args(argv)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright não está instalado neste ambiente. Nenhum dado foi coletado.")
        return 2

    print("Será aberta uma janela Chrome na consulta oficial do TRF4.")
    print("No portal, faça a consulta manual desejada e carregue as movimentações adicionais. Use --cnj para conferir apenas a presença do número informado pelo operador.")
    print("Complete qualquer verificação apenas no próprio portal. Este script não resolve desafios nem acessa documentos.")
    responses: list[dict] = []
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="chrome", headless=False)
        except Exception as exc:
            print(f"Não foi possível iniciar o Chrome instalado ({type(exc).__name__}). Nenhum dado foi coletado.")
            return 2
        context = browser.new_context()
        page = context.new_page()
        page.on("response", lambda response: responses.append({
            "host": urlsplit(response.url).hostname or "",
            "method": response.request.method,
            "status": response.status,
            "resource_type": response.request.resource_type,
        }))
        try:
            page.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=45000)
            input("Quando a consulta e a paginação estiverem visíveis no Chrome, volte aqui e pressione Enter. Para cancelar, feche o Chrome.")
            record = _capture(page, args.cnj, responses)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("Evidência estrutural gravada:", args.output.resolve())
            print("Processo reconhecido na tela:", "sim" if record["reference_number_visible"] else "não")
            print("Verificação humana detectada:", "sim" if record["human_verification_detected"] else "não")
            print("Tabelas observadas:", len(record["tables"]))
            print("Dados pessoais, HTML, cookies e tokens foram descartados.")
            return 0
        except (EOFError, KeyboardInterrupt):
            print("Captura cancelada; nenhum arquivo de evidência foi gravado.")
            return 1
        except Exception as exc:
            print(f"Captura não concluída ({type(exc).__name__}); nenhum conteúdo da página foi exibido.")
            return 2
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())


