"""Read-only, compact portfolio summaries for the monitored-process dashboard."""
import json
import re
import unicodedata
from datetime import datetime, timezone

from .routing import route_process
from .event_reconciliation import reconcile_movements
from .participants import canonical_participants, participant_summary, presentation_participants, professional_kind
from .consolidation import process_metadata


_PUBLIC = {"queued": "pending", "running": "running", "completed": "completed",
           "partial": "partial", "failed": "failed", "awaiting_access": "failed"}


def _time_key(value):
    if not value:
        return ""
    return str(value)


def _active_party(row):
    """Return whether public participant data explicitly identifies the active pole."""
    if row.get("procedural_pole") == "ativo" or row.get("polo_normalized") == "ativo":
        return True
    if row.get("procedural_pole") == "passivo" or row.get("polo_normalized") == "passivo":
        return False
    labels = [row.get("polo"), row.get("role"), row.get("papel"), row.get("side")]
    labels.extend(row.get("roles") or [])
    text = " ".join(str(value) for value in labels if value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    words = set(re.findall(r"[a-z]+", text))
    active = bool(words & {"ativo", "ativa", "autor", "autora", "requerente", "exequente",
                           "impetrante", "recorrente", "reclamante", "demandante", "agravante",
                           "apelante", "embargante", "opoente", "excipiente", "impugnante",
                           "suscitante", "querelante"})
    passive = bool(words & {"passivo", "passiva", "reu", "re", "requerido", "requerida",
                            "executado", "executada", "impetrado", "impetrada", "recorrido",
                            "recorrida", "reclamado", "reclamada", "demandado", "demandada",
                            "agravado", "agravada", "apelado", "apelada", "embargado", "embargada",
                            "oposto", "oposta", "excepto", "excepta", "impugnado", "impugnada",
                            "suscitado", "suscitada", "querelado", "querelada"})
    return active and not passive


def _source_summary(repo, org, watch, source, *, applicable=True, snapshot=None, human=None):
    if not applicable:
        return {"source": source, "applicable": False, "status": "not_applicable",
                "message": "Fonte não aplicável a este processo.", "last_attempt_at": None,
                "last_success_at": None, "next_attempt_at": None, "records_count": 0,
                "error": None}
    aliases = {"PJe/TRF1": ("TRF1-PJE-1G", "TRF1-PJE-JE"), "DataJud": ("DataJud",), "DJEN": ("DJEN",)}
    placeholders = ",".join("?" for _ in aliases[source])
    jobs = [dict(row) for row in repo.db.execute(
        f"SELECT * FROM collection_jobs WHERE org=? AND number=? AND source IN ({placeholders}) ORDER BY id DESC",
        (org, watch["number"], *aliases[source]))]
    latest = jobs[0] if jobs else None
    check_source = "PJe/TRF1" if source == "PJe/TRF1" else source
    check_row = repo.db.execute(
        "SELECT * FROM source_checks WHERE org=? AND number=? AND source=? ORDER BY id DESC LIMIT 1",
        (org, watch["number"], check_source)).fetchone()
    check = dict(check_row) if check_row else None
    latest_human = human
    if latest_human and latest and latest_human["job_id"] != latest["id"]:
        latest_human = None
    status = _PUBLIC.get(latest["status"]) if latest else None
    check_is_newer = bool(check and (not latest or (check.get("checked_at") or "") >= (latest.get("created_at") or "")))
    if check and source == "PJe/TRF1" and check_is_newer:
        pje_states = {"consulta concluída": "completed", "consulta parcial": "partial",
                      "consulta parcialmente concluída": "partial", "captcha_identified": "human",
                      "authentication_required": "failed", "unauthorized": "failed",
                      "layout_incompatible": "failed", "court_unavailable": "failed",
                      "timeout": "failed", "err_empty_response": "failed", "rate_limited": "failed",
                      "process_not_found": "completed"}
        check_status = pje_states.get(check["status"])
        if check_status:
            status = check_status
    elif check and source == "DJEN" and check_is_newer:
        djen_states = {"período consultado": "completed", "erro": "failed", "partial": "partial",
                       "rate_limited": "failed", "error": "failed"}
        status = djen_states.get(check["status"], status)
    if latest_human and latest_human["status"] in ("queued", "aguardando_verificacao_humana"):
        status = "human"
    if status is None and source == "DataJud":
        run = repo.db.execute("SELECT status,started_at,finished_at,error FROM runs WHERE org=? AND number=? ORDER BY id DESC LIMIT 1",
                              (org, watch["number"])).fetchone()
        if run:
            status = {"running": "running", "success": "completed", "error": "failed"}.get(run["status"])
    if source == "DataJud" and not latest:
        run = repo.db.execute("SELECT status FROM runs WHERE org=? AND number=? ORDER BY id DESC LIMIT 1",
                              (org, watch["number"])).fetchone()
        if run:
            status = {"running": "running", "success": "completed", "error": "failed"}.get(run["status"], status)
        elif watch.get("metadata"):
            status = "completed"
        elif watch.get("last_error"):
            status = "failed"
    if status is None:
        status = "not_started"
    source_data = (snapshot or {}).get("data", {}) if source == "PJe/TRF1" else {}
    if source == "PJe/TRF1":
        records = sum(len(source_data.get(key, []) or []) for key in ("movements", "participants", "documents", "hearings"))
    elif source == "DataJud":
        records = repo.db.execute("SELECT count(*) FROM movements WHERE org=? AND number=? AND active=1",
                                  (org, watch["number"])).fetchone()[0]
    else:
        records = repo.db.execute("SELECT count(*) FROM communications WHERE org=? AND number=?",
                                  (org, watch["number"])).fetchone()[0]
    successes = [job for job in jobs if job["status"] in ("completed", "partial")]
    last_success = successes[0].get("completed_at") if successes else None
    if source == "DataJud" and not last_success:
        last_success_row = repo.db.execute("SELECT finished_at FROM runs WHERE org=? AND number=? AND status='success' ORDER BY id DESC LIMIT 1",
                                           (org, watch["number"])).fetchone()
        last_success = last_success_row[0] if last_success_row else (watch.get("last_checked") if watch.get("metadata") and not watch.get("last_error") else None)
    if source == "DJEN" and check and check["status"] == "período consultado":
        last_success = check["checked_at"]
    if source == "PJe/TRF1" and check and check["status"] in ("consulta concluída", "consulta parcial", "consulta parcialmente concluída"):
        last_success = check["checked_at"]
    elif source == "PJe/TRF1":
        prior_success = repo.db.execute("""SELECT checked_at FROM source_checks WHERE org=? AND number=? AND source=?
            AND status IN ('consulta concluída','consulta parcial','consulta parcialmente concluída')
            ORDER BY id DESC LIMIT 1""", (org, watch["number"], "PJe/TRF1")).fetchone()
        if prior_success:
            last_success = prior_success[0]
    run = repo.db.execute("SELECT status,started_at,finished_at,error FROM runs WHERE org=? AND number=? ORDER BY id DESC LIMIT 1",
                          (org, watch["number"])).fetchone() if source == "DataJud" else None
    attempted = ((check or {}).get("checked_at") or ((latest or {}).get("started_at") or (latest or {}).get("created_at"))
                 or (run["started_at"] if run else None))
    error = None
    if latest and latest.get("error"):
        try:
            error = json.loads(latest["error"]).get("message")
        except (ValueError, AttributeError):
            error = "A coleta mais recente apresentou uma falha."
    if source == "DataJud" and run and run["status"] == "error":
        error = run["error"] or watch.get("last_error")
    if check and check.get("error"):
        try:
            parsed = json.loads(check["error"])
            error = parsed.get("message") if isinstance(parsed, dict) else str(parsed)
        except (ValueError, TypeError):
            error = str(check["error"])
    return {"source": source, "applicable": True, "status": status,
            "last_attempt_at": attempted, "last_success_at": last_success,
            "next_attempt_at": latest.get("scheduled_at") if latest and latest["status"] == "queued" else None,
            "records_count": records, "error": error,
            "message": "A última captura válida permanece disponível." if status == "failed" and snapshot else None}


def _consolidated_count(repo, org, number, snapshot):
    """Count timeline events using narrow event columns, not full movement payloads."""
    rows = repo.db.execute("""SELECT fingerprint,when_at,code,name,complements,raw,last_seen
        FROM movements WHERE org=? AND number=? AND active=1""", (org, number)).fetchall()
    events = []
    for row in rows:
        raw = {}
        try:
            parsed = json.loads(row["raw"] or "{}")
            raw = {key: parsed[key] for key in ("id", "documentos", "documents") if key in parsed}
        except (ValueError, TypeError):
            pass
        events.append({"id": "datajud:" + row["fingerprint"], "number": number,
                       "tipo": "movimentacao", "fonte": "DataJud", "occurred_at": row["when_at"],
                       "titulo": row["name"], "original_title": row["name"], "descricao": row["name"],
                       "code": row["code"], "complements": json.loads(row["complements"] or "[]"),
                       "documents": raw.get("documentos") or raw.get("documents") or [], "original": raw})
    for row in (snapshot or {}).get("data", {}).get("movements", []) or []:
        if row.get("occurred_at"):
            events.append({"id": "pje:" + str(row.get("source_id") or "unknown"), "number": number,
                           "tipo": "movimentacao", "fonte": row.get("source") or "PJe/TRF1",
                           "occurred_at": row["occurred_at"], "titulo": row.get("name") or "Movimentação do tribunal",
                           "original_title": row.get("name"), "descricao": row.get("description"),
                           "complements": row.get("complements") or [], "documents": row.get("documents") or []})
    count = len(reconcile_movements(events))
    communications = repo.db.execute("SELECT payload FROM communications WHERE org=? AND number=?",
                                     (org, number)).fetchall()
    for row in communications:
        try:
            item = json.loads(row[0])
        except (ValueError, TypeError):
            continue
        count += bool(item.get("published_at") or item.get("available_at"))
        count += bool(item.get("hearing_at") and item.get("hearing_evidence"))
    return count


def build_overview(repo, org, *, q=None, court=None, state=None, unread_only=False,
                   sort="recent", page=1, size=20, lawyer=None,
                   situation_filter=None, monitoring_status=None):
    watches = repo.list_watches(org)
    items = []
    for watch in watches:
        if court and watch["court"].casefold() != court.casefold():
            continue
        metadata = json.loads(watch.get("metadata") or "[]")
        routing = route_process(watch["number"], metadata)
        pje_applicable = routing.get("selected_connector") in ("TRF1-PJE-1G", "TRF1-PJE-JE")
        pje_compatible_unvalidated = routing.get("status") == "routed_compatible_unvalidated"
        pje_unvalidated = (routing.get("tribunal_id") == "TRF1" and not pje_applicable and
                           any(candidate.get("matches_metadata") and
                               candidate.get("degree") in set(routing.get("grades") or []) and
                               str(candidate.get("system", "")).casefold() in
                               {str(value).casefold() for value in routing.get("system_names") or []} and
                               str(candidate.get("id", "")).startswith("TRF1-PJE-")
                               for candidate in routing.get("court_candidates") or []))
        trf4 = routing.get("tribunal_id") == "TRF4"
        snapshot = (repo.latest_court_snapshot(org, watch["number"])
                    if routing.get("tribunal_id") == "TRF1" else None)
        human = repo.latest_human_verification(org, watch["number"])
        sources = [_source_summary(repo, org, watch, "DataJud"),
                   _source_summary(repo, org, watch, "DJEN")]
        if pje_applicable or snapshot:
            sources.append(_source_summary(repo, org, watch, "PJe/TRF1",
                                           applicable=pje_applicable, snapshot=snapshot, human=human))
        statuses = [item["status"] for item in sources if item["applicable"]]
        if "human" in statuses:
            primary = "human"
        elif "running" in statuses:
            primary = "running"
        elif "pending" in statuses:
            primary = "pending"
        elif any(value in ("failed", "partial") for value in statuses):
            primary = "failed"
        elif "completed" in statuses:
            primary = "updated"
        else:
            primary = "not_queried"
        if state and primary != state:
            continue
        unread = repo.unread_summary(org, watch["number"])
        if unread_only and not unread["unread_count"]:
            continue
        latest_attempts = [item["last_attempt_at"] for item in sources if item["last_attempt_at"]]
        latest_successes = [item["last_success_at"] for item in sources if item["last_success_at"]]
        latest_group = repo.latest_collection_group(org, watch["number"])
        source_jobs = [row for row in latest_group if row["source"] != "Consolidation"]
        last_job = max(source_jobs, key=lambda item: item.get("id") or 0, default=None)
        meta = max(metadata, key=lambda item: item.get("dataHoraUltimaAtualizacao") or "", default={})
        event_count = _consolidated_count(repo, org, watch["number"], snapshot)
        schedule = repo.monitor_schedule(org, watch["number"])
        direct_data = (snapshot or {}).get("data", {})
        people_snapshots = [snapshot] if snapshot else []
        if trf4:
            for source_name in ("eproc/TRF4", "TRF4 — importação assistida"):
                people_snapshots.extend(repo.court_snapshots(org, watch["number"], source_name))
        people = canonical_participants(repo, org, watch["number"],
                    court_snapshots=people_snapshots, metadata_records=metadata)
        people = presentation_participants(people)
        # A communication recipient is not promoted to a court party without an
        # independent procedural-party record from a source.
        party_rows = [row for row in people if "parte" in
                      set(row.get("kinds") or [row.get("kind")])]
        parties = sorted({row["original_name"] for row in party_rows if row.get("original_name")}, key=str.casefold)
        active_parties = sorted({row["original_name"] for row in party_rows
                                 if row.get("original_name")
                                 and "parte" in (row.get("kinds") or [row.get("kind")])
                                 and _active_party(row)}, key=str.casefold)
        lawyers = sorted({row["original_name"] for row in people
                          if professional_kind(row) and row.get("original_name")},
                         key=str.casefold)
        party_summary = participant_summary(people)
        movement_rows = repo.timeline(org, watch["number"])
        situation_data = direct_data
        situation_source = "PJe/TRF1" if snapshot else None
        situation_collected_at = snapshot.get("collected_at") if snapshot else None
        if trf4:
            options = [repo.latest_court_snapshot(org, watch["number"], source_name)
                       for source_name in ("eproc/TRF4", "TRF4 — importação assistida")]
            options = [item for item in options if item]
            if options:
                selected_snapshot = max(options, key=lambda item: str(item.get("collected_at") or ""))
                situation_data = selected_snapshot["data"]
                situation_source = situation_data.get("source") or (
                    "TRF4 — importação assistida" if "eproc" not in str(situation_data.get("system") or "") else "eproc/TRF4")
                situation_collected_at = selected_snapshot.get("collected_at")
        situation_view = process_metadata(watch, movement_rows, source_data=situation_data,
                            source_label=situation_source, source_collected_at=situation_collected_at,
                            communications=repo.communications(org, watch["number"]))
        meta = {**meta, "classe": situation_view.get("classe"),
                "assuntos": situation_view.get("assuntos") or [],
                "grau": situation_view.get("grau"), "orgaoJulgador": situation_view.get("orgao")}
        situation = situation_view.get("situacao")
        pje_movements = direct_data.get("movements", []) or []
        last_movement = max(
            [(row.get("when_at"), row.get("name")) for row in movement_rows if row.get("when_at")]
            + [(row.get("occurred_at"), row.get("name") or row.get("description"))
               for row in pje_movements if row.get("occurred_at")],
            key=lambda value: value[0], default=(None, None))
        from .cnj import format_number
        query_text = " ".join([watch["number"], format_number(watch["number"]), watch["court"],
                               str((meta.get("classe") or {}).get("nome", "") if isinstance(meta.get("classe"), dict) else meta.get("classe") or ""),
                               *parties, *lawyers]).casefold()
        if q and q.casefold() not in query_text:
            continue
        if lawyer and lawyer.casefold() not in {value.casefold() for value in lawyers}:
            continue
        if situation_filter and (not situation or situation_filter.casefold() != situation.casefold()):
            continue
        if monitoring_status == "active" and not schedule.get("enabled"):
            continue
        if monitoring_status == "inactive" and schedule.get("enabled"):
            continue
        from .cnj import format_number
        items.append({"number": watch["number"], "formatted_number": format_number(watch["number"]),
                      "court": watch["court"], "interval": watch["interval"], "created_at": watch["created_at"],
                      "classe": (meta.get("classe") or {}).get("nome") if isinstance(meta.get("classe"), dict) else meta.get("classe"),
                      "assuntos": meta.get("assuntos") or [], "grau": meta.get("grau"),
                      "sistema": (meta.get("sistema") or {}).get("nome") if isinstance(meta.get("sistema"), dict) else meta.get("sistema"),
                      "parties": parties, "active_parties": active_parties, "lawyers": lawyers,
                      "party_summary": party_summary,
                      "active_party_count": len(active_parties),
                      "situation": situation,
                      "situation_status": situation_view.get("situacao_status"),
                      "situation_state": situation_view.get("situacao_estado"),
                      "situation_source": situation_view.get("situacao_fonte"),
                      "situation_evidence": situation_view.get("situacao_derivada"),
                      "attribute_provenance": situation_view.get("proveniencia_atributos"),
                      "attribute_conflicts": situation_view.get("conflitos_atributos"),
                      "last_movement": {"at": last_movement[0], "description": last_movement[1]} if last_movement[0] else None,
                      "last_attempt_at": max(latest_attempts, default=None),
                      "last_success_at": max(latest_successes, default=watch.get("last_checked") if watch.get("metadata") else None),
                      "last_source": last_job.get("source") if last_job else None,
                      "direct_court_status": ("compatible_unvalidated" if pje_compatible_unvalidated else
                          "operational" if pje_applicable else
                          "not_validated" if pje_unvalidated else
                          "not_verified" if routing.get("status") in ("system_unconfirmed", "ambiguous", "tribunal_mismatch")
                          else "not_implemented" if routing.get("tribunal_id") else "not_verified"),
                      "direct_court_message": ("Formulário e conector compatíveis com PJe/TRF1; coleta neste grau ainda não validada independentemente"
                          if pje_compatible_unvalidated else None if pje_applicable else
                          "Conector PJe/TRF1 implementado, mas esta combinação de sistema e grau ainda não foi validada para coleta automática" if pje_unvalidated else
                          "Sistema e grau do tribunal ainda não confirmados" if routing.get("status") in ("system_unconfirmed", "ambiguous", "tribunal_mismatch")
                          else "Consulta direta ao tribunal ainda não implementada" if routing.get("tribunal_id") else
                          "Sistema e grau do tribunal ainda não confirmados"),
                      "state": primary,
                      "unread_count": unread["unread_count"],
                      "attention": any(value in ("failed", "partial", "human") for value in statuses),
                      "next_due": watch.get("next_due"), "event_count": event_count,
                      "schedule": schedule,
                      "sources": sources, "jobs": [{"source": row["source"], "status": row["status"],
                          "scheduled_at": row["scheduled_at"], "started_at": row["started_at"],
                          "completed_at": row["completed_at"]} for row in latest_group]})
    available_lawyers = sorted({lawyer for item in items for lawyer in item["lawyers"]}, key=str.casefold)
    available_situations = sorted({item["situation"] for item in items if item["situation"]}, key=str.casefold)
    if sort == "number":
        items.sort(key=lambda item: item["number"])
    elif sort == "court":
        items.sort(key=lambda item: (item["court"], item["number"]))
    elif sort == "situation":
        items.sort(key=lambda item: (item["situation"] or "", item["number"]))
    elif sort == "movement":
        items.sort(key=lambda item: _time_key((item.get("last_movement") or {}).get("at")), reverse=True)
    else:
        items.sort(key=lambda item: _time_key(item["last_attempt_at"]), reverse=True)
    stats = {key: sum(item["state"] == key for item in items) for key in
             ("updated", "pending", "running", "not_queried")}
    stats["failed"] = sum(any(source["status"] in ("failed", "partial") for source in item["sources"])
                          for item in items)
    stats["human"] = sum(any(source["status"] == "human" for source in item["sources"])
                         for item in items)
    stats["attention"] = sum(item["attention"] for item in items)
    stats["unread_total"] = sum(item["unread_count"] for item in items)
    stats["processes_with_unread"] = sum(item["unread_count"] > 0 for item in items)
    stats["monitoring_active"] = sum(bool(item["schedule"] and item["schedule"].get("enabled")) for item in items)
    stats["monitoring_inactive"] = len(items) - stats["monitoring_active"]
    known_situations = [item["situation"] for item in items if item["situation"]]
    stats["situation_count"] = len(known_situations)
    stats["closed_confirmed"] = sum(item.get("situation_state") == "encerrado_confirmado" for item in items)
    total = len(items)
    start = (page - 1) * size
    return {"total": total, "page": page, "size": size,
            "pages": (total + size - 1) // size, "summary": {"total": total, **stats},
            "items": items[start:start + size],
            "courts": sorted({watch["court"] for watch in watches}),
            "lawyers": available_lawyers, "situations": available_situations}
