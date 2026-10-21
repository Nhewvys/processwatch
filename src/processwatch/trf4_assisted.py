"""Validation and deterministic normalization for user-confirmed TRF4 imports."""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .cnj import normalize, tribunal

SOURCE = "TRF4 — importação assistida"
ALLOWED_HOSTS = {"consulta.trf4.jus.br", "www.trf4.jus.br"}
MAX_MOVEMENTS = 500
MAX_DOCUMENTS = 1000
MAX_TEXT = 2000


def _plain_text(value, *, limit=MAX_TEXT, required=False):
    if not isinstance(value, str):
        if required:
            raise ValueError("Texto obrigatório inválido.")
        return ""
    value = " ".join(value.replace("\x00", "").split()).strip()
    if len(value) > limit:
        raise ValueError("Campo de texto excede o limite permitido.")
    if re.search(r"(?is)<\s*(script|iframe|object|embed|svg|math)\b|javascript\s*:", value):
        raise ValueError("Conteúdo ativo não é permitido.")
    if required and not value:
        raise ValueError("Texto obrigatório ausente.")
    return value


def _safe_document_url(value):
    """Retain only a clean same-site candidate; never persist session/query tokens."""
    if not value:
        return None
    if not isinstance(value, str) or len(value) > 2048:
        return None
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        return None
    if not parsed.path.startswith("/trf4/"):
        return None
    return f"https://{parsed.hostname}{parsed.path}"


