"""Durable collection job execution over the existing providers and services."""
import asyncio
import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import date

from .consolidation import consolidate
from .errors import ProviderError
from .providers.contracts import CollectionOutcome
from .routing import route_process
from .cnj import tribunal
from .providers.pje_trf1 import SOURCE_IDS as PJE_SOURCE_IDS
from .providers.pje_trf1 import PJeTRF1Connector
from .providers.eproc_trf4 import EprocTRF4Connector, SOURCE_ID as TRF4_EPROC_SOURCE, SOURCE_LABEL as TRF4_EPROC_LABEL
from .providers.contracts import CollectionRequest, ConnectorState
from .trf4_assisted import SOURCE as TRF4_ASSISTED_SOURCE, merge_capture


def _failure(source, exc):
    message = str(exc)
    upper = message.upper()
    access = ("API_KEY" in upper or "CHAVE VIGENTE" in upper or
              "HTTP 401" in upper or "HTTP 403" in upper or "ACESSO RECUSADO" in upper)
    permanent_http = "HTTP 4" in upper and "HTTP 429" not in upper
    retryable = not access and not permanent_http
    return {"code": "access_required" if access else "source_error",
            "message": message[:500], "retryable": retryable,
            "awaiting_access": access}


def execute_collection_job(job, engine, repository, djen_client, config, pje_client=None):
    """Run exactly one lease; provider I/O remains outside the SQLite transaction."""
    source = job["source"]
    number = job["number"]
    org = job["org"]
    params = json.loads(job.get("params") or "{}")
    try:
        if source == "DataJud":
            result = asdict(engine.check(number, org))
            routing = route_process(number, json.loads(repository.get(org, number).get("metadata") or "[]"))
            result["routing"] = routing
            run_rows = repository.runs(org, number)
            execution = str(run_rows[-1]["id"]) if run_rows else None
            coverage = {"status": "complete", "run_id": execution,
                        "movements": len(repository.timeline(org, number))}
            repository.finish_collection_job(job["id"], job["lease_token"], status="completed",
                                             result=result, coverage=coverage,
                                             source_execution_id=execution)
            selected = routing.get("selected_connector")
            if selected in PJE_SOURCE_IDS and job.get("group_id"):
                repository.enqueue_group_source(org, number, job["group_id"], selected,
                                                priority=job.get("priority", 50),
                                                max_attempts=job.get("max_attempts", config.collection_max_attempts))
            return CollectionOutcome(source, "completed", coverage=coverage, result=result)

        if source == "DJEN":
            from .monitoring import sync_djen
            start = date.fromisoformat(params["start"]) if params.get("start") else None
            end = date.fromisoformat(params["end"]) if params.get("end") else None
            report = asyncio.run(sync_djen(repository, djen_client, org, number,
                                           initial=config.djen_initial_date, start=start, end=end,
                                           window_days=config.djen_window_days,
                                           overlap_days=config.djen_overlap_days))
            coverage = {"requested_start": report.requested_start,
                        "requested_end": report.requested_end,
                        "windows_completed": report.windows_completed,
                        "records_seen": report.records_seen,
                        "status": report.status}
            result = asdict(report)
            if report.status == "parcial":
                failure = _failure(source, RuntimeError(report.error or "DJEN cobertura parcial."))
                if failure["awaiting_access"]:
                    repository.finish_collection_job(job["id"], job["lease_token"],
                        status="awaiting_access", result=result, coverage=coverage, error=failure)
                else:
                    repository.retry_collection_job(job, code=failure["code"], message=failure["message"],
                                                    retryable=failure["retryable"], result=result,
                                                    coverage=coverage,
                                                    retry_base_seconds=config.collection_retry_base_seconds,
                                                    retry_max_seconds=config.collection_retry_max_seconds)
                return CollectionOutcome(source, "partial", report.records_seen, coverage, result=result,
                                         limitations=[report.error] if report.error else [])
            repository.finish_collection_job(job["id"], job["lease_token"], status="completed",
                                             result=result, coverage=coverage)
            return CollectionOutcome(source, "completed", report.records_seen, coverage, result=result)

        if source == "Consolidation":
            routing = route_process(number, json.loads(repository.get(org, number).get("metadata") or "[]"))
            snapshot = repository.latest_court_snapshot(org, number, "PJe/TRF1")
            snapshots = [snapshot["data"]] if snapshot else []
            events = consolidate(number, repository.timeline(org, number),
                                 repository.communications(org, number), repository, org,
                                 court_snapshots=snapshots)
            result = {"events": len(events),
                      "relations": len(repository.event_relations(org, number)),
                      "routing": routing}
            coverage = {"DataJud": bool(repository.get(org, number).get("metadata")),
                        "DJEN_checks": len(repository.source_coverage(org, number, "DJEN")),
                        "court_snapshots": len(snapshots)}
            repository.finish_collection_job(job["id"], job["lease_token"], status="completed",
                                             result=result, coverage=coverage)
            return CollectionOutcome(source, "completed", len(events), coverage, result=result)

        if source == "ESCAVADOR-API-V2":
            error = {"source": source, "code": "source_retired",
                     "message": "Esta fonte comercial foi removida do fluxo do ProcessWatch.",
                     "retryable": False}
            repository.finish_collection_job(job["id"], job["lease_token"], status="failed", error=error)
            return CollectionOutcome(source, "failed", limitations=[error["message"]])

        if source in PJE_SOURCE_IDS:
            watch = repository.get(org, number)
            routing = route_process(number, json.loads(watch.get("metadata") or "[]"))
            if routing.get("selected_connector") != source:
                raise ValueError("Roteador não confirmou a combinação TRF1/PJe/grau para este processo.")
            provider = pje_client or PJeTRF1Connector(timeout_seconds=config.timeout)
            degree = routing.get("grades", [None])[0]
            verification_id = params.get("human_verification_id")
            if verification_id:
                provider = PJeTRF1Connector(timeout_seconds=config.timeout,
                    verification_callback=repository.update_human_verification,
                    verification_cancelled=lambda: (repository.human_verification(verification_id) or {}).get("status")
                        in ("cancelled", "expired", "failed"), worker_id=job.get("lease_owner"))
            outcome = asyncio.run(provider.collect(CollectionRequest(
                number=number, source=source, collection_type="public_case",
                tribunal="TRF1", system="PJe", degree=degree, parameters=params)))
            coverage = dict(outcome.coverage)
            coverage.setdefault("state", outcome.state.value)
            if outcome.data and outcome.state in (ConnectorState.OPERATIONAL, ConnectorState.PARTIAL):
                data = dict(outcome.data)
                data["evidence"] = [asdict(item) for item in outcome.evidence]
                source_record_id = repository.save_court_snapshot(
                    org, number, "PJe/TRF1", data,
                    status="consulta concluída" if outcome.state == ConnectorState.OPERATIONAL else "consulta parcial",
                    coverage=coverage,
                    error=asdict(outcome.error) if outcome.error else None)
            else:
                # A failed lookup is coverage/diagnostic state, never a new empty
                # snapshot that could hide the last valid public case result.
                data = None
                source_record_id = None
                repository.record_court_attempt(
                    org, number, "PJe/TRF1", status=outcome.state.value,
                    coverage=coverage, error=asdict(outcome.error) if outcome.error else None)
            result = {"source": "PJe/TRF1", "state": outcome.state.value,
                      "found": outcome.found, "records_seen": outcome.coverage.get("movement_count", 0),
                      "movements_seen": len((outcome.data or {}).get("movements", [])),
                      "participants_seen": len((outcome.data or {}).get("participants", [])),
                      "documents_seen": len((outcome.data or {}).get("documents", [])),
                      "source_record_id": source_record_id, "coverage": coverage,
                      "limitations": list(outcome.limitations)}
            if outcome.state == ConnectorState.OPERATIONAL:
                repository.finish_collection_job(job["id"], job["lease_token"], status="completed",
                                                 result=result, coverage=coverage,
                                                 source_execution_id=job.get("execution_id"))
                return CollectionOutcome(source, "completed", result["records_seen"], coverage, result=result)
            if outcome.state == ConnectorState.PARTIAL:
                repository.finish_collection_job(job["id"], job["lease_token"], status="partial",
                                                 result=result, coverage=coverage,
                                                 source_execution_id=job.get("execution_id"),
                                                 error=asdict(outcome.error) if outcome.error else None)
                return CollectionOutcome(source, "partial", result["records_seen"], coverage, result=result,
                                         limitations=list(outcome.limitations))
            if outcome.state == ConnectorState.PROCESS_NOT_FOUND:
                repository.finish_collection_job(job["id"], job["lease_token"], status="completed",
                                                 result=result, coverage=coverage,
                                                 source_execution_id=job.get("execution_id"))
                return CollectionOutcome(source, "process_not_found", 0, coverage, result=result)
            error = asdict(outcome.error) if outcome.error else {
                "source": source, "code": outcome.state.value, "message": "A consulta pública não foi concluída.",
                "state": outcome.state.value, "retryable": False, "stage": None}
            if outcome.state in (ConnectorState.CAPTCHA, ConnectorState.AUTHENTICATION_REQUIRED,
                                 ConnectorState.UNAUTHORIZED):
                repository.finish_collection_job(job["id"], job["lease_token"], status="awaiting_access",
                                                 result=result, coverage=coverage, error=error)
            elif error.get("retryable"):
                is_court = source in PJE_SOURCE_IDS
                repository.retry_collection_job(job, code=error["code"], message=error["message"], retryable=True,
                                                result=result, coverage=coverage,
                                                retry_base_seconds=(config.court_retry_base_seconds if is_court else config.collection_retry_base_seconds),
                                                retry_max_seconds=(config.court_retry_max_seconds if is_court else config.collection_retry_max_seconds))
            else:
                repository.finish_collection_job(job["id"], job["lease_token"], status="failed",
                                                 result=result, coverage=coverage, error=error)
            return CollectionOutcome(source, outcome.state.value, coverage=coverage, result=result,
                                     limitations=[error["message"]])

        if source == TRF4_EPROC_SOURCE:
            authorized_capture = params.get("authorized_capture")
            if authorized_capture:
                if tribunal(number) != "trf4" or authorized_capture.get("number") != number:
                    raise ValueError("A captura autorizada não corresponde ao processo TRF4 do job.")
                if authorized_capture.get("source") != TRF4_ASSISTED_SOURCE:
                    raise ValueError("A origem da captura autorizada não foi confirmada.")
                previous = repository.latest_court_snapshot(org, number, TRF4_ASSISTED_SOURCE)
                merged = merge_capture(previous["data"] if previous else None, authorized_capture)
                baseline = previous is None
                snapshot_id = repository.save_court_snapshot(
                    org, number, TRF4_ASSISTED_SOURCE, merged,
                    status="captura autorizada processada pelo worker",
                    coverage=merged.get("coverage"), baseline_if_first=baseline)
                repository.record_access("trf4_assisted_worker_import", number,
                    "captura da consulta pública confirmada pelo usuário no navegador oficial")
                coverage = dict(merged.get("coverage") or {})
                coverage.update({"status": "completed", "capture_mode": "authorized_browser_session",
                                 "human_session": True, "complete": False})
                result = {"source": TRF4_ASSISTED_SOURCE, "state": "completed",
                          "capture_mode": "authorized_browser_session", "snapshot_id": snapshot_id,
                          "records_seen": len(merged.get("movements", [])), "baseline": baseline,
                          "pagination_status": coverage.get("pagination_status"), "complete": False}
                compact_params = {"authorized_capture_processed": True,
                    "capture_sha256": hashlib.sha256(json.dumps(authorized_capture,
                        ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                    "capture_count": result["records_seen"]}
                repository.finish_collection_job(job["id"], job["lease_token"], status="completed",
                    result=result, coverage=coverage, source_execution_id=job.get("execution_id"),
                    params=compact_params)
                return CollectionOutcome(source, "completed", result["records_seen"], coverage, result=result)
            # This source is scheduled only by an explicit manual refresh. It is
            # intentionally absent from recurring scheduler and initial imports.
            if tribunal(number) != "trf4":
                raise ValueError("O conector eproc/TRF4 não aceita processo de outro tribunal.")
            provider = EprocTRF4Connector(timeout_seconds=config.timeout)
            outcome = asyncio.run(provider.collect(CollectionRequest(
                number=number, source=source, collection_type="public_case",
                tribunal="TRF4", system="eproc", degree=None, parameters=params)))
            coverage = dict(outcome.coverage)
            coverage.setdefault("state", outcome.state.value)
            source_record_id = None
            if outcome.data and outcome.state in (ConnectorState.OPERATIONAL, ConnectorState.PARTIAL):
                data = dict(outcome.data)
                data["evidence"] = [asdict(item) for item in outcome.evidence]
                source_record_id = repository.save_court_snapshot(
                    org, number, TRF4_EPROC_LABEL, data,
                    status="consulta concluída" if outcome.state == ConnectorState.OPERATIONAL else "consulta parcial",
                    coverage=coverage, error=asdict(outcome.error) if outcome.error else None,
                    baseline_if_first=True)
            else:
                repository.record_court_attempt(
                    org, number, TRF4_EPROC_LABEL, status=outcome.state.value,
                    coverage=coverage, error=asdict(outcome.error) if outcome.error else None)
            result = {"source": TRF4_EPROC_LABEL, "state": outcome.state.value,
                      "found": outcome.found,
                      "records_seen": len((outcome.data or {}).get("movements", [])),
                      "source_record_id": source_record_id, "coverage": coverage,
                      "limitations": list(outcome.limitations)}
            if outcome.state == ConnectorState.OPERATIONAL:
                repository.finish_collection_job(job["id"], job["lease_token"], status="completed",
                    result=result, coverage=coverage, source_execution_id=job.get("execution_id"))
                return CollectionOutcome(source, "completed", result["records_seen"], coverage, result=result)
            error = asdict(outcome.error) if outcome.error else {
                "source": source, "code": outcome.state.value,
                "message": "A consulta pública do TRF4 não foi concluída.",
                "state": outcome.state.value, "retryable": False, "stage": None}
            if outcome.state in (ConnectorState.CAPTCHA, ConnectorState.HUMAN_VERIFICATION,
                                 ConnectorState.AUTHENTICATION_REQUIRED, ConnectorState.UNAUTHORIZED):
                repository.finish_collection_job(job["id"], job["lease_token"], status="awaiting_access",
                    result=result, coverage=coverage, error=error)
            elif outcome.state == ConnectorState.PARTIAL:
                repository.finish_collection_job(job["id"], job["lease_token"], status="partial",
                    result=result, coverage=coverage, error=error)
            elif error.get("retryable"):
                repository.retry_collection_job(job, code=error["code"], message=error["message"],
                    retryable=True, result=result, coverage=coverage,
                    retry_base_seconds=config.court_retry_base_seconds,
                    retry_max_seconds=config.court_retry_max_seconds)
            else:
                repository.finish_collection_job(job["id"], job["lease_token"], status="failed",
                    result=result, coverage=coverage, error=error)
            return CollectionOutcome(source, outcome.state.value, coverage=coverage, result=result,
                                     limitations=[error["message"]])

        raise ValueError("Fonte não reconhecida pelo executor local.")
    except Exception as exc:
        failure = _failure(source, exc)
        if failure["awaiting_access"]:
            repository.finish_collection_job(job["id"], job["lease_token"],
                                             status="awaiting_access", error=failure)
        else:
            is_court = source in PJE_SOURCE_IDS
            repository.retry_collection_job(job, code=failure["code"], message=failure["message"],
                                            retryable=failure["retryable"],
                                            retry_base_seconds=(config.court_retry_base_seconds if is_court else config.collection_retry_base_seconds),
                                            retry_max_seconds=(config.court_retry_max_seconds if is_court else config.collection_retry_max_seconds))
        return CollectionOutcome(source, "failed", limitations=[failure["message"]])


class SQLiteCollectionJobRunner:
    """Small adapter so future HTTP/authorized browser runners share the lease contract."""
    def __init__(self, engine, repository, djen_client, config, pje_client=None):
        self.engine, self.repository, self.djen_client, self.config = engine, repository, djen_client, config
        self.pje_client = pje_client

    def run(self, job):
        return execute_collection_job(job, self.engine, self.repository, self.djen_client,
                                      self.config, self.pje_client)


def new_worker_id():
    return "local-" + uuid.uuid4().hex
