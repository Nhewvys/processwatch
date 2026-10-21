"""Read-only connector for the TRF1 PJe public consultation.

The connector follows the page's ordinary browser flow, including its own
JavaScript verification. It never solves, skips, or posts around CAPTCHA.
Only normalized case data and source references are returned; HTML and document
contents are not persisted.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..cnj import format_number, normalize
from ..participants import extract_qualified_name, normalize_procedural_function, professional_kind, safe_participant_name
from ..providers.contracts import (
    CollectionRequest, ConnectorError, ConnectorEvidence, ConnectorResult,
    ConnectorState, SourceCapabilities,
)
from ..source_matrix import get_source


SOURCE_IDS = frozenset({"TRF1-PJE-1G", "TRF1-PJE-JE"})
SOURCE_LABEL = "PJe/TRF1"
_NUMBER_INPUT = "fPP:numProcesso-inputNumeroProcessoDecoration:numProcesso-inputNumeroProcesso"
_DETAIL_PATH = "/consultapublica/ConsultaPublica/DetalheProcessoConsultaPublica/listView.seam"
_LIST_LINK = re.compile(r"openPopUp\([^,]+,\s*['\"]([^'\"]*DetalheProcessoConsultaPublica/listView\.seam\?ca=[^'\"]+)['\"]")
_CPF = re.compile(r"\s*[-–]?\s*CPF\s*:?\s*[\d./-]+", re.I)
_CNPJ = re.compile(r"\s*[-–]?\s*CNPJ\s*:?\s*[\d./-]+", re.I)
_OAB = re.compile(r"\bOAB\s*[/:-]?\s*([A-Z]{2})\s*[/:-]?\s*(\d[\d./-]*)", re.I)
_CPF_VALUE = re.compile(r"\bCPF\s*:?\s*([\d./-]{11,18})", re.I)
_CNPJ_VALUE = re.compile(r"\bCNPJ\s*:?\s*([\d./-]{14,20})", re.I)
_EVENT_DATE = re.compile(r"^(\d{2}/\d{2}/\d{4})(?:\s+(\d{2}:\d{2}(?::\d{2})?))?\s*[-–]\s*(.*)$", re.S)


class _Node:
    def __init__(self, tag: str, attrs: dict[str, str], parent: "_Node | None" = None):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[_Node | str] = []

    def text(self) -> str:
        value = " ".join(child.text() if isinstance(child, _Node) else child for child in self.children)
        return " ".join(value.split())

    def find_all(self, tag: str | None = None):
        found = []
        for child in self.children:
            if not isinstance(child, _Node):
                continue
            if tag is None or child.tag == tag:
                found.append(child)
            found.extend(child.find_all(tag))
        return found

    def closest(self, tag: str):
        node: _Node | None = self
        while node and node.tag != tag:
            node = node.parent
        return node


class _Tree(HTMLParser):
    _VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self._VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if data:
            self.stack[-1].children.append(data)


def _find_id(root: _Node, value: str) -> _Node | None:
    return next((node for node in root.find_all() if node.attrs.get("id") == value), None)


def _visible_text(node: _Node) -> str:
    """Return rendered text while excluding scripts and styles from JSF cells."""
    pieces = []
    for child in node.children:
        if isinstance(child, str):
            pieces.append(child)
        elif child.tag not in ("script", "style"):
            pieces.append(_visible_text(child))
    return " ".join(" ".join(pieces).split())


def _public_document_link_candidate(value: str | None) -> bool:
    """Recognize a portal-local HTTPS link without claiming it is publicly accessible."""
    if not value:
        return False
    try:
        parsed = urlsplit(_resolve_document_href(value) or "")
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname == "pje1g-consultapublica.trf1.jus.br"


def _resolve_document_href(value: str | None) -> str | None:
    """Resolve an actual portal anchor href; never synthesizes a URL from JS onclick."""
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            candidate = value
        elif value.startswith(("/", "?", "./", "../")):
            candidate = urljoin("https://pje1g-consultapublica.trf1.jus.br/consultapublica/ConsultaPublica/", value)
        else:
            return None
        resolved = urlsplit(candidate)
    except ValueError:
        return None
    if (resolved.scheme == "https" and resolved.hostname == "pje1g-consultapublica.trf1.jus.br"
            and not resolved.username and not resolved.password):
        return resolved.geturl()
    return None


def _participant_identity(text: str, polo: str, status: str | None = None) -> dict[str, Any] | None:
    """Parse the PJe participant cell without discarding CPF/CNPJ or explicit role."""
    text = " ".join(str(text or "").split())
    original = text
    cpf_match, cnpj_match = _CPF_VALUE.search(text), _CNPJ_VALUE.search(text)
    cpf = re.sub(r"\D", "", cpf_match.group(1)) if cpf_match else None
    cnpj = re.sub(r"\D", "", cnpj_match.group(1)) if cnpj_match else None
    # Keep the raw cell as evidence, but classify only a full role label from
    # the shared catalog. The source tables can express qualifications either
    # parenthetically or as "PAPEL: NOME"/"NOME - PAPEL".
    text, role = extract_qualified_name(text)
    identity_type = "pessoa_juridica_documento_presente" if cnpj else (
        "pessoa_fisica_documento_presente" if cpf else "unconfirmed")
    cleaned = _CPF.sub("", _CNPJ.sub("", text)).strip(" -–")
    oab_match = _OAB.search(original)
    oab = oab_match.group(2) if oab_match else None
    uf = oab_match.group(1).upper() if oab_match else None
    name = _OAB.sub("", cleaned).strip(" -–") if oab_match else cleaned
    function = normalize_procedural_function(SOURCE_LABEL, role)
    kind = _participant_kind(role, polo)
    kind = professional_kind({"kind": kind, "oab": oab}, name) or kind
    name = safe_participant_name(name, role)
    if kind in {"advogado", "procurador", "defensor"}:
        function = normalize_procedural_function(SOURCE_LABEL, kind)
    if not name or name.casefold() == "participante":
        return None
    return {"name": name, "name_original": name,
            "source_text_original": _redact_identifiers(original), "kind": kind,
            "role": role, "role_original": role, **function,
            "polo": polo if kind == "parte" else None,
            "position_scope": "recurso" if function.get("function_scope") == "recurso" else
                             "incidente" if function.get("function_scope") == "incidente" else "processo consultado",
            "oab": oab, "uf": uf, "cpf": cpf, "cnpj": cnpj,
            "document_number": cpf or cnpj,
            "document_type": "cpf" if cpf else "cnpj" if cnpj else None,
            "status": status, "source": SOURCE_LABEL,
            "evidence": {"kind": "capa processual pública", "table_pole": polo,
                         "source_text_original": _redact_identifiers(original)},
            "identity_type": identity_type, "identity_status": "confirmed_document" if cpf or cnpj else
                "confirmed_oab_uf" if oab and uf else "source_record_unresolved"}


def _document_source_id(href: str | None, onclick: str | None = None) -> str | None:
    """Read a document identifier from the portal's own link without retaining it.

    Movement-row placement supplies the document-to-movement association. This
    helper only accepts the known `idProcessoDoc` parameter from a relative
    portal URL, an HTTPS URL on the public TRF1 host, or the portal's
    `openPopUp(..., '?idProcessoDoc=...')` handler. It never treats the
    resulting reference as proof that the document can be opened publicly.
    """
    candidates: list[str] = []
    if href:
        try:
            parsed = urlsplit(href)
        except ValueError:
            parsed = None
        if parsed and (
            (not parsed.scheme and not parsed.netloc and href.startswith(("/", "?")))
            or (parsed.scheme == "https" and parsed.hostname == "pje1g-consultapublica.trf1.jus.br")
        ):
            candidates.append(parsed.query)
    if onclick and re.match(r"\s*openPopUp\s*\(", onclick, re.I):
        candidates.append(onclick)
    for candidate in candidates:
        match = re.search(r"(?:^|[?&]|&amp;)idProcessoDoc=([A-Za-z0-9_-]{1,80})(?:\b|&|$)", candidate)
        if match:
            return match.group(1)
    return None


def _field(root: _Node, label: str) -> str | None:
    for node in root.find_all():
        if node.tag not in ("div", "span", "label", "th") or _visible_text(node) != label:
            continue
        cell = node.closest("td") or node.closest("th")
        if not cell:
            continue
        text = _visible_text(cell)
        value = text[len(label):].strip() if text.startswith(label) else text.replace(label, "", 1).strip()
        if value:
            return value
        siblings = cell.parent.find_all("td") if cell.parent else []
        try:
            index = siblings.index(cell)
        except ValueError:
            continue
        if index + 1 < len(siblings):
            candidate = _visible_text(siblings[index + 1])
            if candidate and not candidate.startswith(label):
                return candidate
    return None


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value.strip()[:10], "%d/%m/%Y").date().isoformat()
    except ValueError:
        return None


def _explicit_yes_no(value: str | None) -> bool | None:
    if not value:
        return None
    normalized = " ".join(value.casefold().strip().split())
    if normalized in {"sim", "s", "true", "1"}:
        return True
    if normalized in {"não", "nao", "n", "false", "0"}:
        return False
    return None


def _participant_rows(root: _Node, table_prefix: str, polo: str) -> list[dict[str, Any]]:
    table = next((node for node in root.find_all("table")
                  if node.attrs.get("id", "").startswith(table_prefix)), None)
    if not table:
        return []
    result = []
    for row in table.find_all("tr"):
        cells = [child for child in row.children if isinstance(child, _Node) and child.tag in ("td", "th")]
        if len(cells) < 2:
            continue
        text = _visible_text(cells[0])
        if not text or text.casefold() == "participante":
            continue
        participant = _participant_identity(text, polo, _visible_text(cells[1]) or None)
        if participant:
            result.append(participant)
    return result


def _event_rows(root: _Node, max_pages: int = 50) -> tuple[list[dict[str, Any]], int, int, int]:
    table = _find_id(root, "j_id150:processoEvento")
    if not table:
        return [], 0, 0, 0
    movements: list[dict[str, Any]] = []
    rows = [row for row in table.find_all("tr")]
    page_size = 0
    for row in rows:
        cells = [child for child in row.children if isinstance(child, _Node) and child.tag in ("td", "th")]
        if len(cells) < 2:
            continue
        text = _visible_text(cells[0])
        match = _EVENT_DATE.match(text)
        if not match:
            continue
        date_text, time_text, description = match.groups()
        occurrence = 1 + sum(1 for item in movements if item["occurred_at"] == _movement_datetime(date_text, time_text)
                            and item["description"] == description.strip())
        source_id = hashlib.sha256(f"{date_text}|{time_text or ''}|{description.strip()}|{occurrence}".encode()).hexdigest()
        documents = []
        for link in cells[1].find_all("a"):
            label = _visible_text(link)
            href = link.attrs.get("href")
            onclick = link.attrs.get("onclick")
            if label:
                resolved_href = _resolve_document_href(href)
                documents.append({"source_id": _document_source_id(href, onclick),
                                  "label": label, "source": SOURCE_LABEL,
                                  "reference": source_id, "url": resolved_href,
                                  "public_link_candidate_available": bool(resolved_href),
                                  "public_link_validated": False})
        movements.append({"source_id": source_id,
                           "occurred_at": _movement_datetime(date_text, time_text),
                           "date_original": (date_text + (" " + time_text if time_text else "")),
                           "name": description.strip(), "description": description.strip(),
                           "code": None, "complements": [], "documents": documents,
                           "source": SOURCE_LABEL, "provenance": "PJe/TRF1 consulta pública"})
        page_size += 1
    result_counts = []
    for node in root.find_all():
        if any(isinstance(child, _Node) for child in node.children):
            continue
        match = re.fullmatch(r"(\d+)\s+resultados? encontrados?", node.text(), re.I)
        if match:
            ancestor = node
            while ancestor and not (ancestor.attrs.get("id", "").startswith("j_id150:processoEventoPanel")):
                ancestor = ancestor.parent
            if ancestor:
                result_counts.append(int(match.group(1)))
    total = result_counts[0] if result_counts else 0
    pager = _find_id(root, "j_id150:j_id568:j_id569")
    page_count = 1
    if pager:
        script_text = " ".join(node.text() for node in pager.find_all("script"))
        limit = re.search(r"maxValue.{0,8}?(\d+)", script_text, re.I)
        if limit:
            page_count = max(1, int(limit.group(1)))
        else:
            visible_numbers = [int(value) for value in re.findall(r"\b\d+\b", pager.text())]
            if len(visible_numbers) >= 2:
                page_count = max(1, visible_numbers[1])
    return movements, total, page_count, page_size


def _movement_count_known(root: _Node) -> bool:
    for node in root.find_all():
        if any(isinstance(child, _Node) for child in node.children):
            continue
        if not re.fullmatch(r"\d+\s+resultados? encontrados?", node.text(), re.I):
            continue
        ancestor = node
        while ancestor and not ancestor.attrs.get("id", "").startswith("j_id150:processoEventoPanel"):
            ancestor = ancestor.parent
        if ancestor:
            return True
    return False


def _clean_participant_text(text: str, polo: str, status: str | None = None) -> dict[str, Any] | None:
    return _participant_identity(text, polo, status)


def _participant_identity_type(value: str) -> str:
    if _CNPJ.search(value):
        return "pessoa_juridica_documento_presente"
    if _CPF.search(value):
        return "pessoa_fisica_documento_presente"
    return "unconfirmed"


def _redact_identifiers(value: str) -> str:
    value = _CPF.sub(" [identificador pessoal omitido]", value)
    return _CNPJ.sub(" [identificador pessoal omitido]", value)


def _participant_kind(role: str | None, polo: str) -> str:
    """Keep explicit non-party functions out of party counts while retaining row context."""
    normalized = normalize_procedural_function(SOURCE_LABEL, role).get("procedural_function")
    mapping = {"advogado": "advogado", "procurador": "procurador", "defensor": "defensor", "perito": "perito", "assistente técnico": "assistente_tecnico",
               "representante legal": "representante", "assistente": "parte",
               "terceiro interessado": "parte", "terceiro": "parte",
               "relator": "ator_judicial", "magistrado": "ator_judicial",
               "investigado": "participante", "vítima": "participante",
               "paciente": "participante", "autoridade coatora": "participante"}
    return mapping.get(normalized, "parte")


def _runtime_participants(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize participant rows read from rendered public tables in the browser."""
    result = []
    for item in items:
        participant = _clean_participant_text(str(item.get("text") or ""), str(item.get("polo") or "outros"),
                                              str(item.get("status") or "") or None)
        if participant:
            result.append(participant)
    return result


