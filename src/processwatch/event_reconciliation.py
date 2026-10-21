"""Conservative, reversible matching of cross-source movement representations."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from difflib import SequenceMatcher
from copy import deepcopy
from datetime import datetime, timezone


_STOP_WORDS = {"a", "ao", "as", "com", "da", "das", "de", "do", "dos", "e", "em",
               "na", "nas", "no", "nos", "o", "os", "para", "por", "um", "uma"}
_GENERIC_MOVEMENT_TOKENS = {
    "autos", "processo", "processual", "movimentacao", "movimentos", "documento", "documentos",
}
_MOVEMENT_ACTION_TOKENS = {
    "decurso", "decorrido", "decorridos", "remessa", "remetido", "remetidos", "remetida", "remetidas",
    "juntada", "juntado", "juntados", "juntadas", "expedicao", "expedido", "expedida", "expedidos",
    "expedidas", "certificada", "certificado", "publicada", "publicado", "disponibilizacao",
    "conclusao", "conclusos", "conclusa", "intimacao", "intimado", "intimados", "intimar", "devolvido",
}
_STRONG_SINGLE_TOKEN_COMPLEMENTS = {
    "certidao", "sentenca", "despacho", "oficio", "mandado", "alvara",
    "peticao", "intimacao", "publicacao", "acordao", "decisao",
}
_SOURCE_PRIORITY = {"PJe/TRF1": 3, "DataJud": 2}


def normalize_description(value: str | None) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(char for char in value if not unicodedata.combining(char)).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", value))


def _instant(value: str | None):
    if not value or not re.search(r"(?:T| )\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})$", value, re.I):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # Naive timestamps have no defensible cross-source timezone equivalence.
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _same_process(left: dict, right: dict) -> bool:
    left_number = re.sub(r"\D", "", str(left.get("number") or ""))
    right_number = re.sub(r"\D", "", str(right.get("number") or ""))
    return bool(left_number and right_number and left_number == right_number)


def _description(event: dict) -> str:
    text = normalize_description(event.get("original_title") or event.get("titulo") or event.get("descricao"))
    # Explicit grammatical/abbreviation variants observed in the persisted
    # court records. Original descriptions remain intact in source_details.
    variants = (
        (r"ato ordinatorio(?: praticado)?", "ato ordinatorio"),
        (r"(?:autos incluidos|inclusao) no juizo 100 digital", "inclusao no juizo 100 digital"),
        (r"(?:disponibilizado no dj eletronico(?: em \d{2} \d{2} \d{4})?|disponibilizacao no diario da justica eletronico)",
         "disponibilizacao no diario da justica eletronico"),
    )
    for pattern, canonical in variants:
        if re.fullmatch(pattern, text): return canonical
    return text


def _document_ids(event: dict) -> set[str]:
    found = set()
    documents = event.get("documents") or []
    if isinstance(documents, list):
        for item in documents:
            if not isinstance(item, dict):
                continue
            # `source_id` identifies the source row, not the same document
            # across sources. Only document-level identifiers are comparable.
            for key in ("document_id", "documentId", "hash"):
                value = item.get(key)
                if value:
                    found.add(str(value).strip())
    return found


def _document_evidence_conflicts(left: dict, right: dict) -> bool:
    def identifiers(event):
        values = set()
        for item in event.get("documents") or []:
            if isinstance(item, dict):
                values.update(str(item.get(key)).strip() for key in ("document_id", "documentId", "hash")
                              if item.get(key))
        return values

    def types(event):
        return {normalize_description(str(item.get(key)))
                for item in event.get("documents") or [] if isinstance(item, dict)
                for key in ("document_type", "documentType", "type")
                if item.get(key) and normalize_description(str(item.get(key)))}

    left_ids, right_ids = identifiers(left), identifiers(right)
    if left_ids and right_ids and not left_ids.intersection(right_ids):
        return True
    left_types, right_types = types(left), types(right)
    return bool(left_types and right_types and not left_types.intersection(right_types))


def _meaningful_complements(event: dict) -> list[str]:
    values = []
    for item in event.get("complements") or []:
        if not isinstance(item, dict):
            continue
        # Only actual source text is evidence; do not use a guessed TPU label.
        for key in ("nome", "descricao", "valor", "description", "value"):
            text = normalize_description(str(item.get(key) or ""))
            if len(text.split()) >= 2:
                values.append(text)
    return values


def _conflicting_complements(left: dict, right: dict) -> bool:
    def indexed(event):
        result = {}
        for item in event.get("complements") or []:
            if not isinstance(item, dict):
                continue
            label = normalize_description(str(item.get("nome") or item.get("descricao") or item.get("codigo") or ""))
            value = normalize_description(str(item.get("valor") or item.get("value") or ""))
            if label and value:
                result[label] = value
        return result
    a, b = indexed(left), indexed(right)
    return any(a[key] != b[key] for key in a.keys() & b.keys())


def _strictly_more_specific(left: str, right: str) -> bool:
    """True when one complete description is a strict, ordered subset of the other."""
    a, b = left.split(), right.split()
    if len(a) == len(b):
        return False
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    cursor = 0
    for token in longer:
        if cursor < len(shorter) and token == shorter[cursor]:
            cursor += 1
    if cursor != len(shorter):
        return False
    additions = [word for word in longer if word not in shorter and word not in _STOP_WORDS]
    return bool(additions)


def _families_in_text(value: str) -> set[str]:
    text = normalize_description(value)
    families = set()
    if re.search(r"\b(decurso de prazo|decorrido prazo|decorridos prazos|prazo decorrido)\b", text):
        families.add("decurso_prazo")
    if re.search(r"\b(remessa|remetidos? os autos|remetidos? autos)\b", text):
        families.add("remessa")
    if re.search(r"\b(peticao|manifestacao|contestacao|defesa)\b", text):
        families.add("peticao")
    if re.search(r"\b(juntada|juntado|juntados|juntadas)\b", text):
        families.add("juntada")
        if re.search(r"\b(certidao|documento|peticao|manifestacao|contestacao|oficio)\b", text):
            families.add("juntada_documento")
        if re.search(r"\b(manifestacao|peticao)\b", text):
            families.add("juntada_peticao")
    if "certidao" in text and ("documento" in text or "juntada" in text):
        families.add("documento")
    if re.search(r"\b(publicacao|publicado|publicada|disponibilizacao)\b", text):
        families.add("publicacao")
    if re.search(r"\b(comunicacao eletronica|comunicacoes eletronicas)\b", text):
        families.add("comunicacao_eletronica")
    if re.search(r"\b(conclusao|conclusos|conclusa)\b", text):
        families.add("conclusao")
    if re.search(r"\b(intimacao|intimado|intimados|intimar)\b", text):
        families.add("intimacao")
    return families


def _official_tpu_families(event: dict) -> set[str]:
    """Read semantic families only from a versioned, official SGT enrichment."""
    item = event.get("tpu")
    if not isinstance(item, dict):
        return set()
    if not item.get("version") or not item.get("official_name"):
        return set()
    if str(item.get("code") or "") != str(event.get("code") or ""):
        return set()
    if "cnj.jus.br/sgt/" not in str(item.get("source") or "").casefold():
        return set()
    description = normalize_description(str(item.get("official_description") or ""))
    name_families = _families_in_text(str(item.get("official_name") or ""))
    if name_families:
        # The official description may mention another act only to distinguish
        # it (for example, Remessa is explicitly not a Conclusão). For the
        # official petition definition, retain the standardized act category.
        if "juntada" in name_families or "juntada" in description:
            name_families.discard("peticao")
            name_families.add("juntada")
        return name_families
    if "comunicacao eletronica" in description:
        return {"comunicacao_eletronica"}
    if description.startswith("juntada de ") or re.search(r"\b(registra|indica|consigna) (?:a )?juntada\b", description):
        return {"juntada"}
    return set()


def _event_context(event: dict) -> str:
    values = [event.get(key) for key in ("original_title", "titulo", "descricao", "responsible", "recipient")]
    for item in event.get("complements") or []:
        if isinstance(item, dict):
            values.extend(item.get(key) for key in ("nome", "descricao", "valor", "value"))
    return normalize_description(" ".join(str(value) for value in values if value))


def _deadline_subject(event: dict) -> str | None:
    title = normalize_description(" ".join(str(event.get(key) or "")
                                             for key in ("original_title", "titulo", "descricao")))
    match = re.search(r"\bdecorrid[oa]s? prazo de (.+?)(?: em \d{2} \d{2} \d{4}|$)", title)
    if match:
        return match.group(1).strip()
    for item in event.get("complements") or []:
        if not isinstance(item, dict):
            continue
        label = normalize_description(str(item.get("nome") or item.get("descricao") or ""))
        value = normalize_description(str(item.get("valor") or item.get("value") or ""))
        if value and (label in {"parte", "destinatario", "pessoa", "advogado", "nome"}
                      or (label == value and len(value.split()) >= 2 and not re.search(r"\d", value))):
            return value
    return normalize_description(str(event.get("recipient") or "")) or None


def _matching_source_complement(datajud: dict, tribunal: dict) -> str | None:
    """A family label nominates a candidate; a source-specific clue confirms it."""
    other_text = _event_context(tribunal)
    for complement in datajud.get("complements") or []:
        if not isinstance(complement, dict):
            continue
        for key in ("nome", "valor", "value"):
            raw = complement.get(key)
            if raw is None:
                continue
            clue = normalize_description(str(raw))
            meaningful = [word for word in clue.split() if word not in _STOP_WORDS]
            specific_single_term = len(meaningful) == 1 and meaningful[0] in _STRONG_SINGLE_TOKEN_COMPLEMENTS
            if (len(meaningful) >= 2 or specific_single_term) and f" {clue} " in f" {other_text} ":
                return "complement_text_present_in_other_source"
    for key in ("responsible", "recipient"):
        left = normalize_description(str(datajud.get(key) or ""))
        right = normalize_description(str(tribunal.get(key) or ""))
        if left and right and left == right:
            return "structured_person_match"
    if _document_ids(datajud) & _document_ids(tribunal):
        return "shared_document_identifier"
    official_description = normalize_description(str((datajud.get("tpu") or {}).get("official_description") or ""))
    if "comunicacao eletronica" in official_description and "comunicacao eletronica" in other_text:
        return "official_tpu_definition_phrase_present_in_other_source"
    return None


def _semantic_family_evidence(left: dict, right: dict) -> str | None:
    if {left.get("fonte"), right.get("fonte")} != {"DataJud", "PJe/TRF1"}:
        return None
    datajud = left if left.get("fonte") == "DataJud" else right
    tribunal = right if datajud is left else left
    official_families = _official_tpu_families(datajud)
    tribunal_families = _families_in_text(_event_context(tribunal))
    source_families = _families_in_text(_event_context(datajud)) | official_families
    court_families = tribunal_families
    shared_families = source_families & court_families
    compatible = set(shared_families)
    # TPU may label a record only as a petition while the tribunal describes
    # its filing subtype. The “Outras” complement is intentionally treated as
    # underspecified, and only a unique one-to-one manifestation candidate can
    # consume it. It does not match unrelated petition subtypes.
    complement_text = normalize_description(" ".join([
        _event_context(datajud), *[str(item.get(key) or "") for item in datajud.get("complements") or []
        if isinstance(item, dict) for key in ("nome", "descricao", "valor", "value")]]))
    court_text = normalize_description(_event_context(tribunal))
    if ("peticao" in source_families and "juntada_peticao" in court_families
            and "outras" in complement_text and "manifestacao" in court_text
            and "contestacao" not in court_text and "defesa" not in court_text):
        compatible = {"peticao_outras_com_manifestacao"}
    # “Expedida/Certificada” (TPU communication) and an explicitly described
    # publication are compatible only when the other source also says publish.
    if "comunicacao_eletronica" in source_families and "publicacao" in court_families:
        compatible.add("publicacao")
    if "juntada" in compatible and "documento" in compatible:
        compatible.discard("documento")
    if len(compatible) != 1:
        return None
    family = next(iter(compatible))
    support = _matching_source_complement(datajud, tribunal)
    # Exact family semantics plus the exact timestamp and unique graph match
    # are enough. A missing counterpart field is not a conflict.
    if not support:
        support = "unique_exact_instant_compatible_act_family"
    item = datajud.get("tpu") or {}
    code = item.get("code") or datajud.get("code")
    parents = ",".join(str(value) for value in item.get("parent_codes") or [])
    return f"timestamp_official_tpu_family:{family}:code_{code}:parents_{parents}:{support}"


def _same_event(left: dict, right: dict) -> str | None:
    if not _same_process(left, right):
        return None
    left_time, right_time = _instant(left.get("occurred_at")), _instant(right.get("occurred_at"))
    if left_time is None or right_time is None or abs((left_time - right_time).total_seconds()) > 2:
        return None
    if left.get("code") and right.get("code") and str(left["code"]) != str(right["code"]):
        return None
    if _conflicting_complements(left, right) or _document_evidence_conflicts(left, right):
        return None
    for identity_key in ("recipient", "responsible"):
        left_identity = normalize_description(str(left.get(identity_key) or ""))
        right_identity = normalize_description(str(right.get(identity_key) or ""))
        if left_identity and right_identity and left_identity != right_identity:
            return None
    # Explicitly different subtypes/actors are concrete conflicts. Missing
    # subtype information remains compatible with a more detailed counterpart.
    left_context, right_context = _event_context(left), _event_context(right)
    if "decurso_prazo" in (_families_in_text(left_context) | _official_tpu_families(left)):
        left_subject, right_subject = _deadline_subject(left), _deadline_subject(right)
        if left_subject and right_subject and left_subject != right_subject:
            return None
    if ("intimacao" in left_context and "publicacao" in right_context) or (
            "publicacao" in left_context and "intimacao" in right_context):
        return None
    subtype_pairs = (("contestacao", "manifestacao"), ("certidao de intimacao", "certidao de publicacao"))
    for first, second in subtype_pairs:
        if first in left_context and second in right_context or second in left_context and first in right_context:
            return None
    left_text, right_text = _description(left), _description(right)
    if not left_text or not right_text:
        return None
    if left_time != right_time and SequenceMatcher(None, left_text, right_text).ratio() < 0.94:
        return None
    if left_text == right_text:
        return ("timestamp_and_exact_normalized_description" if left_time == right_time else
                "near_timestamp_and_exact_normalized_description")
    if {left.get("fonte"), right.get("fonte")} == {"DataJud", "PJe/TRF1"}:
        datajud = left if left.get("fonte") == "DataJud" else right
        tribunal = right if datajud is left else left
        semantic = _semantic_family_evidence(left, right)
        if semantic and any(family in semantic for family in ("decurso_prazo", "remessa")):
            return semantic
        complement_support = _matching_source_complement(datajud, tribunal)
        if complement_support == "complement_text_present_in_other_source":
            counterpart_text = _event_context(tribunal)
            single_term_support = any(
                len(normalize_description(str(item.get(key) or "")).split()) == 1
                and normalize_description(str(item.get(key) or "")) in counterpart_text.split()
                and normalize_description(str(item.get(key) or "")) in _STRONG_SINGLE_TOKEN_COMPLEMENTS
                for item in datajud.get("complements") or [] if isinstance(item, dict)
                for key in ("nome", "valor", "value"))
            suffix = (":" + complement_support if single_term_support else "")
            return "timestamp_and_explicit_specific_complement" + suffix
        similarity_action_tokens = _MOVEMENT_ACTION_TOKENS - {"intimacao", "publicacao"}
        left_terms = {word for word in left_text.split()
                      if word not in _STOP_WORDS | _GENERIC_MOVEMENT_TOKENS | similarity_action_tokens}
        right_terms = {word for word in right_text.split()
                       if word not in _STOP_WORDS | _GENERIC_MOVEMENT_TOKENS | similarity_action_tokens}
        union = left_terms | right_terms
        overlap = len(left_terms & right_terms) / len(union) if union else 0.0
        sequence = SequenceMatcher(None, left_text, right_text).ratio()
        # Require a high textual match after removing only generic legal-action
        # words; this handles small title variations while leaving distinct acts
        # at the same instant independent. Existing code/document/actor and
        # subtype conflict guards above still take precedence.
        if sequence >= 0.88 or (overlap >= 0.82 and len(left_terms & right_terms) >= 1):
            return f"timestamp_and_high_text_similarity:sequence_{sequence:.2f}:terms_{overlap:.2f}"
    if _document_ids(left) & _document_ids(right):
        return "timestamp_and_shared_document_identifier"

    # A generic label may be enriched only if a source complement explicitly
    # supplies the specific words and the one-to-one match remains unambiguous.
    if _strictly_more_specific(left_text, right_text):
        specific, generic_event = (left_text, right) if len(left_text.split()) > len(right_text.split()) else (right_text, left)
    elif _strictly_more_specific(right_text, left_text):
        specific, generic_event = (right_text, left) if len(right_text.split()) > len(left_text.split()) else (left_text, right)
    else:
        specific = generic_event = None
    if specific and any(
        complement in specific for complement in _meaningful_complements(generic_event)
    ):
        return "timestamp_and_explicit_specific_complement"
    return _semantic_family_evidence(left, right)


def _information_score(event: dict) -> tuple[int, int, int, int]:
    text = normalize_description(event.get("titulo") or event.get("original_title") or event.get("descricao")).split()
    action = bool(set(text) & _MOVEMENT_ACTION_TOKENS)
    informative = {word for word in text if word not in _STOP_WORDS and word not in _GENERIC_MOVEMENT_TOKENS
                   and word not in _MOVEMENT_ACTION_TOKENS and (len(word) > 2 or word.isdigit())}
    supplemental = 4 if event.get("complements") else 0
    supplemental += 2 if event.get("code") else 0
    tpu = event.get("tpu")
    supplemental += 3 if isinstance(tpu, dict) and tpu.get("official_name") else 0
    documents = event.get("documents") or []
    supplemental += min(3, sum(bool(item.get("source_id") or item.get("document_id") or item.get("label") or item.get("description"))
                               for item in documents if isinstance(item, dict)))
    supplemental += 4 if any(isinstance(item, dict) and item.get("public_link_validated") and item.get("url")
                             for item in documents) else 0
    supplemental += 2 if event.get("responsible") or event.get("recipient") else 0
    # A stated act with its specific object outranks a generic category title.
    # The count measures distinct factual terms, while action and source are
    # separate signals; raw text length never decides the result.
    return int(action), len(informative), supplemental, _SOURCE_PRIORITY.get(event.get("fonte"), 0)


def _all_complements(events: list[dict]) -> list[dict]:
    result, seen = [], set()
    for event in events:
        for item in event.get("complements") or []:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                result.append(item)
    return result


def _all_documents(events: list[dict]) -> list[dict]:
    result, seen = [], set()
    for event in events:
        for item in event.get("documents") or []:
            if not isinstance(item, dict):
                continue
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                result.append(item)
    return result


def _merge(events: list[dict], evidence: str) -> dict:
    # The tribunal record is the default user-facing representation; the
    # information score remains useful only as a fallback for other sources.
    primary = next((event for event in events if event.get("fonte") == "PJe/TRF1"), None)
    if primary is None:
        primary = max(events, key=_information_score)
    sources = sorted({event.get("fonte") for event in events if event.get("fonte")})
    ids = sorted({event.get("id") for event in events if event.get("id")})
    merged_id = "movement-group:" + hashlib.sha256("\n".join(ids).encode()).hexdigest()
    original_sources = []
    for event in events:
        original_sources.append({
            "id": event.get("id"), "source": event.get("fonte"),
            "original_id": event.get("original_id"), "occurred_at": event.get("occurred_at"),
            "original_title": event.get("original_title") or event.get("titulo"),
            "description": event.get("descricao"), "code": event.get("code"),
            "complements": event.get("complements") or [],
            "complement_display": event.get("complement_display") or [],
            "tpu": event.get("tpu"), "documents": event.get("documents") or [],
            "original": event.get("original"), "collected_at": event.get("collected_at"),
            "original_url": event.get("original_url"),
            "title": event.get("titulo"),
        })
    result = dict(primary)
    result.update({
        "id": merged_id,
        "tipo": "movimentacao",
        "fonte": " + ".join(sources),
        "fontes": sources,
        "preferred_source": primary.get("fonte"),
        "source_event_ids": ids,
        "source_details": original_sources,
        "documents": _all_documents(events),
        "complements": _all_complements(events),
        "complement_display": _all_complement_display(events),
        "code": primary.get("code") or next((event.get("code") for event in events if event.get("code")), None),
        "tpu": primary.get("tpu") or next((event.get("tpu") for event in events if event.get("tpu")), None),
        "equivalence": {"status": "confirmed", "evidence": evidence},
        "related_to": sorted({related for event in events for related in event.get("related_to", [])}),
    })
    return result


def _all_complement_display(events: list[dict]) -> list[dict]:
    result, seen = [], set()
    for event in events:
        for item in event.get("complement_display") or []:
            key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                result.append(item)
    return result


def reconcile_movements(events: list[dict], confirmed_pairs=()) -> list[dict]:
    """Merge unique, strongly evidenced DataJud/PJe pairs within two seconds.

    Ambiguous one-to-many and many-to-many candidates stay as separate events.
    Original records are copied into `source_details`; caller-owned records and
    SQLite snapshots are never modified.
    """
    eligible = [index for index, event in enumerate(events)
                if event.get("tipo") in ("movimentacao", "movimentacao_tribunal")
                and event.get("fonte") in ("DataJud", "PJe/TRF1")]
    confirmed = {frozenset(pair) for pair in confirmed_pairs}
    # Compare only the time window allowed by _same_event, rather than every
    # historical pair. Explicitly reviewed relations bypass this prefilter.
    buckets, instants, numbers = {}, {}, {}
    by_id = {events[index].get("id"): index for index in eligible}
    reviewed = {}
    for pair in confirmed:
        members = [by_id[value] for value in pair if value in by_id]
        if len(members) == 2:
            a, b = members
            reviewed.setdefault(a, set()).add(b)
            reviewed.setdefault(b, set()).add(a)
    for index in eligible:
        event = events[index]
        instant = _instant(event.get("occurred_at"))
        instants[index] = instant
        numbers[index] = re.sub(r"\D", "", str(event.get("number") or ""))
        if instant is not None:
            buckets.setdefault((numbers[index], int(instant.timestamp())), []).append(index)
    candidates = []
    for left_index in eligible:
        nearby = set(reviewed.get(left_index, ()))
        instant = instants[left_index]
        if instant is not None:
            second = int(instant.timestamp())
            for delta in range(-2, 3):
                nearby.update(buckets.get((numbers[left_index], second + delta), ()))
        for right_index in sorted(nearby):
            if right_index <= left_index:
                continue
            left, right = events[left_index], events[right_index]
            if left.get("fonte") == right.get("fonte"):
                continue
            if not _same_process(left, right):
                continue
            evidence = ("reviewed_confirmed_relation" if frozenset((left.get("id"), right.get("id"))) in confirmed
                        else _same_event(left, right))
            if evidence:
                candidates.append((left_index, right_index, evidence))
    degree = {}
    for left, right, _ in candidates:
        degree[left] = degree.get(left, 0) + 1
        degree[right] = degree.get(right, 0) + 1
    unambiguous = [(left, right, evidence) for left, right, evidence in candidates
                   if degree[left] == degree[right] == 1]
    grouped = {}
    consumed = set()
    for left, right, evidence in unambiguous:
        grouped[left] = _merge([events[left], events[right]], evidence)
        consumed.update((left, right))
    aliases = {}
    for index, merged in grouped.items():
        for source_event_id in merged["source_event_ids"]:
            aliases[source_event_id] = merged["id"]
    # Always return new top-level objects: alias rewriting below must not
    # mutate caller-owned snapshots, including events that remain separate.
    result = [deepcopy(grouped.get(index, event)) for index, event in enumerate(events)
              if index not in consumed or index in grouped]
    for event in result:
        event["related_to"] = sorted({aliases.get(item, item) for item in event.get("related_to", [])
                                       if aliases.get(item, item) != event.get("id")})
    return result
