"""Conservative timeline assembly across verified local source records."""
import json
import re
from urllib.parse import urlsplit
from datetime import datetime, timezone

from .domain import EventoTimeline
from .tpu import display_name
from .movement_titles import movement_presentation
from .event_reconciliation import reconcile_movements
from .process_phases import consolidate_phase, classify as classify_phase, PHASES


_STATUS_MOVEMENTS = {
    # Official TPU codes with their meaning verified against CNJ materials.
    # Baixa and archival are kept distinct; 893 is not treated as resumed case flow.
    "22": ("Baixa definitiva", "baixado"),
    "246": ("Arquivamento definitivo", "arquivado"),
    "849": ("Reativação", "em tramitação"),
    "893": ("Desarquivamento", "indeterminado"),
}


def derive_situation(movements, scope_labels=None):
    """Derive a cautious per-scope situation only from verified TPU status events."""
    by_scope = {}
    for row in movements or []:
        code = str(row.get("code") or row.get("codigo") or "")
        meaning = _STATUS_MOVEMENTS.get(code)
        if not meaning:
            continue
        occurred = row.get("when_at") or row.get("occurred_at") or row.get("dataHora")
        if not occurred:
            continue
        raw_scope = str(row.get("scope") or row.get("grau") or "_escopo_nao_informado")
        scope = str((scope_labels or {}).get(raw_scope, raw_scope))
        by_scope.setdefault(scope, []).append({"code": code, "status_label": meaning[0],
            "state": meaning[1], "occurred_at": occurred, "source": row.get("source") or "DataJud",
            "original_name": row.get("name") or row.get("nome"),
            "evidence_id": row.get("fingerprint") or row.get("source_id") or row.get("id")})
    states = []
    for scope, events in by_scope.items():
        events.sort(key=lambda item: event_key(item["occurred_at"]))
        latest_at = events[-1]["occurred_at"]
        latest = [event for event in events if event_key(event["occurred_at"]) == event_key(latest_at)]
        if len({event["state"] for event in latest}) > 1:
            states.append({"scope": scope, "status": "conflict", "situation": None,
                           "evidence": latest, "as_of": latest_at})
            continue
        chosen = latest[-1]
        if chosen["state"] == "indeterminado":
            label = "Desarquivamento registrado; retomada da tramitação não confirmada"
        else:
            label = chosen["status_label"] if chosen["state"] in {"arquivado", "baixado"} else "Reativado"
        states.append({"scope": scope, "status": "derived", "situation": label,
                       "state": chosen["state"], "evidence": [chosen], "as_of": chosen["occurred_at"]})
    if not states:
        return {"situation": None, "status": "unknown", "evidence": [], "scopes": []}
    distinct = {item.get("situation") for item in states if item["status"] == "derived"}
    if any(item["status"] == "conflict" for item in states) or len(distinct) > 1:
        return {"situation": None, "status": "conflict", "evidence": [e for item in states for e in item["evidence"]],
                "scopes": states}
    label = next(iter(distinct), None)
    return {"situation": label, "status": "derived", "evidence": [e for item in states for e in item["evidence"]],
            "scopes": states}


