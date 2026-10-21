"""Public DJEN communications client; no tribunal-only credentials or endpoints."""
import asyncio
import html
import json
import hashlib
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, urlsplit

import httpx

from ..cnj import normalize
from ..domain import Advogado, ComunicacaoJudicial
from ..participants import mentions
from .contracts import SourceCapabilities


URL = "https://comunicaapi.pje.jus.br/api/v1/comunicacao"


class DjenError(RuntimeError):
    pass


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = 0

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
            return
        if tag in ("br", "p", "div", "li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1


def plain_text(value):
    if not value:
        return None
    parser = _PlainText()
    parser.feed(str(value))
    return html.unescape("".join(parser.parts)).strip()


def pick(source, *names):
    for name in names:
        if source.get(name) is not None:
            return source[name]
    return None


def _names(value):
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    names = [pick(item, "nome", "name") if isinstance(item, dict) else item for item in value]
    return [str(item) for item in names if item]


def _records(value, *, polo_field_path=None):
    if value is None:
        return []
    if not isinstance(value, list):
        value = [value]
    records = []
    for item in value:
        if isinstance(item, str):
            records.append({"nome": item})
        elif isinstance(item, dict):
            outer = item
            if isinstance(item.get("advogado"), dict):
                # Keep outer relation metadata while taking identity/OAB from the
                # nested lawyer record. The untouched original payload is stored too.
                item = {**item, **item["advogado"]}
            name = pick(item, "nome", "name", "nomeParte", "nome_parte", "nomeAdvogado", "nome_advogado")
            if name:
                role_fields = ("papel", "tipo", "tipoParte", "tipo_parte", "qualificacao", "qualificação")
                role_field = next((field for field in role_fields if item.get(field) is not None), None)
                relation_fields = ("parteRepresentada", "parte_representada", "representado",
                                   "representada", "idParteRepresentada", "id_parte_representada",
                                   "vinculo", "vínculo", "relacao", "relação")
                relationships = [{"source_field": field, "value": outer[field],
                                  "status": "source_explicit_relation"}
                                 for field in relation_fields if outer.get(field) not in (None, "", [], {})]
                cpf = pick(item, "cpf", "numeroCpf", "numero_cpf")
                cnpj = pick(item, "cnpj", "numeroCnpj", "numero_cnpj")
                document_number = re.sub(r"\D", "", str(cpf or cnpj or "")) or None
                if document_number and len(document_number) != (11 if cpf else 14):
                    document_number = None
                if not document_number:
                    found = re.search(r"\b(CPF|CNPJ)\s*:?\s*([\d./-]{11,20})", str(name), re.I)
                    if found:
                        document_type = found.group(1).casefold()
                        document_number = re.sub(r"\D", "", found.group(2))
                        expected = 11 if document_type == "cpf" else 14
                        if len(document_number) == expected:
                            cpf = document_number if document_type == "cpf" else cpf
                            cnpj = document_number if document_type == "cnpj" else cnpj
                        else:
                            document_number = None
                else:
                    document_type = "cpf" if cpf else "cnpj"
                records.append({"nome": str(name), "papel": pick(item, "papel", "tipo", "tipoParte", "tipo_parte"),
                                "tipo": pick(item, "tipo", "tipoParte", "tipo_parte"),
                                "polo": pick(item, "polo"), "oab": pick(item, "numero_oab", "numeroOab", "oab"),
                                "uf": pick(item, "uf_oab", "ufOab", "uf"),
                                "polo_source_field": polo_field_path if "polo" in item else None,
                                "role_source_field": role_field,
                                "role_code_original": pick(item, *role_fields),
                                "source_record_id": pick(outer, "id", "idParte", "id_parte", "idPessoa", "id_pessoa"),
                                "source_identity_id": (pick(outer, "advogado_id", "advogadoId") or
                                    pick(outer.get("advogado", {}) if isinstance(outer.get("advogado"), dict) else {},
                                         "id", "idParte", "idPessoa") or
                                    pick(outer, "idParte", "id_parte", "idPessoa", "id_pessoa")),
                                "relationships": relationships,
                                "cpf": str(cpf) if cpf else None, "cnpj": str(cnpj) if cnpj else None,
                                "document_type": "cpf" if cpf else "cnpj" if cnpj else None,
                                "document_number": document_number})
    return records


def safe_url(value):
    if not value:
        return None
    parsed = urlsplit(str(value))
    return str(value) if parsed.scheme == "https" and parsed.netloc else None


def parse_communication(raw, expected_number):
    if not isinstance(raw, dict):
        raise DjenError("DJEN retornou comunicação inválida.")
    candidate = pick(raw, "numero_processo", "numeroProcesso", "numeroprocessocommascara")
    if not candidate:
        raise DjenError("DJEN retornou comunicação sem número processual.")
    try:
        number = normalize(str(candidate))
    except ValueError:
        raise DjenError("DJEN retornou número processual inválido.") from None
    if number != expected_number:
        raise DjenError("DJEN retornou comunicação de outro processo.")
    lawyer_records = _records(pick(raw, "destinatarioadvogados", "advogados", "advogadosDestinatarios", "advogados_destinatarios"))
    lawyers = [Advogado(nome=item["nome"], oab=str(item["oab"]) if item["oab"] else None, uf=item["uf"])
               for item in lawyer_records]
    parties = _records(pick(raw, "partes", "partesProcessuais", "partes_processuais"),
                       polo_field_path="partes[].polo")
    recipient_raw = pick(raw, "destinatarios", "nomesDestinatarios", "nomeDestinatario", "nome_destinatario")
    recipient_records = _records(recipient_raw, polo_field_path="destinatarios[].polo")
    recipients = _names(recipient_raw)
    original_id = pick(raw, "id", "hash")
    source_id = str(original_id) if original_id is not None else "content:" + hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    text = pick(raw, "texto", "texto_comunicacao", "textoComunicacao")
    source_url = safe_url(pick(raw, "link", "url", "linkComunicacao", "link_comunicacao"))
    if not source_url and raw.get("hash"):
        source_url = URL + "/" + quote(str(raw["hash"]), safe="") + "/certidao"
    hearing = pick(raw, "dataAudiencia", "data_audiencia")
    hearing_status = pick(raw, "situacaoAudiencia", "situacao_audiencia")
    allowed_statuses = {"designada", "realizada", "cancelada", "redesignada"}
    return ComunicacaoJudicial(
        source_id=source_id, number=number, hash=str(raw["hash"]) if raw.get("hash") is not None else None,
        tribunal=pick(raw, "siglaTribunal", "sigla_tribunal", "tribunal"),
        orgao=pick(raw, "nomeOrgao", "nome_orgao", "orgao"),
        tipo=pick(raw, "tipoComunicacao", "tipo_comunicacao"),
        documento_tipo=pick(raw, "tipoDocumento", "tipo_documento"),
        available_at=pick(raw, "dataDisponibilizacao", "data_disponibilizacao"),
        published_at=pick(raw, "dataPublicacao", "data_publicacao"),
        text_original=text, text_plain=plain_text(text),
        recipients=recipients, lawyers=lawyers, parties=parties,
        participants=mentions(number, source_id, parties, lawyer_records, recipient_records),
        meio=pick(raw, "meio", "meioPublicacao", "meio_publicacao"),
        url=source_url,
        status=pick(raw, "status", "situacao"),
        cancellation=pick(raw, "motivo_cancelamento", "motivoCancelamento"),
        hearing_at=str(hearing) if hearing else None,
        hearing_status=str(hearing_status).lower() if hearing_status and str(hearing_status).lower() in allowed_statuses else None,
        hearing_evidence="campo estruturado de data de audiência no DJEN" if hearing else None,
        collected_at=datetime.now(timezone.utc).isoformat(),
        original=raw,
    )


@dataclass(frozen=True)
class DjenFetch:
    items: list[ComunicacaoJudicial]
    period_start: str | None
    period_end: str | None
    complete: bool


@dataclass(frozen=True)
class OabFetch:
    items: list[ComunicacaoJudicial]
    pages: int
    reported: int
    period_start: str
    period_end: str


class DjenClient:
    source_id = "DJEN"
    capabilities = SourceCapabilities(process_lookup=True, communications=True,
                                      participant_mentions=True, discovery_oab=True, pagination=True,
                                      incremental=True)

    def __init__(self, client=None, *, timeout=20, max_pages=200, min_interval=1.0,
                 sleep=asyncio.sleep, clock=time.monotonic):
        self.client = client
        self.timeout = timeout
        self.max_pages = max_pages
        self.min_interval = min_interval
        self.sleep = sleep
        self.clock = clock
        self.last_request = None

    async def _wait_for_slot(self):
        if self.last_request is not None:
            delay = self.min_interval - (self.clock() - self.last_request)
            if delay > 0:
                await self.sleep(delay)
        self.last_request = self.clock()

    async def _get(self, client, params):
        for attempt in range(3):
            try:
                await self._wait_for_slot()
                response = await client.get(URL, params=params, headers={"Accept": "application/json"}, timeout=self.timeout)
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt == 2:
                    raise DjenError("DJEN indisponível por falha de rede ou timeout.") from None
                await self.sleep(min(2 ** attempt, 8))
                continue
            if response.status_code == 403:
                raise DjenError("DJEN recusou a consulta (HTTP 403); cobertura não confirmada.")
            if response.status_code in (429,) or 500 <= response.status_code <= 599:
                if attempt == 2:
                    raise DjenError(f"DJEN indisponível (HTTP {response.status_code}).")
                retry_after = response.headers.get("Retry-After", "")
                try:
                    delay = min(60, max(0, float(retry_after)))
                except ValueError:
                    delay = 60 if response.status_code == 429 else min(2 ** attempt, 8)
                await self.sleep(delay)
                continue
            if response.status_code != 200:
                raise DjenError(f"DJEN recusou a consulta (HTTP {response.status_code}).")
            try:
                data = response.json()
            except ValueError:
                raise DjenError("DJEN retornou JSON inválido.") from None
            if response.headers.get("X-RateLimit-Remaining") == "0":
                reset = response.headers.get("X-RateLimit-Reset", "")
                try:
                    parsed = float(reset)
                    delay = parsed - time.time() if parsed > 1_000_000_000 else parsed
                    await self.sleep(min(30, max(0, delay)))
                except ValueError:
                    await self.sleep(1)
            return data
        raise DjenError("DJEN indisponível.")

    async def fetch(self, number, start: date | None = None, end: date | None = None):
        number = normalize(number)
        if start and end and end < start:
            raise ValueError("Data final anterior à inicial.")
        client = self.client or httpx.AsyncClient(follow_redirects=False)
        owns_client = self.client is None
        items = []
        seen_pages = set()
        try:
            for page in range(1, self.max_pages + 1):
                params = {"numeroProcesso": number, "pagina": page, "itensPorPagina": 100}
                if start:
                    params["dataDisponibilizacaoInicio"] = start.isoformat()
                if end:
                    params["dataDisponibilizacaoFim"] = end.isoformat()
                data = await self._get(client, params)
                if not isinstance(data, dict) or not isinstance(data.get("items"), list) or not isinstance(data.get("count"), int):
                    raise DjenError("DJEN retornou formato de paginação inválido.")
                batch = data["items"]
                signature = json.dumps(batch, sort_keys=True, ensure_ascii=False)
                if batch and signature in seen_pages:
                    raise DjenError("DJEN repetiu uma página; cobertura não confirmada.")
                seen_pages.add(signature)
                items.extend(parse_communication(raw, number) for raw in batch)
                if len(items) >= data["count"] and len(batch) < 100:
                    return DjenFetch(items, start.isoformat() if start else None,
                                     end.isoformat() if end else None, bool(start and end))
                if not batch:
                    if len(items) >= data["count"]:
                        return DjenFetch(items, start.isoformat() if start else None,
                                         end.isoformat() if end else None, bool(start and end))
                    raise DjenError("DJEN retornou página vazia antes de completar os resultados.")
            raise DjenError("DJEN excedeu o limite local de páginas; cobertura incompleta.")
        finally:
            if owns_client:
                await client.aclose()

    async def fetch_by_oab(self, oab: str, uf: str, start: date, end: date) -> OabFetch:
        """Discover only publications with an exact structured OAB/UF match."""
        if not oab or len(oab) > 12 or not oab.isalnum() or len(uf) != 2 or not uf.isalpha():
            raise ValueError("OAB/UF inválidas.")
        uf = uf.upper()
        if end < start or (end - start).days > 30:
            raise ValueError("Informe um período de até 31 dias para descoberta OAB.")
        client = self.client or httpx.AsyncClient(follow_redirects=False)
        owns_client = self.client is None
        items = []
        seen_pages = set()
        received = 0
        try:
            for page in range(1, self.max_pages + 1):
                params = {"numeroOab": oab, "ufOab": uf,
                          "dataDisponibilizacaoInicio": start.isoformat(),
                          "dataDisponibilizacaoFim": end.isoformat(),
                          "pagina": page, "itensPorPagina": 100}
                data = await self._get(client, params)
                if not isinstance(data, dict) or not isinstance(data.get("items"), list) or not isinstance(data.get("count"), int):
                    raise DjenError("DJEN retornou paginação OAB inválida.")
                if data["count"] >= 10000:
                    raise DjenError("DJEN excedeu o limite documentado de 10.000 resultados; divida o período.")
                batch = data["items"]
                signature = json.dumps(batch, sort_keys=True, ensure_ascii=False)
                if batch and signature in seen_pages:
                    raise DjenError("DJEN repetiu página OAB; cobertura não confirmada.")
                seen_pages.add(signature)
                received += len(batch)
                for raw in batch:
                    lawyers = _records(pick(raw, "destinatarioadvogados", "advogados", "advogadosDestinatarios", "advogados_destinatarios"))
                    if not any(str(lawyer.get("oab") or "").upper().lstrip("0") == oab.upper().lstrip("0")
                               and str(lawyer.get("uf") or "").upper() == uf for lawyer in lawyers):
                        continue
                    candidate = pick(raw, "numero_processo", "numeroProcesso", "numeroprocessocommascara")
                    try:
                        number = normalize(str(candidate))
                    except (ValueError, TypeError):
                        continue
                    items.append(parse_communication(raw, number))
                if received >= data["count"] and len(batch) < 100:
                    return OabFetch(items, page, data["count"], start.isoformat(), end.isoformat())
                if not batch:
                    if received >= data["count"]:
                        return OabFetch(items, page, data["count"], start.isoformat(), end.isoformat())
                    raise DjenError("DJEN retornou página OAB vazia antes de completar os resultados.")
            raise DjenError("Limite local de páginas OAB atingido; cobertura incompleta.")
        finally:
            if owns_client:
                await client.aclose()
