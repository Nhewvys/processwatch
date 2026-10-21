"""Versioned presentation vocabulary; original judicial events remain evidence."""
import re
import unicodedata
from datetime import datetime, timezone

VERSION = "2026-10-15.1"
PHASES = {
    "em_tramitacao": "Em tramitação",
    "em_grau_de_recurso": "Em grau de recurso",
    "aguardando_julgamento_do_recurso": "Aguardando julgamento do recurso",
    "recurso_julgado": "Recurso julgado",
    "baixado": "Baixa definitiva",
    "arquivado": "Arquivamento definitivo",
    "transito_em_julgado": "Trânsito em julgado",
    "reativado": "Reativado",
    "desarquivado": "Desarquivamento registrado",
    "suspenso": "Suspenso",
    "sobrestado": "Sobrestado",
    "desconhecido": "Situação desconhecida",
}
# Reuse only TPU codes already verified by this project; keyword rules are
# explicit title recognizers, not a claim to a new official TPU mapping.
TPU_PHASES = {"22": "baixado", "246": "arquivado", "849": "reativado", "893": "desarquivado"}

def normalized(value):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", str(value or "")).casefold()
                            if not unicodedata.combining(c)).split())

def classify(title, code=None, *, appeal_context=False):
    text = normalized(title)
    if str(code or "") in TPU_PHASES: return TPU_PHASES[str(code)]
    for phase, label in PHASES.items():
        if text == normalized(label): return phase
    if re.search(r"\b(?:nao|sem)\s+(?:houve\s+)?(?:transito|baixa|arquivamento|reativacao)", text): return None
    for pattern, phase in (
        (r"\btransit(?:o|ado) em julgado\b", "transito_em_julgado"),
        (r"\bdesarquiv", "desarquivado"),
        (r"\b(?:reativa|retomada da tramitacao)", "reativado"),
        (r"\bbaixa(?:do|da)? definitiva\b", "baixado"),
        (r"\barquivamento definitivo\b|\barquivado definitivamente\b", "arquivado"),
        (r"\bacordao\b|\bjulgamento definitivo d[oe] recurso\b|\brecurso (?:julgado|provido|desprovido|nao provido)\b", "recurso_julgado"),
        (r"\bpauta\b|\binclus[aã]o em pauta\b|\b(?:conclusos?|para) (?:para )?julgamento\b", "awaiting"),
        (r"\bsobrest", "sobrestado"),
        (r"\bsuspen", "suspenso"),
        (r"\brecurso\b|\bagravo\b|\bapelacao\b|\bturma recursal\b", "em_grau_de_recurso"),
        (r"^em (?:tramitacao|andamento)$", "em_tramitacao"),
    ):
        if re.search(pattern, text):
            return ("aguardando_julgamento_do_recurso" if appeal_context or "recurso" in text or "recursal" in text
                    else "em_tramitacao") if phase == "awaiting" else phase
    return None

def _time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return datetime.min.replace(tzinfo=timezone.utc)

def consolidate_phase(movements, *, source_data=None, communications=()):
    rows = [dict(row, source=row.get("source") or "DataJud") for row in movements or []]
    source_data = source_data or {}
    rows += [dict(row, source=row.get("source") or "PJe/TRF1") for row in source_data.get("movements", [])]
    # Read only unambiguous publication headings; hypothetical judicial orders
    # inside a decision ("após o trânsito, arquive-se") do not establish a phase.
    for communication in communications or ():
        text = str(communication.get("text_plain") or "")
        heading = re.search(r"Intimação da Pauta de Julgamentos|foi inclu[ií]do na sess[aã]o de julgamento", text, re.I)
        if heading:
            rows.append({"name": heading.group(0) + " do recurso" if re.search(r"RECORRENTE|RECURSO|TURMA RECURSAL", text, re.I) else heading.group(0),
                "when_at": communication.get("published_at") or communication.get("available_at"),
                "source": "DJEN", "source_id": communication.get("source_id")})
    evidence = []
    appeal = False
    for row in sorted(rows, key=lambda row: _time(row.get("when_at") or row.get("occurred_at"))):
        title = row.get("name") or row.get("nome") or row.get("title") or ""
        code = row.get("code") or row.get("codigo")
        phase = classify(title, code, appeal_context=appeal)
        if not phase: continue
        appeal = phase in {"em_grau_de_recurso", "aguardando_julgamento_do_recurso", "recurso_julgado"} or appeal
        evidence.append({"phase": phase, "title": title, "code": code,
            "source": row.get("source"), "occurred_at": row.get("when_at") or row.get("occurred_at"),
            "event_id": row.get("fingerprint") or row.get("source_id") or row.get("id"),
            "scope": row.get("scope"), "method": "verified_tpu_code" if str(code) in TPU_PHASES else "explicit_event_title"})
    specificity = {"em_tramitacao": 0, "em_grau_de_recurso": 1,
                   "aguardando_julgamento_do_recurso": 2, "recurso_julgado": 3}
    selected = max(evidence, key=lambda item: (_time(item["occurred_at"]),
        specificity.get(item["phase"], 4), item["source"] == "PJe/TRF1"), default=None)
    phase = selected["phase"] if selected else "desconhecido"
    return {"code": phase, "label": PHASES[phase], "catalog_version": VERSION,
            "as_of": selected["occurred_at"] if selected else None,
            "evidence": [selected] if selected else [], "history": evidence}