def _attribute_candidates(documents, source_data, source_label=None, source_collected_at=None):
    source_label = source_label or "fonte direta não identificada"
    aliases = {
        "classe": (("DataJud", "classe.nome", "classe"), (source_label, "class", "class")),
        "assuntos": (("DataJud", "assuntos", "assuntos"), (source_label, "subjects", "subjects")),
        "distribuicao": (("DataJud", "dataAjuizamento", "dataAjuizamento"), (source_label, "distribution_date", "distribution_date")),
        "jurisdicao": (("DataJud", "jurisdicao", "jurisdicao"), (source_label, "jurisdiction", "jurisdiction")),
        "grau": (("DataJud", "grau", "grau"), (source_label, "degree", "degree")),
        "orgao": (("DataJud", "orgaoJulgador", "orgaoJulgador"), (source_label, "court_organ", "court_organ")),
        "valor_da_causa": (("DataJud", "valorCausa", "valorCausa"), (source_label, "case_value", "case_value")),
        "segredo_justica": (("DataJud", "segredoJustica", "segredoJustica"), (source_label, "judicial_secrecy", "judicial_secrecy")),
        "justica_gratuita": (("DataJud", "justicaGratuita", "justicaGratuita"), (source_label, "legal_aid", "legal_aid")),
        "situacao": (("DataJud", "situacao", "situacao"), (source_label, "situation", "situation")),
    }
    candidate_map = {key: [] for key in aliases}
    for doc in documents:
        updated = doc.get("dataHoraUltimaAtualizacao")
        # `_scope` is an internal document identity/hash in stored DataJud
        # responses. Judicial context is carried by `grau` and must be preferred.
        scope = doc.get("grau") or doc.get("_scope") or doc.get("id")
        for attribute, (source, path, key) in [(name, pair[0]) for name, pair in aliases.items()]:
            original = doc.get(key)
            value = original
            code = None
            if key == "classe" and isinstance(value, dict):
                code = value.get("codigo") or value.get("code")
                value = value.get("nome")
            if _has_attribute_value(value):
                candidate_map[attribute].append({"source": source, "field": path, "original": original,
                    "value": value, "code": code, "reference_at": updated, "scope": scope})
    for attribute, (source, path, key) in [(name, pair[1]) for name, pair in aliases.items()]:
        value = source_data.get(key)
        if _has_attribute_value(value):
            candidate_map[attribute].append({"source": source, "field": path, "original": value,
                "value": value, "reference_at": source_data.get("collected_at") or source_collected_at,
                "scope": source_data.get("degree")})
    return candidate_map


def _has_attribute_value(value):
    if value is None or value == "" or value == []:
        return False
    if isinstance(value, dict):
        return any(_has_attribute_value(item) for item in value.values())
    return True


def _scope_key(value):
    if isinstance(value, dict):
        value = value.get("nome") or value.get("name") or value.get("codigo") or value.get("code")
    text = " ".join(str(value or "").casefold().replace("º", "o").split())
    aliases = {"juizado especial": "je", "juizados especiais": "je",
               "turma recursal": "tr", "turmas recursais": "tr",
               "1o grau": "1g", "primeiro grau": "1g",
               "2o grau": "2g", "segundo grau": "2g"}
    return aliases.get(text, text)


def _attribute_value_key(attribute, item):
    value = item.get("value")
    if attribute == "distribuicao":
        raw = str(value or "").strip()
        match = re.match(r"^(\d{4})-?(\d{2})-?(\d{2})", raw)
        if match:
            return ("distribution-date", "-".join(match.groups()))
    if attribute == "classe":
        code = item.get("code")
        if code is None and isinstance(value, str):
            match = re.search(r"\((\d{1,6})\)\s*$", value)
            code = match.group(1) if match else None
        if code is not None:
            return ("class-code", str(code))
        value = re.sub(r"\s*\(\d{1,6}\)\s*$", "", str(value or ""))
    elif attribute == "assuntos":
        def subject_codes(value):
            if isinstance(value, dict):
                code = value.get("codigo") or value.get("code")
                if code is not None:
                    return [str(code)]
                return [code for child in value.values() for code in subject_codes(child)]
            if isinstance(value, (list, tuple)):
                return [code for child in value for code in subject_codes(child)]
            if isinstance(value, str):
                # A court may provide a complete TPU path while DataJud returns
                # only its leaf; compare that leaf instead of the display form.
                # PJe's public page parser can return a clipped final leaf such
                # as ``Pessoa com Deficiência (11946``. Accept a missing closing
                # parenthesis only at the end of the value; internal display
                # codes still require their complete ``(NNNN)`` form.
                found = re.findall(r"\((\d{1,6})\)(?=\s|$)|\((\d{1,6})\s*$", value)
                found = [complete or trailing for complete, trailing in found]
                return found[-1:] if found else []
            return []
        codes = subject_codes(value)
        if codes:
            return ("subject-leaf-codes", tuple(sorted(set(codes))))
    if isinstance(value, str):
        import unicodedata
        folded = unicodedata.normalize("NFKD", value)
        value = " ".join("".join(char for char in folded if not unicodedata.combining(char)).casefold().split())
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _resolve_attribute(candidates, *, preferred_scope=None, attribute=None):
    if not candidates:
        return {"value": None, "source": None, "status": "unknown", "evidence": [], "conflict": False,
                "rule": "no_source_value"}
    effective = candidates
    scope_key = _scope_key(preferred_scope)
    scoped = [item for item in candidates if scope_key and _scope_key(item.get("scope")) == scope_key]
    if scoped:
        effective = scoped
    distinct = {_attribute_value_key(attribute, item) for item in effective}
    if len(distinct) == 1:
        chosen = effective[-1]
        return {"value": chosen["value"], "source": chosen["source"],
                "status": "confirmed_source_value", "evidence": candidates,
                "decision_evidence": effective, "conflict": False,
                "rule": "same_scope_values_agree" if scoped else "sources_agree"}
    # A source is never selected merely because it is DataJud or a direct court
    # connector. Only comparable, explicit reference timestamps can select the
    # newest value; ties or missing dates remain unresolved and retain all evidence.
    dated = []
    for item in effective:
        reference_at = item.get("reference_at")
        if not reference_at:
            dated = []
            break
        try:
            dated.append((event_key(str(reference_at)), item))
        except (TypeError, ValueError):
            dated = []
            break
    if dated:
        latest_at = max(value for value, _ in dated)
        latest = [item for value, item in dated if value == latest_at]
        latest_values = {_attribute_value_key(attribute, item) for item in latest}
        if len(latest_values) == 1:
            chosen = latest[-1]
            return {"value": chosen["value"], "source": chosen["source"], "status": "conflict",
                    "evidence": candidates, "decision_evidence": effective,
                    "conflict_evidence": effective, "conflict": True,
                    "rule": "latest_comparable_reference_with_conflict_retained"}
    return {"value": None, "source": None, "status": "conflict", "evidence": candidates,
            "decision_evidence": effective, "conflict_evidence": effective,
            "conflict": True, "rule": "unresolved_conflict_without_unique_latest_evidence"}