def normalize_capture(payload: dict, expected_number: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Formato de captura inválido.")
    if payload.get("schema_version") != 1 or payload.get("source") != "TRF4":
        raise ValueError("Versão ou fonte da captura não suportada.")
    if payload.get("capture_mode") != "chrome_extension_assisted":
        raise ValueError("Modo de captura não suportado.")
    if payload.get("page_host") != "consulta.trf4.jus.br":
        raise ValueError("A captura não identifica a página oficial de consulta do TRF4.")
    number = normalize(_plain_text(payload.get("process_number"), limit=30, required=True))
    if number != expected_number or tribunal(number) != "trf4":
        raise ValueError("O processo capturado não corresponde ao processo TRF4 esperado.")
    captured = payload.get("captured_at")
    try:
        captured_at = datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ValueError("Data da captura inválida.") from None
    if captured_at.tzinfo is None:
        raise ValueError("A data da captura precisa informar fuso horário.")
    captured_at = captured_at.astimezone(timezone.utc).isoformat()
    movements = payload.get("movements")
    documents = payload.get("documents", [])
    if not isinstance(movements, list) or not movements or len(movements) > MAX_MOVEMENTS:
        raise ValueError("A captura deve conter de 1 a 500 movimentações.")
    if not isinstance(documents, list) or len(documents) > MAX_DOCUMENTS:
        raise ValueError("Lista de documentos inválida ou acima do limite.")

    normalized_movements = []
    occurrences = defaultdict(int)
    known_ids = set()
    document_count = 0
    for entry in movements:
        if not isinstance(entry, dict):
            raise ValueError("Movimentação em formato inválido.")
        name = _plain_text(entry.get("name"), required=True)
        occurred_at = _plain_text(entry.get("occurred_at"), limit=50, required=True)
        # Accept date-only or local timestamp; do not manufacture a timezone.
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?)?", occurred_at):
            raise ValueError("Data de movimentação em formato inválido.")
        try:
            datetime.fromisoformat(occurred_at)
        except ValueError:
            raise ValueError("Data de movimentação inválida.") from None
        original_date = _plain_text(entry.get("date_original") or occurred_at, limit=80)
        row_documents = entry.get("documents", [])
        if not isinstance(row_documents, list) or len(row_documents) > 50:
            raise ValueError("Metadados documentais inválidos.")
        document_count += len(row_documents)
        if document_count > MAX_DOCUMENTS:
            raise ValueError("Captura acima do limite de 1.000 metadados documentais.")
        safe_docs = []
        for doc in row_documents:
            if not isinstance(doc, dict):
                continue
            url = _safe_document_url(doc.get("url"))
            safe_docs.append({
                "source": SOURCE,
                "source_id": _plain_text(doc.get("source_id"), limit=120) or None,
                "description": _plain_text(doc.get("description"), limit=300) or None,
                "type": _plain_text(doc.get("type"), limit=100) or None,
                "url": None,  # No candidate link is publicly verified by this extension.
                "public_link_candidate_available": bool(url),
                "public_link_validated": False,
                "metadata_only": True,
                "link_status": "Link público não validado; abra pelo portal oficial" if url else "Link público não disponível",
            })
        document_identity = "|".join(sorted(
            ":".join(str(item.get(key) or "") for key in ("source_id", "type", "description"))
            for item in safe_docs))
        identity = "\n".join((number, occurred_at, name.casefold(), document_identity))
        base = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        occurrences[base] += 1
        source_id = _plain_text(entry.get("source_id"), limit=160)
        if source_id and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", source_id):
            source_id = None
        source_id = source_id or f"{base}:{occurrences[base]}"
        if source_id in known_ids:
            raise ValueError("Identificador de movimento repetido no payload.")
        known_ids.add(source_id)
        normalized_movements.append({
            "source": SOURCE, "source_id": source_id,
            "occurred_at": occurred_at, "date_original": original_date,
            "name": name, "description": name,
            "documents": safe_docs, "original": {"date": original_date, "description": name},
        })

    cls = _plain_text(payload.get("class"), limit=300) or None
    organ = _plain_text(payload.get("court_organ"), limit=300) or None
    situation = _plain_text(payload.get("situation"), limit=300) or None
    top_documents = payload.get("documents", [])
    if not isinstance(top_documents, list) or len(top_documents) > MAX_DOCUMENTS:
        raise ValueError("Lista de documentos inválida ou acima do limite.")
    if document_count + len(top_documents) > MAX_DOCUMENTS:
        raise ValueError("Captura acima do limite de 1.000 metadados documentais.")
    safe_top_documents = []
    for doc in top_documents:
        if not isinstance(doc, dict):
            raise ValueError("Metadado documental em formato inválido.")
        safe_top_documents.append({
            "source": SOURCE,
            "source_id": _plain_text(doc.get("source_id"), limit=120) or None,
            "description": _plain_text(doc.get("description"), limit=300) or None,
            "type": _plain_text(doc.get("type"), limit=100) or None,
            "url": None,
            "public_link_candidate_available": bool(_safe_document_url(doc.get("url"))),
            "public_link_validated": False, "metadata_only": True,
            "link_status": "Link público não validado; abra pelo portal oficial" if _safe_document_url(doc.get("url")) else "Link público não disponível",
        })
    subjects = payload.get("subjects", [])
    if not isinstance(subjects, list) or len(subjects) > 50:
        raise ValueError("Assuntos em formato inválido.")
    subjects = [_plain_text(item, limit=300, required=True) for item in subjects]
    pagination = payload.get("pagination_status")
    if pagination not in {"manually_loaded", "possibly_incomplete", "unknown"}:
        raise ValueError("Estado de paginação inválido.")
    return {
        "source": SOURCE, "source_id": None, "capture_mode": "chrome_extension_assisted",
        "extractor_version": _plain_text(payload.get("extractor_version") or "1.0.0", limit=30),
        "number": number, "tribunal": "TRF4", "system": "eproc", "grade": None,
        "captured_at": captured_at, "class": cls, "court_organ": organ,
        "situation": situation, "subjects": subjects,
        "distribution_date": None, "participants": [], "hearings": [],
        "documents": safe_top_documents, "movements": normalized_movements,
        "coverage": {"movement_count": len(normalized_movements), "pagination_status": pagination,
                     "complete": False, "capture_scope": "movimentos presentes no DOM no momento da ação do usuário"},
    }


def merge_capture(previous: dict | None, current: dict) -> dict:
    """Accumulate manually loaded pages without dropping prior captured movements."""
    if not previous:
        return current
    result = dict(previous)
    result.update({key: value for key, value in current.items()
                   if value not in (None, [], "") and key not in ("movements", "documents", "coverage")})
    by_id = {item["source_id"]: item for item in previous.get("movements", [])}
    by_id.update({item["source_id"]: item for item in current.get("movements", [])})
    result["movements"] = list(by_id.values())
    docs = {}
    for item in [*previous.get("documents", []), *current.get("documents", [])]:
        key = item.get("source_id") or repr(sorted(item.items()))
        docs[key] = item
    result["documents"] = list(docs.values())
    result["coverage"] = dict(current.get("coverage") or {})
    result["coverage"]["movement_count"] = len(result["movements"])
    result["coverage"]["complete"] = False
    result["coverage"]["capture_scope"] = "união das movimentações presentes nas importações assistidas"
    return result
