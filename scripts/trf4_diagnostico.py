"""Diagnóstico local e não invasivo da consulta pública oficial do TRF4.

Faz somente DNS e GET das páginas públicas de serviço/formulário. Não envia
CNJ, CPF, cookies, credenciais nem tenta resolver desafios de acesso.
"""
from __future__ import annotations

import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone


OFFICIAL_URLS = (
    "https://www.trf4.jus.br/trf4/controlador.php?acao=pagina_visualizar&id_pagina=3929",
    "https://consulta.trf4.jus.br/trf4/controlador.php?acao=consulta_processual_pesquisa",
)
USER_AGENT = "ProcessWatch local connectivity diagnostic/1.0"
MAX_BYTES = 1_000_000


@dataclass(frozen=True)
class ProbeResult:
    host: str
    state: str
    addresses: tuple[str, ...] = ()
    status: int | None = None
    final_host: str | None = None
    tls_verified: bool | None = None
    redirected: bool = False


def classify_response(status: int, body: bytes) -> str:
    """Classifica resposta sem expor texto, cookies ou dados do portal."""
    if status in (401, 403):
        return "acesso_restrito"
    if status == 429:
        return "limite_de_requisicoes"
    if status >= 500:
        return "portal_indisponivel"
    text = body.decode("iso-8859-1", "ignore").casefold()
    if "hdninfracaptcha" in text or "aguarde a verifica" in text and "captcha" in text:
        return "verificacao_humana_detectada"
    if "consulta pública está desativada" in text or "consulta publica esta desativada" in text:
        return "rota_antiga_desativada"
    if (status == 200 and "consulta processual unificada" in text
            and "selforma" in text and "txtvalor" in text):
        return "formulario_publico_disponivel_sem_consulta_de_processo"
    if status == 200:
        return "resposta_http_valida_sem_formulario_confirmado"
    return "resposta_http_nao_confirmada"


def _dns(host: str) -> tuple[str, ...]:
    rows = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return tuple(sorted({row[4][0] for row in rows}))


def probe(url: str, timeout: float = 15.0) -> ProbeResult:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname or ""
    try:
        addresses = _dns(host)
    except socket.gaierror:
        return ProbeResult(host, "falha_dns")
    if not addresses:
        return ProbeResult(host, "falha_dns")

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    try:
        # Contexto padrão mantém a validação normal de certificados TLS.
        with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
            body = response.read(MAX_BYTES)
            final_host = urllib.parse.urlsplit(response.geturl()).hostname
            return ProbeResult(host, classify_response(response.status, body), addresses,
                               response.status, final_host, True, final_host != host)
    except urllib.error.HTTPError as response:
        # Não lê nem imprime o corpo de erros HTTP, que poderia conter dados da consulta.
        final_host = urllib.parse.urlsplit(response.geturl()).hostname
        return ProbeResult(host, classify_response(response.code, b""), addresses,
                           response.code, final_host, True, final_host != host)
    except ssl.SSLCertVerificationError:
        return ProbeResult(host, "falha_validacao_tls", addresses, tls_verified=False)
    except TimeoutError:
        return ProbeResult(host, "timeout", addresses)
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError):
            return ProbeResult(host, "falha_validacao_tls", addresses, tls_verified=False)
        if isinstance(reason, socket.gaierror):
            return ProbeResult(host, "falha_dns")
        if isinstance(reason, TimeoutError):
            return ProbeResult(host, "timeout", addresses)
        return ProbeResult(host, "falha_conexao", addresses)
    except OSError:
        return ProbeResult(host, "falha_conexao", addresses)


def main() -> int:
    print("Diagnóstico da consulta pública oficial do TRF4 (somente leitura)")
    print("Nenhum número de processo, documento ou dado pessoal será enviado.")
    print("Horário UTC:", datetime.now(timezone.utc).isoformat())
    results = [probe(url) for url in OFFICIAL_URLS]
    for result in results:
        tls = "validado" if result.tls_verified else ("não estabelecido" if result.tls_verified is None else "inválido")
        status = str(result.status) if result.status is not None else "sem HTTP"
        final = result.final_host or "sem resposta final"
        redirect = "; houve redirecionamento" if result.redirected else ""
        ips = ", ".join(result.addresses) if result.addresses else "não resolvido"
        print(f"{result.host}: {result.state}; DNS [{ips}]; HTTP {status}; destino {final}; TLS {tls}{redirect}")
    if any(result.state == "verificacao_humana_detectada" for result in results):
        print("Resultado: o portal respondeu, mas apresentou verificação humana; nenhuma consulta por processo foi executada.")
    elif all(result.status == 200 for result in results):
        print("Resultado: páginas oficiais acessíveis. Isso não comprova extração processual nem autorização de automação.")
    else:
        print("Resultado: houve falha de rede/acesso; consulte o estado de cada endereço acima.")
    print("Este diagnóstico não contorna CAPTCHA, autenticação, bloqueios ou limites do tribunal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