async def _read_runtime_evidence(page) -> list[dict[str, Any]]:
    """Read visible table cell text only; never inspect/fetch linked documents."""
    return await page.evaluate("""() => {
      const result = [];
      const tables = [
        ['j_id150:processoPartesPoloAtivoResumidoTableBinding', 'ativo'],
        ['j_id150:processoPartesPoloPassivoResumidoTableBinding', 'passivo'],
        ['j_id150:processoParteOutrosInteressadosResumidoTableBinding', 'outros']
      ];
      for (const [id, polo] of tables) {
        const table = document.getElementById(id);
        if (!table) continue;
        for (const row of table.querySelectorAll('tr')) {
          const cells = [...row.children].filter(e => e.tagName === 'TD' || e.tagName === 'TH');
          if (cells.length < 2) continue;
          result.push({text: cells[0].innerText || cells[0].textContent || '',
                       status: cells[1].innerText || cells[1].textContent || '', polo});
        }
      }
      return result;
    }""")


def _movement_datetime(date_text: str, time_text: str | None) -> str:
    dt = datetime.strptime(date_text + (" " + time_text if time_text else " 00:00:00"),
                           "%d/%m/%Y %H:%M:%S" if time_text and time_text.count(":") == 2 else
                           "%d/%m/%Y %H:%M" if time_text else "%d/%m/%Y %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc).isoformat()


def parse_detail_html(html: str, number: str, source_id: str = "TRF1-PJE-JE") -> dict[str, Any]:
    """Extract only visible public fields; useful for sanitized fixture regression tests."""
    normalized = normalize(number)
    root = _Tree(html).root
    text = root.text()
    if format_number(normalized) not in text:
        raise ValueError("Número consultado não corresponde ao processo exibido.")
    class_value = _field(root, "Classe Judicial")
    class_match = re.match(r"(.*?)\s*\((\d+)\)\s*$", class_value or "")
    organ_value = _field(root, "Órgão Julgador")
    organ, address = (organ_value.split(" Endereço ", 1) + [None])[:2] if organ_value else (None, None)
    distribution_text = _field(root, "Data da Distribuição")
    subjects_text = _field(root, "Assunto")
    case_value_text = (_field(root, "Valor da Causa") or _field(root, "Valor da causa") or
                       _field(root, "Valor da Ação"))
    secrecy_text = (_field(root, "Segredo de Justiça") or _field(root, "Segredo de justiça") or
                    _field(root, "Segredo de justica"))
    legal_aid_text = (_field(root, "Justiça Gratuita") or _field(root, "Justiça gratuita") or
                      _field(root, "Assistência Judiciária Gratuita"))
    participants = []
    participants.extend(_participant_rows(root, "j_id150:processoPartesPoloAtivoResumidoTableBinding", "ativo"))
    participants.extend(_participant_rows(root, "j_id150:processoPartesPoloPassivoResumidoTableBinding", "passivo"))
    participants.extend(_participant_rows(root, "j_id150:processoParteOutrosInteressadosResumidoTableBinding", "outros"))
    movements, movement_total, pages_total, page_size = _event_rows(root)
    documents = []
    table = _find_id(root, "j_id150:processoDocumentoGridTab")
    if table:
        for row in table.find_all("tr"):
            cells = [child for child in row.children if isinstance(child, _Node) and child.tag in ("td", "th")]
            if len(cells) < 2:
                continue
            if any(cell.tag == "th" for cell in cells):
                continue
            label = _visible_text(cells[0])
            if label and label.casefold() not in {"documento", "certidão"}:
                # Keep document metadata only; never fetch the linked file.
                view = next((node for node in cells[0].find_all() if node.attrs.get("title") == "Visualizar"), None)
                onclick = view.attrs.get("onclick", "") if view else ""
                href = view.attrs.get("href") if view else None
                documents.append({"source_id": _document_source_id(href, onclick),
                                  "description": label, "type": _visible_text(cells[1]) or None,
                                  "url": _resolve_document_href(href),
                                  "public_link_candidate_available": bool(_resolve_document_href(href)),
                                  "public_link_validated": False,
                                  "source": SOURCE_LABEL, "metadata_only": True})
    return {
        "number": normalized, "formatted_number": format_number(normalized),
        "tribunal": "TRF1", "system": "PJe", "degree": "JE" if source_id.endswith("-JE") else "1º grau",
        "class": class_value, "class_code": class_match.group(2) if class_match else None,
        "subjects": [subjects_text] if subjects_text else [], "distribution_date": _iso_date(distribution_text),
        "distribution_date_original": distribution_text,
        "jurisdiction": _field(root, "Jurisdição"),
        "court_organ": {"name": (organ or "").strip() or None, "address": (address or "").strip() or None},
        "case_value": case_value_text or None,
        "case_value_evidence": "capa processual pública" if case_value_text else None,
        "judicial_secrecy": _explicit_yes_no(secrecy_text),
        "judicial_secrecy_original": secrecy_text,
        "judicial_secrecy_evidence": "campo explícito da capa processual pública" if _explicit_yes_no(secrecy_text) is not None else None,
        "legal_aid": _explicit_yes_no(legal_aid_text),
        "legal_aid_original": legal_aid_text,
        # A last movement is not promoted to a process situation.
        "situation": None, "situation_evidence": None,
        "participants": participants, "movements": movements, "documents": documents,
        "hearings": [],
        "coverage": {"movement_total_reported": movement_total, "movement_total_known": _movement_count_known(root),
                     "movement_pages_total": pages_total,
                     "movement_pages_collected": 1, "movement_page_size": page_size,
                     "movement_count": len(movements), "participants": len(participants),
                     "document_metadata": len(documents),
                     "complete": _movement_count_known(root) and movement_total == len(movements)},
    }


class PJeTRF1Connector:
    source_ids = SOURCE_IDS
    source_id = SOURCE_LABEL
    capabilities = SourceCapabilities(process_lookup=True, metadata=True, movements=True,
                                      process_cover=True, participants=True, hearings=False,
                                      pagination=True, browser_required=True,
                                      document_metadata=True, incremental=False,
                                      authentication_required=False)

    def __init__(self, *, timeout_seconds: float = 60, max_pages: int = 50,
                 browser_channel: str | None = None, browser_executable: str | None = None,
                 verification_callback=None, verification_cancelled=None, worker_id=None):
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages
        self.browser_channel = browser_channel or os.getenv("PROCESSWATCH_PJE_BROWSER", "").strip() or None
        self.browser_executable = browser_executable or os.getenv("PROCESSWATCH_PJE_BROWSER_EXECUTABLE", "").strip() or None
        self.verification_callback = verification_callback
        self.verification_cancelled = verification_cancelled or (lambda: False)
        self.worker_id = worker_id

    @staticmethod
    def _source_url(source_id: str) -> str:
        item = get_source(source_id)
        if not item or item.get("court") != "TRF1" or item.get("system", "").casefold() != "pje":
            raise ValueError("Combinação TRF1/PJe não consta da matriz de fontes.")
        url = item.get("query_url")
        if not url or not url.startswith("https://pje1g-consultapublica.trf1.jus.br/"):
            raise ValueError("URL oficial de consulta não verificada para TRF1/PJe.")
        return url

    def _browser_options(self):
        if self.browser_executable:
            return {"executable_path": self.browser_executable}
        if self.browser_channel:
            return {"channel": self.browser_channel}
        candidates = [
            Path(os.getenv("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.getenv("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.getenv("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return {"executable_path": str(candidate)}
        for channel in ("msedge", "chrome"):
            if shutil.which(channel):
                return {"channel": channel}
        return {}

    async def collect(self, request: CollectionRequest) -> ConnectorResult:
        number = normalize(request.number)
        source = request.source
        if source not in self.source_ids:
            return self._error(source, "unsupported_source", "Fonte TRF1/PJe não corresponde à matriz.",
                               ConnectorState.NOT_VERIFIED, retryable=False, stage="validation")
        if (request.tribunal or "").casefold() != "trf1" or (request.system or "").casefold() != "pje":
            return self._error(source, "routing_mismatch", "Tribunal e sistema precisam estar confirmados pelo roteador.",
                               ConnectorState.NOT_VERIFIED, retryable=False, stage="validation")
        expected_degree = "je" if source.endswith("-JE") else "1o grau"
        normalized_degree = " ".join(str(request.degree or "").casefold().replace("º", "o").split())
        if normalized_degree != expected_degree:
            return self._error(source, "degree_mismatch", "Grau do processo não corresponde ao conector TRF1/PJe.",
                               ConnectorState.NOT_VERIFIED, retryable=False, stage="validation")
        url = self._source_url(source)
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return self._error(source, "browser_runtime_missing",
                               "Instale a dependência opcional de navegador do ProcessWatch para usar o PJe/TRF1.",
                               ConnectorState.NOT_IMPLEMENTED, retryable=False, stage="browser_start")
        try:
            async with async_playwright() as playwright:
                assisted = bool(request.parameters.get("human_assist"))
                browser = await playwright.chromium.launch(headless=not assisted, **self._browser_options())
                try:
                    context = await browser.new_context(locale="pt-BR")
                    page = await context.new_page()
                    page.set_default_timeout(int(self.timeout_seconds * 1000))
                    response = await page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout_seconds * 1000))
                    if response and response.status in (401, 403):
                        return self._error(source, "unauthorized", "Consulta pública recusou o acesso.",
                                           ConnectorState.UNAUTHORIZED, retryable=False, stage="open_search")
                    if response and response.status == 429:
                        return self._error(source, "rate_limited", "O portal limitou temporariamente as consultas.",
                                           ConnectorState.RATE_LIMITED, retryable=False, stage="open_search")
                    if response and response.status >= 500:
                        return self._error(source, "court_unavailable", "Consulta pública do TRF1 está indisponível.",
                                           ConnectorState.COURT_UNAVAILABLE, retryable=True, stage="open_search")
                    number_input = page.locator(f'[id="{_NUMBER_INPUT}"]')
                    if not await number_input.count():
                        if await page.locator('input[type="password"]').count():
                            return self._error(source, "authentication_required", "A consulta pública redirecionou para autenticação.",
                                               ConnectorState.AUTHENTICATION_REQUIRED, retryable=False, stage="open_search")
                        return self._error(source, "layout_incompatible", "Campo de pesquisa pública do TRF1 não encontrado.",
                                           ConnectorState.LAYOUT_INCOMPATIBLE, retryable=False, stage="open_search")
                    await number_input.fill(format_number(number))
                    await page.get_by_role("button", name=re.compile("Pesquisar", re.I)).click()
                    await page.wait_for_timeout(900)
                    body_text = await page.locator("body").inner_text()
                    if await self._captcha_visible(page, body_text):
                        if not assisted:
                            return self._error(source, "captcha_identified",
                                               "A consulta exige verificação humana; solicite uma retomada assistida.",
                                               ConnectorState.CAPTCHA, retryable=False, stage="search")
                        verification_id = request.parameters.get("human_verification_id")
                        if self.verification_callback:
                            self.verification_callback(verification_id, "aguardando_verificacao_humana",
                                                       worker_id=self.worker_id)
                        deadline = asyncio.get_running_loop().time() + min(
                            300, max(30, int(request.parameters.get("human_verification_ttl", 180))))
                        verified = False
                        while asyncio.get_running_loop().time() < deadline:
                            if self.verification_cancelled():
                                if self.verification_callback:
                                    self.verification_callback(verification_id, "cancelled", error_code="cancelled")
                                return self._error(source, "human_verification_cancelled",
                                    "A retomada assistida foi cancelada.", ConnectorState.CAPTCHA,
                                    retryable=False, stage="human_verification")
                            await asyncio.sleep(1)
                            try:
                                body_text = await page.locator("body").inner_text(timeout=2000)
                            except Exception:
                                continue
                            no_results = bool(re.search(r"nenhum processo encontrado|não foram encontrados processos|nenhum registro encontrado", body_text, re.I))
                            has_results = await page.locator('a[onclick*="DetalheProcessoConsultaPublica/listView.seam?ca="]').count() > 0
                            if not await self._captcha_visible(page, body_text) and (no_results or has_results):
                                verified = True
                                break
                        if not verified:
                            if self.verification_callback:
                                self.verification_callback(verification_id, "expired", error_code="verification_timeout")
                            return self._error(source, "human_verification_expired",
                                "O tempo para concluir a verificação humana terminou; solicite nova retomada.",
                                ConnectorState.CAPTCHA, retryable=False, stage="human_verification")
                        if self.verification_callback:
                            self.verification_callback(verification_id, "accepted", worker_id=self.worker_id)
                    if re.search(r"nenhum processo encontrado|não foram encontrados processos|nenhum registro encontrado", body_text, re.I):
                        return ConnectorResult(source, ConnectorState.PROCESS_NOT_FOUND, found=False,
                                               coverage={"status": "consulta concluída", "results": 0},
                                               evidence=(ConnectorEvidence(source, url, _now(), "Playwright UI", (), note="Busca pública sem resultado"),))
                    result_link = page.locator('a[onclick*="DetalheProcessoConsultaPublica/listView.seam?ca="]')
                    if not await result_link.count():
                        return self._error(source, "layout_incompatible", "O resultado esperado não apareceu após a pesquisa pública.",
                                           ConnectorState.LAYOUT_INCOMPATIBLE, retryable=False, stage="search")
                    onclick = await result_link.first.get_attribute("onclick") or ""
                    match = _LIST_LINK.search(onclick)
                    if not match:
                        return self._error(source, "layout_incompatible", "Link de detalhe do processo não reconhecido.",
                                           ConnectorState.LAYOUT_INCOMPATIBLE, retryable=False, stage="result_link")
                    href = match.group(1).replace("\\x2D", "-").replace("\\x2d", "-")
                    detail_url = "https://pje1g-consultapublica.trf1.jus.br" + href if href.startswith("/") else href
                    if not detail_url.startswith("https://pje1g-consultapublica.trf1.jus.br" + _DETAIL_PATH + "?"):
                        return self._error(source, "layout_incompatible", "Link de detalhe fora da consulta pública TRF1.",
                                           ConnectorState.LAYOUT_INCOMPATIBLE, retryable=False, stage="result_link")
                    detail_response = await page.goto(detail_url, wait_until="domcontentloaded", timeout=int(self.timeout_seconds * 1000))
                    if detail_response and detail_response.status in (401, 403):
                        return self._error(source, "unauthorized", "O detalhe público recusou o acesso.",
                                           ConnectorState.UNAUTHORIZED, retryable=False, stage="open_detail")
                    await page.locator('[id="j_id150:processoEvento"]').wait_for(state="attached")
                    current_html = await page.content()
                    data = parse_detail_html(current_html, number, source)
                    runtime_people = _runtime_participants(await _read_runtime_evidence(page))
                    if runtime_people:
                        data["participants"] = runtime_people
                        data["coverage"]["participants"] = len(runtime_people)
                    total_pages = min(int(data["coverage"]["movement_pages_total"]), self.max_pages)
                    pages_collected = 1
                    all_movements = list(data["movements"])
                    page_size = max(1, int(data["coverage"]["movement_page_size"] or 15))
                    for target_page in range(2, total_pages + 1):
                        pager_input = page.locator('[id="j_id150:j_id568:j_id569Input"]')
                        next_button = page.locator('[id="j_id150:j_id568:j_id569ArrowInc"]')
                        if not await pager_input.count() or not await next_button.count():
                            break
                        old_first = await page.locator('[id="j_id150:processoEvento"] tbody tr:nth-child(2)').inner_text()
                        old_page = await pager_input.get_attribute("value")
                        await next_button.click()
                        await page.wait_for_function(
                            "([id, old]) => { const input=document.getElementById(id); "
                            "return input && input.value !== old; }",
                            arg=["j_id150:j_id568:j_id569Input", old_page],
                            timeout=int(self.timeout_seconds * 1000))
                        await page.wait_for_function(
                            "([id, old]) => { const row=document.querySelector(`[id=\\\"${id}\\\"] tbody tr:nth-child(2)`); "
                            "return row && row.innerText.trim() && row.innerText !== old; }",
                            arg=["j_id150:processoEvento", old_first], timeout=int(self.timeout_seconds * 1000))
                        page_html = await page.content()
                        page_data = parse_detail_html(page_html, number, source)
                        if not page_data["movements"]:
                            break
                        all_movements.extend(page_data["movements"])
                        pages_collected += 1
                    # Distinguish identical occurrences; retain each row and its source page.
                    data["movements"] = all_movements
                    duplicate_counts: dict[str, int] = {}
                    for item in all_movements:
                        base = hashlib.sha256((item["date_original"] + "|" + item["description"]).encode()).hexdigest()
                        duplicate_counts[base] = duplicate_counts.get(base, 0) + 1
                        item["source_id"] = hashlib.sha256(f"{base}|{duplicate_counts[base]}".encode()).hexdigest()
                    reported = int(data["coverage"]["movement_total_reported"] or 0)
                    complete = (data["coverage"].get("movement_total_known", False) and
                                pages_collected == data["coverage"]["movement_pages_total"] and
                                len(all_movements) == reported)
                    data["coverage"].update({"movement_pages_collected": pages_collected,
                                             "movement_count": len(all_movements), "complete": complete,
                                             "participants": len(data["participants"]),
                                             "status": "consulta concluída" if complete else "consulta parcialmente concluída"})
                    state = ConnectorState.OPERATIONAL if complete else ConnectorState.PARTIAL
                    evidence = (ConnectorEvidence(source, detail_url, _now(), "Playwright UI",
                                                  ("process_cover", "participants", "movements", "document_metadata"),
                                                  note="Página pública consultada sem abrir documentos"),)
                    return ConnectorResult(source, state, found=True, data=data,
                                           coverage=data["coverage"], evidence=evidence,
                                           limitations=() if complete else ("Histórico acessível parcialmente nesta execução.",))
                finally:
                    await browser.close()
        except TimeoutError:
            return self._error(source, "timeout", "Tempo limite ao consultar a página pública do TRF1.",
                               ConnectorState.COURT_UNAVAILABLE, retryable=True, stage="browser_navigation")
        except Exception as exc:
            # Classify transport barriers without persisting raw browser exceptions (which can contain URLs).
            code, message, state, retryable = classify_browser_error(exc)
            return self._error(source, code, message, state, retryable=retryable, stage="browser_navigation")

    async def fetch_public_case(self, number: str) -> dict[str, Any]:
        """Legacy shape retained for integrations using the previous PJe provider protocol."""
        result = await self.collect(CollectionRequest(number=number, source="TRF1-PJE-JE",
            collection_type="public_case", tribunal="TRF1", system="PJe", degree="JE"))
        return {"status": result.state.value, "reason": result.error.message if result.error else None,
                "result": result.data, "coverage": result.coverage}

    @staticmethod
    async def _captcha_visible(page, body_text: str) -> bool:
        text = body_text.casefold()
        if any(token in text for token in ("verifique que você é humano", "verificação de segurança",
                                            "complete o captcha", "confirme que você não é um robô")):
            return True
        try:
            return await page.locator('iframe[title*="challenge" i], iframe[src*="hcaptcha.com/captcha/"], iframe[src*="recaptcha/api2/bframe"]').count() > 0
        except Exception:
            return False

    @staticmethod
    def _error(source, code, message, state, *, retryable, stage):
        return ConnectorResult(source, state, found=None,
                               coverage={"status": state.value, "complete": False},
                               error=ConnectorError(source, code, message, state, retryable, stage))


def classify_browser_error(exc):
    """Return a sanitized category; never persist raw browser text or request URLs."""
    detail = str(exc).upper()
    if "ERR_EMPTY_RESPONSE" in detail:
        return "err_empty_response", "O portal encerrou a conexão sem enviar resposta.", ConnectorState.EMPTY_RESPONSE, True
    if "ERR_CONNECTION_RESET" in detail:
        return "connection_reset", "A conexão com o portal foi interrompida.", ConnectorState.COURT_UNAVAILABLE, True
    if "ERR_TIMED_OUT" in detail or type(exc).__name__ == "TimeoutError":
        return "timeout", "Tempo limite ao consultar a página pública do TRF1.", ConnectorState.COURT_UNAVAILABLE, True
    if "429" in detail or "TOO MANY REQUESTS" in detail:
        return "rate_limited", "O portal limitou temporariamente as consultas; nova tentativa exige ação posterior.", ConnectorState.RATE_LIMITED, False
    if any(token in detail for token in ("401", "403", "UNAUTHORIZED", "FORBIDDEN")):
        return "unauthorized", "O portal recusou o acesso.", ConnectorState.UNAUTHORIZED, False
    if "CAPTCHA" in detail or "CHALLENGE" in detail:
        return "captcha_identified", "A consulta exige verificação humana.", ConnectorState.CAPTCHA, False
    return "browser_error", f"Falha técnica do navegador ({type(exc).__name__}).", ConnectorState.COURT_UNAVAILABLE, True


def registered_connector_ids() -> set[str]:
    """Expose only matrix rows for which the optional browser runtime exists."""
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return set()
    return set(SOURCE_IDS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

