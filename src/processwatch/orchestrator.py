"""Scheduling policy and source ordered collection groups."""
from datetime import date, datetime, timezone
import json

from .routing import route_process
from .cnj import tribunal
from .environment_policy import require_operational_office

def _parse_time(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _fresh(checked_at, seconds):
    checked = _parse_time(checked_at)
    return bool(checked and (datetime.now(timezone.utc) - checked).total_seconds() < seconds)


def schedule_sync(repository, org, number, config, *, collection_type="initial",
                  priority=50, force=False, start=None, end=None):
    """Enqueue only stale sources; initial/manual collection never waits for I/O."""
    require_operational_office(config.environment, org)
    watch = repository.get(org, number)
    metadata = json.loads(watch.get("metadata") or "[]")
    routing = route_process(number, metadata)
    active_jobs = repository.active_collection_group(org, number)
    if active_jobs and not force:
        return {"group_id": active_jobs[0]["group_id"], "jobs": active_jobs,
                "status": "queued", "routing": routing}
    datajud_fresh = bool(watch.get("metadata") and _fresh(watch.get("last_checked"), watch["interval"]))
    latest_djen = repository.latest_source_check(org, number, "DJEN")
    djen_fresh = bool(latest_djen and latest_djen.get("status") == "período consultado"
                      and _fresh(latest_djen.get("checked_at"), config.djen_sync_interval))
    include_datajud = bool(routing["datajud_alias"] and (force or not datajud_fresh))
    include_djen = force or start is not None or end is not None or not djen_fresh
    pje_check = repository.latest_source_check(org, number, "PJe/TRF1") if routing["selected_connector"] else None
    pje_fresh = bool(pje_check and _fresh(pje_check.get("checked_at"), config.court_sync_interval))
    additional_sources = ([routing["selected_connector"]]
                          if routing["selected_connector"] and (force or not pje_fresh) else [])
    # An explicit user refresh may probe the public TRF4 form. It is deliberately
    # excluded from initial, OAB, batch and scheduled cycles until real results
    # and pagination are verified; a security challenge becomes awaiting_access.
    if collection_type == "manual_refresh" and force and tribunal(number) == "trf4":
        additional_sources.append("TRF4-EPROC-DIRECT")
    if not include_datajud and not include_djen and not additional_sources:
        return {"group_id": None, "jobs": [], "status": "fresh", "routing": routing}
    result = repository.enqueue_collection_group(
        org, number, collection_type, priority=priority,
        include_datajud=include_datajud, include_djen=include_djen,
        additional_sources=additional_sources,
        start=start.isoformat() if isinstance(start, date) else start,
        end=end.isoformat() if isinstance(end, date) else end,
        max_attempts=config.collection_max_attempts)
    result["status"] = "queued"
    result["routing"] = routing
    return result


def schedule_due(repository, config, org="local"):
    """Schedule only explicitly enabled per-process subscriptions.

    One scheduler tick coalesces missed intervals and submits no browser-only
    tribunal job. Such sources still require an explicit manual collection.
    """
    if org is None and config.environment == "production":
        raise ValueError("O scheduler de produção deve processar um escritório por ciclo.")
    if org is not None:
        require_operational_office(config.environment, org)
    repository.requeue_stale_oab_monitor_jobs(org, stale_after_seconds=7200)
    due = repository.enqueue_due_monitor_schedules(
        org, limit=10, max_attempts=config.collection_max_attempts)
    oab_due = repository.enqueue_due_oab_monitor_jobs(org, limit=10)
    return sum(item["jobs_created"] for item in due) + len(oab_due)