def event_key(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _explicit_flag(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        folded = " ".join(value.casefold().strip().split())
        if folded in {"sim", "s", "true", "1"}:
            return True
        if folded in {"não", "nao", "n", "false", "0"}:
            return False
    return None


def _situation_bucket(value):
    text = " ".join(str(value or "").casefold().split())
    if any(term in text for term in ("arquiv", "arquivamento")):
        return "arquivado"
    if "baixa" in text or "baixado" in text:
        return "baixado"
    if any(term in text for term in ("reativad", "em tramita", "em andamento", "ativo")):
        return "em_tramitacao"
    if any(term in text for term in ("suspens", "sobrest")):
        return "suspenso"
    if "encerrad" in text:
        return "encerrado_confirmado"
    return None


def process_metadata(watch, movements, *, source_data=None, source_label=None, source_collected_at=None, communications=()):
    documents = json.loads(watch.get("metadata") or "[]")
    latest = max(documents, key=lambda doc: doc.get("dataHoraUltimaAtualizacao") or "", default={})
    source_data = source_data if isinstance(source_data, dict) else {}
    # Preserve every field-level source candidate and conflict; selection policy is
    # attribute scoped: explicit direct cover fields win for the same process, with
    # DataJud retained as alternate evidence. No blanket provider priority is applied.
    datajud_documents = []
    for doc in documents:
        if not isinstance(doc, dict):
            continue
        datajud_documents.append({**doc, "classe": doc.get("classe") or doc.get("class"),
            "orgaoJulgador": doc.get("orgaoJulgador") or doc.get("court_organ"),
            "valorCausa": doc.get("valorCausa", doc.get("valor_da_causa", doc.get("valor_causa"))),
            "segredoJustica": doc.get("segredoJustica", doc.get("segredo_justica", doc.get("sigilo"))),
            "justicaGratuita": doc.get("justicaGratuita", doc.get("justica_gratuita")),
            "situacao": doc.get("situacao", doc.get("situacaoProcessual"))})
    candidates = _attribute_candidates(datajud_documents, source_data, source_label, source_collected_at)
    preferred_scope = source_data.get("degree") or latest.get("grau")
    resolved = {key: _resolve_attribute(value, preferred_scope=preferred_scope, attribute=key)
                for key, value in candidates.items()}
    # Keep provider-native values intact while converting explicit tri-state flags.
    case_value = resolved["valor_da_causa"]["value"]
    secrecy_raw = (None if resolved["segredo_justica"]["conflict"] else
                   _explicit_flag(resolved["segredo_justica"]["value"]))
    legal_aid_raw = (None if resolved["justica_gratuita"]["conflict"] else
                     _explicit_flag(resolved["justica_gratuita"]["value"]))
    explicit_situation = resolved["situacao"]["value"]
    if isinstance(explicit_situation, dict):
        explicit_situation = explicit_situation.get("name") or explicit_situation.get("description") or explicit_situation.get("nome")
    scope_labels = {}
    for doc in datajud_documents:
        if doc.get("_scope"):
            scope_labels[str(doc["_scope"])] = "|".join(str(doc.get(key) or "") for key in
                ("tribunal", "grau", "sistema"))
    derived = derive_situation(movements, scope_labels)
    chosen_situation = (None if resolved["situacao"]["conflict"] else
                        explicit_situation if explicit_situation not in (None, "") else derived["situation"])
    explicit_bucket = _situation_bucket(explicit_situation)
    derived_buckets = {"em_tramitacao" if state == "em tramitação" else state
                       for state in (item.get("state") for item in derived.get("scopes", [])) if state}
    derived_bucket = next(iter(derived_buckets)) if len(derived_buckets) == 1 else None
    semantic_conflict = bool(explicit_situation and derived["situation"] and
        (explicit_bucket != derived_bucket if explicit_bucket and derived_bucket else
         str(explicit_situation).strip().casefold() != derived["situation"].casefold()))
    situation_status = ("conflict" if resolved["situacao"]["conflict"] or semantic_conflict
                        else "explicit" if explicit_situation not in (None, "")
                        else derived["status"])
    if resolved["situacao"]["conflict"]:
        situation_state = None
    elif explicit_situation not in (None, ""):
        normalized_explicit = " ".join(str(explicit_situation).casefold().split())
        explicit_closed = normalized_explicit in {"encerrado", "encerrada", "encerrado definitivamente",
                                                   "encerrada definitivamente"}
        situation_state = "encerrado_confirmado" if explicit_closed else "explicit_source_status"
    else:
        derived_states = {item.get("state") for item in derived.get("scopes", [])
                          if item.get("status") == "derived"}
        situation_state = next(iter(derived_states)) if len(derived_states) == 1 else None
    provenance = {key: value for key, value in resolved.items()}
    conflicts = {key: value.get("conflict_evidence", value["evidence"])
                 for key, value in resolved.items() if value["conflict"]}
    if situation_status == "conflict" and derived["evidence"]:
        conflicts["situacao"] = [*conflicts.get("situacao", []),
            {"source": "DataJud movements/TPU", "field": "movimentos.codigo",
             "original": derived["evidence"], "value": derived["situation"],
             "reference_at": derived["evidence"][-1].get("occurred_at"), "rule": "verified_tpu_event_sequence"}]
    value_evidence = resolved["valor_da_causa"]["evidence"]
    value_source = resolved["valor_da_causa"]["source"]
    secrecy_source = resolved["segredo_justica"]["source"] if secrecy_raw is not None else None
    legal_aid_source = resolved["justica_gratuita"]["source"] if legal_aid_raw is not None else None
    organ = resolved["orgao"]["value"] or {}
    cls = resolved["classe"]["value"]
    system = latest.get("sistema") or {}
    subjects = resolved["assuntos"]["value"] or []
    distribution = resolved["distribuicao"]["value"]
    jurisdiction = resolved["jurisdicao"]["value"]
    grade = resolved["grau"]["value"]
    result = {
        "number": watch["number"], "court": watch["court"],
        "classe": cls.get("nome") if isinstance(cls, dict) else cls,
        "assuntos": subjects,
        "distribuicao": distribution,
        "jurisdicao": jurisdiction,
        "grau": grade,
        "orgao": organ if isinstance(organ, dict) else {"nome": str(organ)},
        "sistema": system.get("nome") if isinstance(system, dict) else system,
        "ultima_atualizacao_documento": latest.get("dataHoraUltimaAtualizacao"),
        "ultima_movimentacao": max((row["when_at"] for row in movements),
                                     key=event_key, default=None),
        "ultima_sincronizacao": watch.get("last_checked"),
        "situacao": chosen_situation,
        "situacao_status": situation_status,
        "situacao_estado": situation_state,
        "situacao_explicita": explicit_situation,
        "situacao_derivada": derived,
        "situacao_fonte": resolved["situacao"]["source"] if explicit_situation and situation_status != "conflict" else
            ("DataJud/TPU" if derived["status"] == "derived" and situation_status != "conflict" else None),
        "valor_da_causa": case_value,
        "valor_da_causa_fonte": value_source,
        "valor_da_causa_coletado_em": source_collected_at if value_source == source_label else
            next((item.get("reference_at") for item in reversed(value_evidence)
                  if item.get("source") == value_source), None),
        "valor_da_causa_conflito": resolved["valor_da_causa"]["conflict"],
        "segredo_justica": ("Divergência entre fontes" if resolved["segredo_justica"]["conflict"] else
            "Sim" if secrecy_raw is True else "Não" if secrecy_raw is False else "Não informado"),
        "segredo_justica_fonte": secrecy_source,
        "segredo_justica_coletado_em": source_collected_at if secrecy_source == source_label else
            next((item.get("reference_at") for item in reversed(resolved["segredo_justica"]["evidence"])
                  if item.get("source") == secrecy_source), None),
        "justica_gratuita": ("Divergência entre fontes" if resolved["justica_gratuita"]["conflict"] else
            "Sim" if legal_aid_raw is True else "Não" if legal_aid_raw is False else "Não informado"),
        "justica_gratuita_fonte": legal_aid_source,
        "proveniencia_atributos": provenance,
        "conflitos_atributos": conflicts,
    }
    phase = consolidate_phase(movements, source_data=source_data, communications=communications)
    explicit_phase = classify_phase(explicit_situation) if explicit_situation else None
    def phase_date(value):
        try: return event_key(str(value))
        except (ValueError, TypeError): return datetime.min.replace(tzinfo=timezone.utc)
    explicit_date = max((entry.get("reference_at") for entry in resolved["situacao"]["evidence"]
                         if entry.get("reference_at")), key=phase_date, default=None)
    use_explicit = bool(explicit_phase and (phase["code"] == "desconhecido" or
        phase_date(explicit_date) > phase_date(phase["as_of"]) or
        phase_date(explicit_date) == phase_date(phase["as_of"]) and explicit_phase != "em_tramitacao"))
    if use_explicit:
        phase = {**phase, "code": explicit_phase, "label": PHASES[explicit_phase], "as_of": explicit_date,
                 "evidence": resolved["situacao"]["evidence"], "method": "explicit_source_status"}
    result["situacao"] = phase["label"] if phase["code"] != "desconhecido" else None
    result["fase_processual"] = phase
    if not use_explicit and phase["code"] != "desconhecido":
        result["situacao_estado"] = phase["code"]
        result["situacao_fonte"] = phase["evidence"][0]["source"]
        result["situacao_status"] = "conflict" if situation_status == "conflict" else "derived"
        result["situacao_derivada"] = {**derived, "situation": phase["label"], "status": "derived",
                                      "phase": phase["code"], "evidence": phase["evidence"]}
    if not candidates["segredo_justica"]:
        for key in ("segredo_justica", "segredo_justica_fonte", "segredo_justica_coletado_em"):
            result.pop(key, None)
    return result


def consolidate(number, movements, communications, repository=None, org="local", court_snapshots=None):
    events = []
    confirmed_pairs = []
    enrichments = repository.movement_enrichments(org, number, movements) if repository else {}
    for row in movements:
        original_name = row["name"]
        enrichment = enrichments.get(row["fingerprint"])
        presentation = movement_presentation(row, enrichment)
        title = presentation["title"]
        events.append(EventoTimeline(
            id="datajud:" + row["fingerprint"], number=number,
            tipo="movimentacao", titulo=title,
            descricao=original_name, occurred_at=row["when_at"],
            temporal_context="data da movimentação", fonte="DataJud",
            original_id=json.loads(row["raw"]).get("id"),
            collected_at=row["last_seen"], validation="TPU validada" if display_name(row["code"], original_name) != original_name else "original",
            original_title=original_name, code=row["code"],
            complements=json.loads(row["complements"]),
            complement_display=presentation["complement_display"],
            original=json.loads(row["raw"]),
            tpu=enrichment,
            fontes=["DataJud"], source_event_ids=["datajud:" + row["fingerprint"]],
            documents=_datajud_document_metadata(json.loads(row["raw"])),
        ))
    for item in communications:
        event_at = item.get("published_at") or item.get("available_at")
        if not event_at:
            continue
        events.append(EventoTimeline(
            id="djen:" + item["source_id"], number=number,
            tipo="comunicacao", titulo=item.get("tipo") or "Comunicação judicial",
            descricao=item.get("text_plain"), occurred_at=event_at,
            temporal_context="data de publicação" if item.get("published_at") else "data de disponibilização",
            fonte="DJEN", original_id=item["source_id"], original_url=item.get("url"),
            collected_at=item.get("collected_at"),
            original=item,
        ))
        if item.get("hearing_at") and item.get("hearing_evidence"):
            events.append(EventoTimeline(
                id="djen-hearing:" + item["source_id"], number=number,
                tipo="audiencia", titulo="Audiência " + (item.get("hearing_status") or "informada"),
                descricao=item["hearing_evidence"], occurred_at=item["hearing_at"],
                temporal_context="data de audiência informada pela fonte", fonte="DJEN",
                original_id=item["source_id"], original_url=item.get("url"),
                collected_at=item.get("collected_at"), validation="campo estruturado",
                related_to=["djen:" + item["source_id"]], original=item,
            ))
    # Keep source rows separate until the conservative matcher proves a unique pair.
    for snapshot in court_snapshots or []:
        for row in snapshot.get("movements", []):
            if not row.get("occurred_at"):
                continue
            source_name = row.get("source") or snapshot.get("source") or "PJe/TRF1"
            prefix = ("trf4-eproc" if source_name == "eproc/TRF4" else
                      "trf4-assist" if source_name.startswith("TRF4") else "pje")
            events.append(EventoTimeline(
                id=prefix + ":" + str(row.get("source_id") or "unknown"), number=number,
                tipo="movimentacao", titulo=row.get("name") or "Movimentação do tribunal",
                descricao=row.get("description"), occurred_at=row["occurred_at"],
                temporal_context="data da movimentação", fonte=source_name,
                original_id=row.get("source_id"), collected_at=snapshot.get("collected_at"),
                validation="texto original da consulta pública", original=row,
                fontes=[source_name], source_event_ids=[prefix + ":" + str(row.get("source_id") or "unknown")],
                documents=_safe_documents(row.get("documents", [])),
            ))
    if repository:
        index = {event.id: event for event in events}
        for relation in repository.event_relations(org, number):
            if relation["status"] != "confirmed":
                continue
            confirmed_pairs.append((relation["left_id"], relation["right_id"]))
            left, right = index.get(relation["left_id"]), index.get(relation["right_id"])
            if left and right:
                left.related_to.append(right.id)
                right.related_to.append(left.id)
    serialized = [event.model_dump() for event in events]
    serialized = reconcile_movements(serialized, confirmed_pairs=confirmed_pairs)
    return sorted(serialized, key=lambda event: (event_key(event["occurred_at"]), event["id"]), reverse=True)


def _safe_documents(items):
    """Expose only useful metadata and links explicitly marked as public/validated."""
    result = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type") or item.get("document_type")
        # Some captured JSF rows included onclick JavaScript where a type should be.
        if kind and re.search(r"\b(function|javascript:|openpopup|clearformhiddenparams)\b", str(kind), re.I):
            kind = None
        raw_url = item.get("url")
        host = urlsplit(raw_url).hostname if isinstance(raw_url, str) else None
        source = item.get("source") or "PJe/TRF1"
        allowed_host = source == "PJe/TRF1" and host == "pje1g-consultapublica.trf1.jus.br"
        validated = bool(item.get("public_link_validated") is True and allowed_host and raw_url.startswith("https://"))
        candidate = bool(item.get("public_link_candidate_available"))
        safe_candidate = bool(candidate and allowed_host and raw_url.startswith("https://"))
        result.append({
            "source": source,
            "source_id": item.get("source_id") or item.get("document_id") or item.get("reference"),
            "description": item.get("description") or item.get("label"),
            "type": kind,
            "metadata_only": bool(item.get("metadata_only", True)),
            "url": item.get("url") if validated or safe_candidate else None,
            "public_link_validated": validated,
            "public_link_candidate_available": candidate,
            "link_status": ("validado" if validated else
                            "Link localizado, mas acesso público não validado" if candidate else
                            "Link público não disponível"),
        })
    return result


def _datajud_document_metadata(original):
    if not isinstance(original, dict):
        return []
    found = original.get("documentos") or original.get("documents") or []
    if not isinstance(found, list):
        found = [found] if isinstance(found, dict) else []
    return _safe_documents(found)
