"""Local HTTP API and HTML interface over the independent process engine."""
import json
import hashlib
import hmac
import time
import secrets
import ipaddress
import sqlite3
from contextvars import ContextVar
from collections import defaultdict, deque
from dataclasses import asdict
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .config import Config
from .database_paths import validate_database_path
from .environment_policy import is_local_office_blocked
from .engine import Engine
from .providers.datajud import DataJud
from .repository import SQLiteRepository, now
from .cnj import format_number, normalize, tribunal
from .consolidation import consolidate, process_metadata, _safe_documents
from .providers.djen import DjenClient, DjenError
from .providers.pje import UnavailablePJeProvider
from .monitoring import sync_djen
from .discovery import by_oab, discover_oab
from .orchestrator import schedule_sync
from .routing import route_process, public_query_url
from .dashboard import build_overview
from .participants import canonical_participants, presentation_participants, professional_kind
from .trf4_assisted import SOURCE as TRF4_ASSISTED_SOURCE, normalize_capture, merge_capture
from .providers.eproc_trf4 import SOURCE_ID as TRF4_EPROC_SOURCE, SOURCE_LABEL as TRF4_EPROC_LABEL
from .sqlite_metrics import current_sqlite_metrics, instrument_repository
from .error_sanitizer import is_sensitive_field, sanitize_error


class RegisterRequest(BaseModel):
    number: str
    interval: int | None = Field(default=None, gt=0)
    fetch: bool = Field(default=True, description="Compatibilidade: a coleta sempre é enfileirada sem bloquear a resposta.")


class RefreshRequest(BaseModel):
    inicio: date | None = None
    fim: date | None = None


class OabSearchRequest(BaseModel):
    oab: str
    uf: str
    inicio: date
    fim: date


class OabAddRequest(OabSearchRequest):
    numbers: list[str] = Field(min_length=1, max_length=50)
    portfolio: str = Field(min_length=1, max_length=100)
    interval: int | None = Field(default=None, gt=0)


class OabMonitorCreateRequest(BaseModel):
    oab: str = Field(min_length=1, max_length=12)
    uf: str = Field(min_length=2, max_length=2)
    lawyer_name: str | None = Field(default=None, max_length=160)
    enabled: bool = True
    interval_hours: int = Field(default=24, ge=6, le=48)


class OabMonitorUpdateRequest(BaseModel):
    enabled: bool | None = None
    interval_hours: int | None = Field(default=None, ge=6, le=48)
    lawyer_name: str | None = Field(default=None, max_length=160)


class CommunicationReadRequest(BaseModel):
    read: bool


class MonitorScheduleRequest(BaseModel):
    enabled: bool
    interval_hours: int = Field(default=24, ge=6, le=48)


class CalendarItemCreateRequest(BaseModel):
    item_type: str
    title: str = Field(min_length=1, max_length=180)
    starts_at: str = Field(min_length=10, max_length=40)
    ends_at: str | None = Field(default=None, max_length=40)
    number: str | None = Field(default=None, max_length=25)
    responsible: str | None = Field(default=None, max_length=160)
    notes: str = Field(default="", max_length=4000)
    status: str | None = None


class CalendarItemUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=180)
    starts_at: str | None = Field(default=None, max_length=40)
    ends_at: str | None = Field(default=None, max_length=40)
    number: str | None = Field(default=None, max_length=25)
    responsible: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=4000)
    status: str | None = None


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=1024)
    office_id: str | None = None


class PlatformAdminCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254, description="E-mail de acesso do novo administrador global.")
    password: str = Field(min_length=12, max_length=1024, description="Senha inicial; nunca retornada pela API.")


class PlatformAdminUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=254, description="Novo e-mail de acesso.")


class PlatformAdminStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool


class PlatformAdminPasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class PlatformAdminPasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_password: str = Field(min_length=12, max_length=1024,
                              description="Nova senha inicial; nunca retornada pela API.")


class PlatformOfficeCreateRequest(BaseModel):
    office_id: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=160)
    email: str = Field(min_length=3, max_length=254)
    phone: str = Field(default="", max_length=40)
    address: str = Field(default="", max_length=500)
    administrator_email: str = Field(min_length=3, max_length=254)
    administrator_name: str = Field(min_length=1, max_length=160)
    administrator_password: str = Field(min_length=12, max_length=1024)


class PlatformOfficeStatusRequest(BaseModel):
    active: bool


class PlatformOfficeUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160,
                             description="Nome cadastral do escritório.")
    email: str | None = Field(default=None, max_length=254,
                              description="E-mail de contato; envie texto vazio para limpar.")
    phone: str | None = Field(default=None, max_length=40,
                              description="Telefone de contato; envie texto vazio para limpar.")
    address: str | None = Field(default=None, max_length=500,
                                description="Endereço; envie texto vazio para limpar.")


class PlatformAccessPurposeRequest(BaseModel):
    purpose: str = Field(min_length=8, max_length=240)


class SelectOfficeRequest(BaseModel):
    office_id: str


class OfficeUserCreateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, description="E-mail da conta a criar ou vincular.")
    full_name: str = Field(default="", max_length=160,
                           description="Nome completo da conta nova ou nome exibido neste vínculo.")
    role: str = Field(default="lawyer", description="Papel neste escritório: lawyer ou office_admin.")
    temporary_password: str | None = Field(default=None, min_length=12, max_length=1024,
        description="Obrigatória somente para conta nova; omita ao vincular conta existente.")


class OfficeUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    email: str = Field(default="", max_length=254)
    phone: str = Field(default="", max_length=40)
    address: str = Field(default="", max_length=500)


class OfficeUserUpdateRequest(BaseModel):
    role: str | None = None
    active: bool | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=160)


class PlatformOfficeUserUpdateRequest(BaseModel):
    role: str | None = None
    active: bool | None = Field(default=None, description="Estado do vínculo somente neste escritório.")
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    account_active: bool | None = Field(default=None,
        description="Estado da conta global; afeta o acesso do usuário a todos os escritórios.")


class AccountDeleteRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)


class OfficeLifecycleRequest(BaseModel):
    office_id: str = Field(min_length=2, max_length=64)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)


class RecoveryRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class RecoveryCompleteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    token: str = Field(min_length=20, max_length=256)
    new_password: str = Field(min_length=12, max_length=1024)


class ProfileUpdateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=160)


SERVICE_PERMISSIONS = frozenset({
    "processes:read", "communications:read", "oab:read", "oab:manage",
    "sync:request", "jobs:read", "communications:read_state:write",
})


class ServiceCredentialCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    permissions: list[str] = Field(min_length=1, max_length=len(SERVICE_PERMISSIONS))
    expires_in_days: int = Field(default=90, ge=1, le=365)


class ServiceCredentialRotateRequest(BaseModel):
    expires_in_days: int = Field(default=90, ge=1, le=365)


def service_permissions_for(path, method):
    """Closed permission map for service-token requests; unmatched routes deny by default."""
    method, path = method.upper(), path.rstrip("/") or "/"
    if path == "/api/scheduler/status" and method == "GET":
        return {"jobs:read"}
    if path.startswith("/api/monitoramentos/oab"):
        if path.endswith("/comunicacoes") and method == "GET":
            return {"oab:read", "communications:read"}
        if path.endswith("/atualizar") and method == "POST":
            return {"oab:read", "sync:request"}
        if path == "/api/monitoramentos/oab":
            return {"oab:manage"} if method == "POST" else {"oab:read"} if method == "GET" else None
        if method == "GET":
            return {"oab:read"}
        if method in ("PATCH", "DELETE"):
            return {"oab:manage"}
    if path.startswith("/api/discovery/oab/"):
        if path.endswith("/results") and method == "GET":
            return {"oab:read"}
        if path.endswith("/search") and method == "POST":
            return {"oab:read", "sync:request"}
        if path.endswith("/add") and method == "POST":
            return {"oab:manage", "sync:request"}
    if path == "/api/communications" and method == "GET":
        return {"communications:read"}
    if path.startswith("/api/communications/"):
        if path.endswith("/read") and method == "PATCH":
            return {"communications:read_state:write"}
        if method == "GET":
            return {"communications:read"}
    if path == "/api/processes" and method == "GET":
        return {"processes:read"}
    # Registering a new case changes the monitored portfolio. Service credentials
    # can request refreshes, but portfolio mutation remains a human-session action.
    if path == "/api/processes" and method == "POST":
        return None
    if path == "/api/processes/overview" and method == "GET":
        return {"processes:read"}
    if path == "/api/processes/check-all" and method == "POST":
        return {"sync:request"}
    if path.startswith("/api/processes/"):
        if path.endswith("/sync") or path.endswith("/runs"):
            return {"jobs:read"} if method == "GET" else None
        if path.endswith("/schedule"):
            return {"jobs:read"} if method == "GET" else {"sync:request"} if method == "PUT" else None
        if path.endswith("/check") and method == "POST":
            return {"sync:request"}
        if method == "GET":
            return {"processes:read"}
    if path.startswith("/api/processos/"):
        if path.endswith("/intimacoes"):
            return {"communications:read"} if method == "GET" else None
        if path.endswith("/eventos-nao-lidos/ler-todos") or "/eventos/" in path and path.endswith("/ler"):
            return {"communications:read_state:write"} if method == "POST" else None
        if path.endswith("/atualizar") or path.endswith("/pje/retomar") or "/pje/retomada/" in path:
            return {"sync:request"} if method == "POST" else None
        if path.endswith("/timeline") and method == "GET":
            return {"processes:read", "communications:read"}
        if method == "GET":
            return {"processes:read"}
    if path in ("/api/calendar", "/api/reports/summary", "/api/processes/activity") and method == "GET":
        return {"processes:read"}
    return None


def create_app(repository=None, engine=None, org="local", config=None, scheduler_active=False,
               scheduler_status_provider=None,
               djen_client=None, pje_provider=None):
    config = config or Config.from_env()
    owns_repository = repository is None
    repository = repository or SQLiteRepository(
        config.db, environment=config.environment, primary_path=config.primary_db or None)
    db_entry = next((row for row in repository.db.execute("PRAGMA database_list") if row[1] == "main"), None)
    if db_entry:
        validate_database_path(db_entry[2] or ":memory:", environment=config.environment,
                                primary_path=config.primary_db or None)
    instrument_repository(repository)
    engine = engine or Engine(repository, DataJud(config))
    djen_client = djen_client or DjenClient(min_interval=config.djen_min_interval)
    pje_provider = pje_provider or UnavailablePJeProvider()
    app = FastAPI(title="ProcessWatch", version="0.2.0", docs_url=None, redoc_url=None,
                  openapi_url="/openapi.json")
    extension_origin = f"chrome-extension://{config.chrome_extension_id}"
    app.add_middleware(CORSMiddleware, allow_origins=[extension_origin],
                       allow_methods=["GET", "POST"],
                       allow_headers=["Authorization", "Content-Type", "X-ProcessWatch-Nonce"],
                       allow_credentials=False, max_age=300)
    app.state.repository = repository
    app.state.engine = engine
    app.state.org = org
    tenant_context = ContextVar(f"processwatch_org_{id(app)}", default=org)
    identity_context = ContextVar(f"processwatch_identity_{id(app)}", default=None)
    platform_identity_context = ContextVar(f"processwatch_platform_identity_{id(app)}", default=None)
    platform_session_context = ContextVar(f"processwatch_platform_session_{id(app)}", default=None)
    session_context = ContextVar(f"processwatch_session_{id(app)}", default=None)
    service_context = ContextVar(f"processwatch_service_{id(app)}", default=None)
    service_audit_logged = ContextVar(f"processwatch_service_audit_{id(app)}", default=False)
    auth_kind_context = ContextVar(f"processwatch_auth_kind_{id(app)}", default=None)

    def current_org():
        return tenant_context.get()

    def current_identity():
        return identity_context.get()

    def current_platform_identity():
        return platform_identity_context.get()

    def require_platform_admin():
        identity = current_platform_identity()
        if not identity:
            raise HTTPException(status_code=403, detail="Ação permitida somente à administração da plataforma.")
        return identity

    def audit_platform(action, *, target_org=None, number=None, purpose):
        identity = require_platform_admin()
        repository.record_access(action, number, purpose,
                                 actor=f"platform_admin:{identity['email']}", org=target_org)

    def sanitize_job_diagnostics(value):
        if isinstance(value, dict):
            return {key: "[redigido]" if is_sensitive_field(key) else sanitize_error(item)
                    if key.casefold() in
                    {"error", "last_error", "monitor_error", "result"} else sanitize_job_diagnostics(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [sanitize_job_diagnostics(item) for item in value]
        return value

    def require_office_admin():
        identity = current_identity()
        if not identity or identity.get("role") != "office_admin":
            raise HTTPException(status_code=403, detail="Ação permitida somente ao administrador deste escritório.")
        return identity

    def record_access(action, number, purpose):
        identity = current_identity()
        service = service_context.get()
        if service:
            service_audit_logged.set(True)
        actor = (f"service:{service['id']}" if service else
                 identity["email"] if identity else "legacy-bearer")
        repository.record_access(action, number, purpose, actor=actor, org=current_org())

    def require_credential_administrator():
        identity = current_identity()
        kind = auth_kind_context.get()
        if identity and identity.get("role") == "office_admin":
            return identity.get("email")
        if config.auth_mode == "token" and kind == "legacy_bearer":
            return "legacy-bearer-admin"
        raise HTTPException(status_code=403, detail="Somente um administrador autenticado pode gerir credenciais de serviço.")
    app.state.scheduler_status_provider = scheduler_status_provider
    requests_by_ip = defaultdict(deque)
    login_attempts_by_ip = defaultdict(deque)
    app.state.extension_nonces = {}

    def _scheduler_running():
        if scheduler_status_provider is not None:
            try:
                return bool(scheduler_status_provider())
            except Exception:
                return False
        return bool(scheduler_active)

    def _platform_worker_telemetry():
        """Expose live worker state only when this app has a runtime probe."""
        provider = scheduler_status_provider
        if provider is None:
            return {"state": "unknown", "telemetry_available": False,
                    "evidence": "Esta instância não recebeu um provedor de estado em tempo real."}
        try:
            observed = provider()
        except Exception:
            return {"state": "unknown", "telemetry_available": False,
                    "evidence": "O provedor de estado em tempo real não respondeu."}
        if isinstance(observed, dict):
            state = observed.get("state")
            if state not in ("running", "stopped", "unknown"):
                state = "unknown"
            return {**observed, "state": state, "telemetry_available": state != "unknown",
                    "observed_at": now()}
        if isinstance(observed, bool):
            return {"state": "running" if observed else "stopped", "telemetry_available": True,
                    "evidence": "Estado atual informado pelo provedor associado ao worker local.",
                    "observed_at": now()}
        return {"state": "unknown", "telemetry_available": False,
                "evidence": "O provedor não retornou um estado reconhecido."}

    def _scheduler_summary():
        running = _scheduler_running()
        row = repository.db.execute("""SELECT
            sum(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled_count,
            sum(CASE WHEN enabled=1 AND next_run_at<=? THEN 1 ELSE 0 END) AS due_count
            FROM monitor_schedules WHERE org=?""", (now(), current_org())).fetchone()
        oab_row = repository.db.execute("""SELECT sum(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled_count,
            sum(CASE WHEN enabled=1 AND next_run_at<=? THEN 1 ELSE 0 END) AS due_count
            FROM oab_monitors WHERE org=?""", (now(), current_org())).fetchone()
        active_jobs = repository.db.execute("""SELECT count(*) FROM collection_jobs
            WHERE org=? AND collection_type='scheduled_refresh' AND status IN ('queued','running')""",
            (current_org(),)).fetchone()[0]
        telemetry = _platform_worker_telemetry()
        return {"worker_running": running, "scheduler_running": running,
                "worker_telemetry": telemetry,
                "enabled_processes": row["enabled_count"] or 0, "due_processes": row["due_count"] or 0,
                "enabled_oab_monitors": oab_row["enabled_count"] or 0,
                "due_oab_monitors": oab_row["due_count"] or 0,
                "scheduled_jobs_active": active_jobs,
                "message": "O serviço local está consultando a fila." if running
                    else "O serviço local está parado; horários vencidos serão recuperados quando ele iniciar."}

    def _routing(number, watch=None):
        watch = watch or repository.get(current_org(), number)
        return route_process(number, json.loads(watch.get("metadata") or "[]"))

    def _trf4_snapshot(number):
        try:
            if tribunal(normalize(number)) != "trf4":
                return None
        except ValueError:
            return None
        return (repository.latest_court_snapshot(current_org(), number, TRF4_EPROC_LABEL)
                or _trf4_assisted_snapshot(number))

    def _trf4_assisted_snapshot(number):
        try:
            if tribunal(normalize(number)) != "trf4":
                return None
        except ValueError:
            return None
        return repository.latest_court_snapshot(current_org(), number, TRF4_ASSISTED_SOURCE)

    def _trf1_snapshot(number):
        """Read a persisted public PJe cover independently of new-query routing."""
        try:
            if tribunal(normalize(number)) != "trf1":
                return None
        except ValueError:
            return None
        return repository.latest_court_snapshot(current_org(), number, "PJe/TRF1")

    def _timeline_court_snapshots(number, *, include_pje=False):
        snapshots = []
        pje_snapshot = _trf1_snapshot(number) if include_pje or tribunal(normalize(number)) == "trf1" else None
        if pje_snapshot:
            snapshots.append(dict(pje_snapshot["data"], collected_at=pje_snapshot["collected_at"]))
        direct_snapshot = repository.latest_court_snapshot(current_org(), number, TRF4_EPROC_LABEL)
        for trf4_snapshot in (_trf4_assisted_snapshot(number), direct_snapshot):
            if trf4_snapshot:
                snapshots.append(dict(trf4_snapshot["data"], collected_at=trf4_snapshot["collected_at"]))
        return snapshots

    def _require_extension_origin(request):
        expected = f"chrome-extension://{config.chrome_extension_id}"
        origin = request.headers.get("origin", "")
        if not config.chrome_extension_id or not hmac.compare_digest(origin, expected):
            raise HTTPException(status_code=403, detail="Origem da extensão ProcessWatch não autorizada.")
        client_host = request.client.host if request.client else ""
        try:
            if not ipaddress.ip_address(client_host).is_loopback:
                raise HTTPException(status_code=403, detail="Importação permitida somente no computador local.")
        except ValueError:
            raise HTTPException(status_code=403, detail="Origem local inválida.") from None

    def _pje_applies(routing):
        return (routing.get("tribunal_id") == "TRF1" and
                routing.get("selected_connector") in ("TRF1-PJE-1G", "TRF1-PJE-JE"))

    def _direct_court_status(routing, check=None, snapshot=None):
        if _pje_applies(routing):
            state_map = {"consulta concluída": "completed", "consulta parcial": "partial",
                         "consulta parcialmente concluída": "partial", "captcha_identified": "awaiting_access",
                         "authentication_required": "awaiting_access", "unauthorized": "awaiting_access",
                         "process_not_found": "completed", "layout_incompatible": "failed",
                         "court_unavailable": "failed", "timeout": "failed",
                         "err_empty_response": "failed", "rate_limited": "failed"}
            return {"source": "PJe/TRF1", "status": state_map.get(check.get("status"), "completed" if snapshot else "not_started")
                    if check else ("completed" if snapshot else "not_started"),
                    "message": ("Formulário compartilhado com o PJe/TRF1 validado em grau JE; este grau está habilitado por compatibilidade, sem validação independente."
                                if routing.get("status") == "routed_compatible_unvalidated" else None),
                    "applicable": True, "automatic": True,
                    "validation_scope": "compatible_unvalidated" if routing.get("status") == "routed_compatible_unvalidated" else "validated",
                    "routing_status": routing.get("status"),
                    "connector": routing.get("selected_connector")}
        if routing.get("tribunal_id") == "TRF1":
            grades = set(routing.get("grades") or [])
            systems = {str(value).casefold() for value in (routing.get("system_names") or [])}
            candidates = [item for item in routing.get("court_candidates", [])
                          if item.get("matches_metadata") and str(item.get("id", "")).startswith("TRF1-PJE-")
                          and item.get("degree") in grades
                          and str(item.get("system", "")).casefold() in systems]
            if len(candidates) == 1:
                item = candidates[0]
                return {"source": "PJe/TRF1", "status": "not_validated",
                        "message": "Conector PJe/TRF1 implementado, mas esta combinação de sistema e grau ainda não foi validada para coleta automática.",
                        "applicable": True, "connector": item.get("id"),
                        "routing_status": item.get("routing_status"), "automatic": False}
        if routing.get("tribunal_id") == "TRF4":
            state = (check or {}).get("status")
            state_map = {"operational": "completed", "consulta concluída": "completed",
                         "partially_operational": "partial", "consulta parcial": "partial",
                         "captcha_identified": "awaiting_access", "authentication_required": "awaiting_access",
                         "unauthorized": "awaiting_access", "timeout": "failed",
                         "court_unavailable": "failed", "source_unavailable": "failed",
                         "layout_incompatible": "failed", "not_implemented": "not_implemented"}
            status = state_map.get(state, "completed" if snapshot else "not_started")
            error = json.loads(check["error"]) if check and check.get("error") else None
            return {"source": TRF4_EPROC_LABEL, "status": status,
                    "message": error.get("message") if error else "A tentativa direta só ocorre por atualização manual; o scheduler permanece desativado para TRF4.",
                    "applicable": True, "connector": TRF4_EPROC_SOURCE, "automatic": False}
        return {"source": "Consulta direta ao tribunal", "status": "not_implemented",
                "message": "Consulta direta ao tribunal ainda não implementada",
                "applicable": False, "connector": None,
                "tribunal": routing.get("tribunal_id"),
                "system": routing.get("system_names") or [],
                "degree": routing.get("grades") or [],
                "routing_status": routing.get("status")}

    @app.middleware("http")
    async def protect(request: Request, call_next):
        request_started = time.perf_counter()
        sqlite_metrics = {"sqlite_seconds": 0.0, "sqlite_calls": 0}
        path = request.url.path
        ip = request.client.host if request.client else "unknown"
        recent = requests_by_ip[ip]
        moment = time.monotonic()
        while recent and recent[0] < moment - 60:
            recent.popleft()
        def secure_response(response):
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "font-src 'self' data:; worker-src 'self' blob:; connect-src 'self'; "
                "base-uri 'none'; frame-ancestors 'none'"
            )
            if config.production and config.public_origin.startswith("https://"):
                response.headers["Strict-Transport-Security"] = "max-age=31536000"
            return response

        if len(recent) >= 120:
            return secure_response(JSONResponse({"detail": "Muitas solicitações; aguarde um minuto."}, status_code=429))
        recent.append(moment)
        if config.auth_mode == "accounts" and path == "/api/auth/login":
            attempts = login_attempts_by_ip[ip]
            while attempts and attempts[0] < moment - 60:
                attempts.popleft()
            if len(attempts) >= 10:
                return secure_response(JSONResponse({"detail": "Muitas tentativas de login; aguarde um minuto."}, status_code=429))
            attempts.append(moment)
        if path == "/api/platform/auth/login":
            attempts = login_attempts_by_ip[ip]
            while attempts and attempts[0] < moment - 60:
                attempts.popleft()
            if len(attempts) >= 10:
                return secure_response(JSONResponse({"detail": "Muitas tentativas de login; aguarde um minuto."}, status_code=429))
            attempts.append(moment)
        org_token = identity_token = session_token = service_token = service_audit_token = auth_kind_token = None
        platform_identity_token = platform_session_token = None
        public_account_paths = {"/api/auth/mode", "/api/auth/login",
                                "/api/auth/recovery/request", "/api/auth/recovery/complete"}
        if path.startswith("/api/") and path not in ("/api/health", "/api/auth/mode"):
            authorization = request.headers.get("authorization", "")
            bearer = authorization[7:].strip() if authorization[:7].casefold() == "bearer " else ""
            is_platform_path = path.startswith("/api/platform/")
            if is_platform_path:
                if path != "/api/platform/auth/login":
                    raw_platform_session = request.cookies.get("processwatch_platform_session", "")
                    platform_session = repository.platform_admin_session(
                        raw_platform_session, idle_minutes=config.session_idle_minutes)
                    if not platform_session:
                        return secure_response(JSONResponse({"detail": "Sessão administrativa ausente ou expirada."}, status_code=401))
                    if request.method not in ("GET", "HEAD", "OPTIONS"):
                        origin = request.headers.get("origin")
                        expected = config.public_origin or f"{request.url.scheme}://{request.url.netloc}"
                        if not origin or not hmac.compare_digest(origin.rstrip("/"), expected):
                            return secure_response(JSONResponse({"detail": "Origem da solicitação não autorizada."}, status_code=403))
                    platform_identity_token = platform_identity_context.set(
                        {"id": platform_session["admin_id"], "email": platform_session["email"],
                         "is_owner": bool(platform_session["is_owner"])})
                    platform_session_token = platform_session_context.set(platform_session)
            service = repository.authenticate_service_credential(bearer) if bearer.startswith("pwsvc_") else None
            if is_platform_path:
                pass
            elif service:
                required = service_permissions_for(path, request.method)
                if required is None or not required.issubset(set(service["permissions"])):
                    repository.record_access("service_api_denied", None,
                        f"{request.method}: permissão insuficiente ou rota sem autorização explícita",
                        actor=f"service:{service['id']}", org=service["org"])
                    return secure_response(JSONResponse({"detail": "Credencial sem permissão para esta operação."}, status_code=403))
                if is_local_office_blocked(config.environment, service["org"]):
                    return secure_response(JSONResponse({"detail": "O escritório reservado 'local' está indisponível em produção."}, status_code=403))
                org_token = tenant_context.set(service["org"])
                service_token = service_context.set(service)
                service_audit_token = service_audit_logged.set(False)
                auth_kind_token = auth_kind_context.set("service")
            elif config.auth_mode == "accounts":
                if path.startswith("/api/extension/trf4/"):
                    # Keep the pre-existing local Chrome extension contract, but
                    # confine its bearer credential to capture endpoints and the
                    # configured legacy office. The endpoint still checks origin,
                    # loopback client, nonce, process registration and payload.
                    if not config.access_token or not hmac.compare_digest(authorization, "Bearer " + config.access_token):
                        return secure_response(JSONResponse({"detail": "Acesso não autorizado."}, status_code=401))
                    if is_local_office_blocked(config.environment, org):
                        return secure_response(JSONResponse({"detail": "O escritório reservado 'local' está indisponível em produção."}, status_code=403))
                    org_token = tenant_context.set(org)
                    auth_kind_token = auth_kind_context.set("legacy_bearer")
                elif path not in public_account_paths:
                    raw_session = request.cookies.get("processwatch_session", "")
                    inactive_allowed = path in {"/api/auth/me", "/api/auth/logout", "/api/office",
                                                "/api/office/activate"}
                    session = repository.auth_session(raw_session, idle_minutes=config.session_idle_minutes,
                                                      allow_inactive=inactive_allowed)
                    if not session:
                        return secure_response(JSONResponse({"detail": "Sessão ausente ou expirada."}, status_code=401))
                    if (is_local_office_blocked(config.environment, session["active_office_id"])
                            and path != "/api/auth/office"):
                        return secure_response(JSONResponse({"detail": "O escritório reservado 'local' está indisponível em produção."}, status_code=403))
                    if request.method not in ("GET", "HEAD", "OPTIONS"):
                        origin = request.headers.get("origin")
                        expected = config.public_origin or f"{request.url.scheme}://{request.url.netloc}"
                        if not origin or not hmac.compare_digest(origin.rstrip("/"), expected):
                            return secure_response(JSONResponse({"detail": "Origem da solicitação não autorizada."}, status_code=403))
                    org_token = tenant_context.set(session["active_office_id"])
                    identity_token = identity_context.set({"id": session["user_id"], "email": session["email"],
                                                           "full_name": session.get("full_name"),
                                                           "display_name": session.get("display_name"),
                                                           "role": session["role"], "offices": session["offices"]})
                    session_token = session_context.set(session)
                    auth_kind_token = auth_kind_context.set("session")
            elif not service:
                if not config.access_token:
                    return secure_response(JSONResponse({"detail": "Configure PROCESSWATCH_ACCESS_TOKEN ou habilite contas autenticadas."}, status_code=503))
                if not hmac.compare_digest(authorization, "Bearer " + config.access_token):
                    return secure_response(JSONResponse({"detail": "Acesso não autorizado."}, status_code=401))
                auth_kind_token = auth_kind_context.set("legacy_bearer")
            if config.production:
                parts = path.strip("/").split("/")
                path_office = None
                if len(parts) > 3 and parts[:3] in (["api", "platform", "offices"],
                                                   ["api", "platform", "processes"]):
                    path_office = parts[3]
                query_office = request.query_params.get("office_id")
                if (is_local_office_blocked(config.environment, path_office) or
                        is_local_office_blocked(config.environment, query_office)):
                    return secure_response(JSONResponse({"detail": "O escritório reservado 'local' está indisponível em produção."}, status_code=404))
        metrics_token = current_sqlite_metrics.set(sqlite_metrics)
        try:
            try:
                response = await call_next(request)
            except Exception:
                service = service_context.get()
                if service:
                    route = request.scope.get("route")
                    route_path = getattr(route, "path", path)
                    repository.record_access("service_api_error", None,
                        f"{request.method} {route_path}: exceção na resposta HTTP",
                        actor=f"service:{service['id']}", org=service["org"])
                raise
            service = service_context.get()
            if service and not service_audit_logged.get():
                route = request.scope.get("route")
                route_path = getattr(route, "path", path)
                repository.record_access("service_api_request", None,
                    f"{request.method} {route_path} -> {response.status_code}",
                    actor=f"service:{service['id']}", org=service["org"])
        finally:
            if org_token is not None:
                tenant_context.reset(org_token)
            if identity_token is not None:
                identity_context.reset(identity_token)
            if session_token is not None:
                session_context.reset(session_token)
            if service_token is not None:
                service_context.reset(service_token)
            if service_audit_token is not None:
                service_audit_logged.reset(service_audit_token)
            if auth_kind_token is not None:
                auth_kind_context.reset(auth_kind_token)
            if platform_identity_token is not None:
                platform_identity_context.reset(platform_identity_token)
            if platform_session_token is not None:
                platform_session_context.reset(platform_session_token)
            current_sqlite_metrics.reset(metrics_token)
        request_duration = (time.perf_counter() - request_started) * 1000
        sqlite_duration = sqlite_metrics["sqlite_seconds"] * 1000
        response.headers["Server-Timing"] = (
            f"app;dur={request_duration:.2f}, sqlite;dur={sqlite_duration:.2f}, "
            f"sqlcount;desc=\"{sqlite_metrics['sqlite_calls']} statements/fetches\"")
        response.headers["X-ProcessWatch-Request-Ms"] = f"{request_duration:.2f}"
        response.headers["X-ProcessWatch-SQLite-Ms"] = f"{sqlite_duration:.2f}"
        response.headers["X-ProcessWatch-SQLite-Calls"] = str(sqlite_metrics["sqlite_calls"])
        return secure_response(response)

    if owns_repository:
        @app.on_event("shutdown")
        def close_repository():
            repository.close()

    @app.get("/", include_in_schema=False)
    async def home():
        page = (Path(__file__).parent / "web" / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(page)

    @app.get("/platform-admin", include_in_schema=False)
    async def platform_admin_page():
        page = (Path(__file__).parent / "web" / "platform-admin.html").read_text(encoding="utf-8")
        return HTMLResponse(page)

    @app.get("/assets/platform-admin.js", include_in_schema=False)
    async def platform_admin_script():
        path = Path(__file__).parent / "web" / "platform-admin.js"
        return Response(content=path.read_bytes(), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})

    @app.get("/assets/platform-admin.css", include_in_schema=False)
    async def platform_admin_styles():
        path = Path(__file__).parent / "web" / "platform-admin.css"
        return Response(content=path.read_text(encoding="utf-8"), media_type="text/css",
                        headers={"Cache-Control": "no-cache"})

    @app.get("/assets/logo.png", include_in_schema=False)
    async def app_logo():
        logo = Path(__file__).parent / "web" / "logo.png"
        return Response(content=logo.read_bytes(), media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/assets/ui-correction.css", include_in_schema=False)
    async def ui_correction_styles():
        stylesheet = Path(__file__).parent / "web" / "ui-correction.css"
        return Response(content=stylesheet.read_text(encoding="utf-8"), media_type="text/css",
                        headers={"Cache-Control": "no-cache"})

    @app.get("/assets/app.js", include_in_schema=False)
    async def application_script():
        script = Path(__file__).parent / "web" / "app.js"
        return Response(content=script.read_bytes(), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "organization": current_org(),
            "default_interval": config.interval,
            "datajud_configured": bool(config.api_key and not config.api_key.startswith("APIKey ")),
            "scheduler_active": _scheduler_running(),
        }

    @app.get("/api/scheduler/status")
    async def scheduler_status():
        return _scheduler_summary()

    @app.get("/api/auth/mode")
    async def auth_mode():
        return {"mode": config.auth_mode}

    @app.post("/api/platform/auth/login", summary="Entrar como administrador da plataforma")
    async def platform_admin_login(payload: LoginRequest):
        administrator = repository.authenticate_platform_admin(payload.email, payload.password)
        if not administrator:
            repository.record_access("platform_admin_login_failed", None,
                                     "falha de autenticação administrativa", actor="platform_admin:unknown", org=None)
            raise HTTPException(status_code=401, detail="E-mail ou senha inválidos.")
        raw_token = secrets.token_urlsafe(48)
        expiry = datetime.now(timezone.utc) + timedelta(days=config.session_days)
        repository.create_platform_admin_session(administrator["id"], raw_token, expiry.isoformat())
        repository.record_access("platform_admin_login", None, "login administrativo da plataforma",
                                 actor=f"platform_admin:{administrator['email']}", org=None)
        response = JSONResponse({"status": "ok", "administrator": administrator,
                                 "expires_at": expiry.isoformat()})
        response.set_cookie("processwatch_platform_session", raw_token,
                            max_age=config.session_days * 86400, httponly=True,
                            secure=config.session_cookie_secure, samesite="strict", path="/")
        return response

    @app.get("/api/platform/auth/me", summary="Consultar identidade administrativa da plataforma")
    async def platform_admin_me():
        administrator = require_platform_admin()
        session = platform_session_context.get()
        return {"administrator": administrator, "expires_at": session["expires_at"]}

    @app.post("/api/platform/auth/logout", summary="Encerrar sessão administrativa da plataforma")
    async def platform_admin_logout():
        session = platform_session_context.get()
        if session:
            revoked = repository.revoke_platform_admin_session(session["session_id"])
            if not revoked:
                audit_platform("platform_admin_logout_failed",
                               purpose="encerramento recusado: sessão já revogada ou indisponível")
                raise HTTPException(status_code=409, detail="A sessão administrativa não pôde ser encerrada.")
            audit_platform("platform_admin_logout", purpose="encerramento da sessão administrativa")
        response = JSONResponse({"status": "ok"})
        response.delete_cookie("processwatch_platform_session", path="/", httponly=True,
                               secure=config.session_cookie_secure, samesite="strict")
        return response

    @app.post("/api/platform/auth/password", summary="Alterar a própria senha global",
              description="Exige senha atual. Revoga todas as sessões globais após a alteração.")
    async def platform_admin_change_password(payload: PlatformAdminPasswordChangeRequest):
        actor = require_platform_admin()
        try:
            repository.change_platform_admin_password(actor["id"], payload.current_password,
                                                      payload.new_password)
        except PermissionError as exc:
            audit_platform("platform_admin_password_change_failed",
                           purpose="alteração própria recusada: senha atual inválida")
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except ValueError as exc:
            audit_platform("platform_admin_password_change_failed", purpose="alteração própria recusada por validação")
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except sqlite3.IntegrityError:
            audit_platform("platform_admin_password_change_failed", purpose="falha transacional na alteração própria")
            raise HTTPException(status_code=500, detail="Não foi possível alterar a senha.") from None
        audit_platform("platform_admin_password_change", purpose="alteração da própria senha; sessões revogadas")
        response = JSONResponse({"status": "password_changed", "message": "Senha alterada. Entre novamente."})
        response.delete_cookie("processwatch_platform_session", path="/", httponly=True,
                               secure=config.session_cookie_secure, samesite="strict")
        return response

    @app.get("/api/platform/admins", summary="Listar administradores globais")
    async def platform_admins_list():
        require_platform_admin()
        audit_platform("platform_admins_list", purpose="consulta de administradores globais")
        return {"items": repository.list_platform_admins()}

    @app.post("/api/platform/admins", status_code=201, summary="Criar administrador global delegado",
              description="Cria administrador sem propriedade. A senha inicial é armazenada por hash e nunca é devolvida.")
    async def platform_admins_create(payload: PlatformAdminCreateRequest):
        require_platform_admin()
        try:
            result = repository.create_platform_admin(payload.email, payload.password)
        except ValueError as exc:
            audit_platform("platform_admin_create_failed", purpose="criação recusada por validação")
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except sqlite3.IntegrityError:
            audit_platform("platform_admin_create_failed", purpose="criação recusada por e-mail já cadastrado")
            raise HTTPException(status_code=409, detail="O e-mail já está cadastrado como administrador global.") from None
        audit_platform("platform_admin_create", purpose=f"admin_id={result['id']}; administrador delegado criado")
        return result

    @app.get("/api/platform/admins/{admin_id}", summary="Consultar administrador global")
    async def platform_admins_detail(admin_id: int):
        require_platform_admin()
        result = repository.platform_admin(admin_id)
        if not result:
            audit_platform("platform_admin_detail_failed", purpose=f"consulta recusada: admin_id={admin_id} inexistente")
            raise HTTPException(status_code=404, detail="Administrador global não encontrado.")
        audit_platform("platform_admin_detail", purpose=f"consulta de admin_id={admin_id}")
        return result

    @app.patch("/api/platform/admins/{admin_id}", summary="Atualizar cadastro de administrador global",
               description="Atualiza o e-mail de acesso e revoga as sessões existentes quando houver alteração.")
    async def platform_admins_update(admin_id: int, payload: PlatformAdminUpdateRequest):
        actor = require_platform_admin()
        target = repository.platform_admin(admin_id)
        if not target:
            audit_platform("platform_admin_update_failed", purpose=f"atualização recusada: admin_id={admin_id} inexistente")
            raise HTTPException(status_code=404, detail="Administrador global não encontrado.")
        if target["is_owner"] and not actor["is_owner"]:
            audit_platform("platform_admin_update_denied", purpose=f"administrador delegado tentou alterar proprietário admin_id={admin_id}")
            raise HTTPException(status_code=403, detail="Somente o proprietário pode atualizar o próprio cadastro.")
        try:
            result = repository.update_platform_admin_email(admin_id, payload.email)
        except ValueError as exc:
            audit_platform("platform_admin_update_failed", purpose=f"atualização recusada por validação: admin_id={admin_id}")
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except sqlite3.IntegrityError:
            audit_platform("platform_admin_update_failed", purpose=f"e-mail já cadastrado para admin_id={admin_id}")
            raise HTTPException(status_code=409, detail="O e-mail já está cadastrado como administrador global.") from None
        audit_platform("platform_admin_update", purpose=f"admin_id={admin_id}; campo alterado=email; sessões revogadas se alterado")
        response = JSONResponse(result)
        session = platform_session_context.get()
        if session and session["admin_id"] == admin_id and target["email"].casefold() != result["email"].casefold():
            response.delete_cookie("processwatch_platform_session", path="/", httponly=True,
                                   secure=config.session_cookie_secure, samesite="strict")
        return response

    @app.patch("/api/platform/admins/{admin_id}/status", summary="Ativar ou desativar administrador global")
    async def platform_admins_status(admin_id: int, payload: PlatformAdminStatusRequest):
        actor = require_platform_admin()
        target = repository.platform_admin(admin_id)
        if not target:
            audit_platform("platform_admin_status_failed", purpose=f"alteração recusada: admin_id={admin_id} inexistente")
            raise HTTPException(status_code=404, detail="Administrador global não encontrado.")
        if target["is_owner"] and not actor["is_owner"]:
            audit_platform("platform_admin_status_denied", purpose=f"administrador delegado tentou alterar estado do proprietário admin_id={admin_id}")
            raise HTTPException(status_code=403, detail="Somente o proprietário pode administrar o estado da identidade proprietária.")
        try:
            result = repository.set_platform_admin_active(admin_id, payload.active)
        except ValueError as exc:
            audit_platform("platform_admin_status_failed", purpose=f"estado recusado por proteção de proprietário admin_id={admin_id}")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        audit_platform("platform_admin_activate" if payload.active else "platform_admin_deactivate",
                       purpose=f"admin_id={admin_id}; estado={'ativo' if payload.active else 'inativo'}; sessões revogadas na desativação")
        response = JSONResponse(result)
        session = platform_session_context.get()
        if session and session["admin_id"] == admin_id and not payload.active:
            response.delete_cookie("processwatch_platform_session", path="/", httponly=True,
                                   secure=config.session_cookie_secure, samesite="strict")
        return response

    @app.post("/api/platform/admins/{admin_id}/sessions/revoke", summary="Revogar sessões de administrador global")
    async def platform_admins_revoke_sessions(admin_id: int):
        actor = require_platform_admin()
        target = repository.platform_admin(admin_id)
        if not target:
            audit_platform("platform_admin_sessions_revoke_failed", purpose=f"revogação recusada: admin_id={admin_id} inexistente")
            raise HTTPException(status_code=404, detail="Administrador global não encontrado.")
        if target["is_owner"] and not actor["is_owner"]:
            audit_platform("platform_admin_sessions_revoke_denied", purpose=f"administrador delegado tentou revogar sessões do proprietário admin_id={admin_id}")
            raise HTTPException(status_code=403, detail="Somente o proprietário pode revogar as próprias sessões administrativas.")
        session = platform_session_context.get()
        keep_session = session["session_id"] if session and session["admin_id"] == admin_id else None
        revoked = repository.revoke_platform_admin_sessions(admin_id, except_session_id=keep_session)
        audit_platform("platform_admin_sessions_revoke", purpose=f"admin_id={admin_id}; sessões revogadas={revoked}")
        return {"status": "revoked", "admin_id": admin_id, "revoked_sessions": revoked,
                "current_session_preserved": bool(keep_session)}

    @app.post("/api/platform/admins/{admin_id}/password/reset", summary="Redefinir senha de administrador delegado",
              description="Somente o proprietário pode redefinir a senha de um administrador delegado. "
                          "Revoga todas as sessões da identidade afetada.")
    async def platform_admins_reset_password(admin_id: int, payload: PlatformAdminPasswordResetRequest):
        actor = require_platform_admin()
        if not actor["is_owner"]:
            audit_platform("platform_admin_password_reset_denied",
                           purpose=f"administrador delegado tentou redefinir admin_id={admin_id}")
            raise HTTPException(status_code=403, detail="Somente o proprietário pode redefinir senhas de administradores globais.")
        target = repository.platform_admin(admin_id)
        if not target:
            audit_platform("platform_admin_password_reset_failed",
                           purpose=f"redefinição recusada: admin_id={admin_id} inexistente")
            raise HTTPException(status_code=404, detail="Administrador global não encontrado.")
        if target["is_owner"]:
            audit_platform("platform_admin_password_reset_denied",
                           purpose="tentativa de redefinir a senha do proprietário pela API")
            raise HTTPException(status_code=409, detail="A senha do proprietário só pode ser recuperada por procedimento local.")
        try:
            result = repository.reset_delegated_platform_admin_password(admin_id, payload.new_password)
        except ValueError as exc:
            audit_platform("platform_admin_password_reset_failed",
                           purpose=f"redefinição recusada: admin_id={admin_id}")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except LookupError as exc:
            audit_platform("platform_admin_password_reset_failed",
                           purpose=f"redefinição recusada: admin_id={admin_id} inexistente")
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except sqlite3.IntegrityError:
            audit_platform("platform_admin_password_reset_failed",
                           purpose=f"falha transacional: admin_id={admin_id}")
            raise HTTPException(status_code=500, detail="Não foi possível redefinir a senha.") from None
        audit_platform("platform_admin_password_reset", purpose=f"senha redefinida para admin_id={admin_id}; sessões revogadas")
        return {"status": "password_reset", "administrator": result,
                "message": "Senha redefinida. A pessoa deverá entrar novamente."}

    @app.get("/api/platform/overview", summary="Indicadores globais reais da instalação")
    async def platform_overview():
        identity = require_platform_admin()
        audit_platform("platform_overview", purpose="consulta dos indicadores globais da plataforma")
        db = repository.db
        count = lambda sql, args=(): db.execute(sql, args).fetchone()[0]
        excluded = " AND org<>'local'" if config.production else ""
        return {
            "offices": count("SELECT count(*) FROM offices" + (" WHERE id<>'local'" if config.production else "")),
            "active_offices": count("SELECT count(*) FROM offices WHERE active=1" + (" AND id<>'local'" if config.production else "")),
            "users": count("SELECT count(DISTINCT u.id) FROM users u JOIN office_memberships m ON m.user_id=u.id WHERE u.active=1" + (" AND m.office_id<>'local'" if config.production else "")),
            "processes": count("SELECT count(*) FROM watches" + (" WHERE org<>'local'" if config.production else "")),
            "movements": count("SELECT count(*) FROM movements" + (" WHERE org<>'local'" if config.production else "")),
            "communications": count("SELECT count(*) FROM communication_inbox" + (" WHERE org<>'local'" if config.production else "")),
            "jobs": {status: count("SELECT count(*) FROM collection_jobs WHERE status=?" + excluded, (status,))
                     for status in ("queued", "running", "completed", "partial", "failed", "awaiting_access")},
            "oab_jobs": {status: count("SELECT count(*) FROM oab_monitor_jobs WHERE status=?" + excluded, (status,))
                         for status in ("queued", "running", "completed", "failed", "cancelled")},
            "worker_running": _scheduler_running(),
            "worker_telemetry": _platform_worker_telemetry(),
            "platform_admin": identity["email"],
        }

    @app.get("/api/platform/offices", summary="Listar escritórios da plataforma")
    async def platform_offices():
        require_platform_admin()
        audit_platform("platform_offices_list", purpose="consulta da lista global de escritórios")
        rows = [row for row in repository.list_offices()
                if not is_local_office_blocked(config.environment, row["id"])]
        for item in rows:
            item["stats"] = repository.platform_office_stats(item["id"])
        return {"items": rows}

    @app.get("/api/platform/offices/{office_id}", summary="Consultar escritório e seus usuários")
    async def platform_office_detail(office_id: str):
        require_platform_admin()
        office = repository.office(office_id)
        if not office:
            raise HTTPException(status_code=404, detail="Escritório não encontrado.")
        audit_platform("platform_office_detail", target_org=office_id,
                       purpose="consulta administrativa de dados do escritório")
        users = [dict(row) for row in repository.db.execute("""SELECT u.id,u.email,u.full_name,
            u.active user_active,m.role,m.active membership_active,m.display_name,m.created_at
            FROM office_memberships m JOIN users u ON u.id=m.user_id WHERE m.office_id=?
            ORDER BY m.role,u.email""", (office_id,)).fetchall()]
        return {"office": office, "stats": repository.platform_office_stats(office_id), "users": users}

    @app.patch("/api/platform/offices/{office_id}", summary="Atualizar cadastro de escritório",
               description=("Atualização parcial de nome e dados de contato. Funciona para escritórios ativos ou inativos. "
                            "O identificador é imutável; ativação e desativação continuam em /status."))
    async def platform_update_office(office_id: str, payload: PlatformOfficeUpdateRequest):
        require_platform_admin()
        changes = payload.model_dump(exclude_unset=True)
        if not changes or any(value is None for value in changes.values()):
            raise HTTPException(status_code=422,
                                detail="Informe ao menos um campo cadastral não nulo.")
        before = repository.office(office_id)
        if not before:
            audit_platform("platform_office_update_failed", target_org=office_id,
                           purpose="edição cadastral recusada: escritório inexistente")
            raise HTTPException(status_code=404, detail="Escritório não encontrado.")
        try:
            updated = repository.patch_platform_office(office_id, changes)
        except ValueError as exc:
            audit_platform("platform_office_update_failed", target_org=office_id,
                           purpose="edição cadastral recusada por validação; campos=" + ",".join(sorted(changes)))
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except LookupError as exc:
            audit_platform("platform_office_update_failed", target_org=office_id,
                           purpose="edição cadastral recusada: escritório inexistente")
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except sqlite3.IntegrityError:
            audit_platform("platform_office_update_failed", target_org=office_id,
                           purpose="edição cadastral revertida por conflito de integridade")
            raise HTTPException(status_code=409,
                                detail="A edição foi revertida por conflito de integridade cadastral.") from None
        changed = sorted(field for field in changes if before[field] != updated[field])
        audit_platform("platform_office_update", target_org=office_id,
                       purpose="edição cadastral concluída; campos alterados=" + ",".join(changed))
        return updated

    @app.get("/api/platform/offices/{office_id}/users",
             summary="Listar usuários vinculados a um escritório")
    async def platform_office_users(office_id: str):
        require_platform_admin()
        if not repository.office(office_id):
            audit_platform("platform_office_users_list_failed", target_org=office_id,
                           purpose="consulta recusada: escritório inexistente")
            raise HTTPException(status_code=404, detail="Escritório não encontrado.")
        audit_platform("platform_office_users_list", target_org=office_id,
                       purpose="consulta global de usuários e vínculos do escritório")
        return {"office_id": office_id, "items": repository.office_users(office_id)}

    @app.post("/api/platform/offices/{office_id}/users/{user_id}/recovery", status_code=201,
              summary="Emitir token de recuperação de usuário do escritório",
              description="Somente administrador global. Token único válido por 30 minutos, retornado uma vez. "
                          "A entrega deve ser manual por canal privado; não há envio automático de e-mail.")
    async def platform_issue_user_recovery(office_id: str, user_id: int):
        actor = require_platform_admin()
        try:
            issued = repository.issue_office_password_recovery_token(
                user_id, office_id, actor=f"platform_admin:{actor['email']}", ttl_minutes=30)
        except LookupError:
            audit_platform("platform_user_recovery_issue_failed", target_org=office_id,
                           purpose=f"conta ativa não encontrada: user_id={user_id}")
            raise HTTPException(status_code=404, detail="Conta ativa não encontrada neste escritório.") from None
        audit_platform("platform_user_recovery_issue", target_org=office_id,
                       purpose=f"token de recuperação emitido para user_id={user_id}; validade de 30 minutos")
        return {"status": "issued", "user_id": user_id, **issued,
                "message": "Token de uso único. Compartilhe somente em canal privado; não há envio por e-mail."}

    @app.post("/api/platform/offices/{office_id}/users", status_code=201,
              summary="Criar ou vincular usuário a um escritório",
              description=("Cria uma conta e seu vínculo, ou vincula uma conta existente sem alterar sua senha. "
                           "Pode preparar um escritório inativo sem reativá-lo. A senha temporária é aceita "
                           "somente para uma conta nova."))
    async def platform_add_office_user(office_id: str, payload: OfficeUserCreateRequest):
        require_platform_admin()
        if not repository.office(office_id):
            audit_platform("platform_office_user_create_failed", target_org=office_id,
                           purpose="criação/vínculo recusado: escritório inexistente")
            raise HTTPException(status_code=404, detail="Escritório não encontrado.")
        try:
            result = repository.ensure_platform_office_membership(
                office_id, payload.email, payload.role, password=payload.temporary_password,
                full_name=payload.full_name)
        except ValueError as exc:
            audit_platform("platform_office_user_create_failed", target_org=office_id,
                           purpose="criação/vínculo recusado por validação")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except sqlite3.IntegrityError:
            audit_platform("platform_office_user_create_failed", target_org=office_id,
                           purpose="criação/vínculo recusado por conflito de conta ou vínculo")
            raise HTTPException(status_code=409, detail="Não foi possível criar ou vincular a conta informada.") from None
        audit_platform("platform_office_user_create" if result["created"] else "platform_office_user_link",
                       target_org=office_id,
                       purpose=f"user_id={result['id']}; conta={'criada' if result['created'] else 'existente vinculada'}; papel={result['role']}")
        return result

    @app.get("/api/platform/offices/{office_id}/users/{user_id}",
             summary="Consultar conta e vínculos do usuário")
    async def platform_office_user_detail(office_id: str, user_id: int):
        require_platform_admin()
        result = repository.platform_office_user(office_id, user_id)
        if not result:
            audit_platform("platform_office_user_detail_failed", target_org=office_id,
                           purpose=f"consulta recusada: user_id={user_id} não vinculado")
            raise HTTPException(status_code=404, detail="Usuário não vinculado a este escritório.")
        audit_platform("platform_office_user_detail", target_org=office_id,
                       purpose=f"consulta global de user_id={user_id} e seus vínculos")
        return result

    @app.patch("/api/platform/offices/{office_id}/users/{user_id}",
               summary="Atualizar conta ou vínculo de usuário",
               description=("role, active e display_name alteram o vínculo apenas neste escritório. "
                            "account_active altera a conta global e seu acesso a todos os escritórios; "
                            "desativá-la revoga todas as sessões e chaves de serviço do usuário."))
    async def platform_update_office_user(office_id: str, user_id: int,
                                          payload: PlatformOfficeUserUpdateRequest):
        require_platform_admin()
        try:
            repository.update_office_membership(
                office_id, user_id, **payload.model_dump(exclude_unset=True))
        except LookupError as exc:
            audit_platform("platform_office_user_update_failed", target_org=office_id,
                           purpose=f"atualização recusada: user_id={user_id} não vinculado")
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            audit_platform("platform_office_user_update_failed", target_org=office_id,
                           purpose=f"atualização recusada por regra de negócio: user_id={user_id}")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        changed = sorted(payload.model_dump(exclude_unset=True))
        audit_platform("platform_office_user_update", target_org=office_id,
                       purpose=f"user_id={user_id}; campos alterados={','.join(changed)}")
        return {"status": "updated", "user_id": user_id,
                "user": repository.platform_office_user(office_id, user_id)}

    @app.delete("/api/platform/offices/{office_id}/users/{user_id}",
                summary="Remover vínculo de usuário com um escritório",
                description="Remove somente o vínculo deste escritório. Outros vínculos do usuário são preservados.")
    async def platform_remove_office_user(office_id: str, user_id: int):
        require_platform_admin()
        if not repository.office(office_id):
            audit_platform("platform_office_user_remove_failed", target_org=office_id,
                           purpose=f"remoção recusada: escritório inexistente; user_id={user_id}")
            raise HTTPException(status_code=404, detail="Escritório não encontrado.")
        try:
            removed = repository.remove_office_membership(office_id, user_id)
        except ValueError as exc:
            audit_platform("platform_office_user_remove_failed", target_org=office_id,
                           purpose=f"remoção recusada por regra de último administrador: user_id={user_id}")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        if not removed:
            audit_platform("platform_office_user_remove_failed", target_org=office_id,
                           purpose=f"remoção recusada: user_id={user_id} não vinculado")
            raise HTTPException(status_code=404, detail="Usuário não vinculado a este escritório.")
        remaining = repository.db.execute("SELECT count(*) FROM office_memberships WHERE user_id=?",
                                           (user_id,)).fetchone()[0]
        audit_platform("platform_office_user_remove", target_org=office_id,
                       purpose=f"user_id={user_id}; vínculo removido; outros vínculos={remaining}")
        return {"status": "removed", "user_id": user_id, "remaining_memberships": remaining}

    @app.post("/api/platform/offices", status_code=201, summary="Provisionar escritório e primeiro administrador")
    async def platform_provision_office(payload: PlatformOfficeCreateRequest):
        identity = require_platform_admin()
        try:
            result = repository.provision_office(payload.office_id, payload.name,
                payload.administrator_email, payload.administrator_password,
                full_name=payload.administrator_name, office_email=payload.email,
                phone=payload.phone, address=payload.address)
        except (ValueError, LookupError) as exc:
            audit_platform("platform_office_provision_failed", target_org=payload.office_id,
                           purpose="provisionamento recusado por validação")
            raise HTTPException(status_code=422, detail=str(exc)) from None
        except sqlite3.IntegrityError:
            audit_platform("platform_office_provision_failed", target_org=payload.office_id,
                           purpose="provisionamento recusado por conflito de identificador ou e-mail")
            raise HTTPException(status_code=409, detail="O identificador do escritório ou o e-mail já está em uso.") from None
        audit_platform("platform_office_provision", target_org=result["office"]["id"],
                       purpose="criação de escritório e primeiro administrador")
        return {"office": result["office"], "administrator": result["administrator"]}

    @app.patch("/api/platform/offices/{office_id}/status", summary="Ativar ou desativar escritório")
    async def platform_set_office_status(office_id: str, payload: PlatformOfficeStatusRequest):
        require_platform_admin()
        try:
            result = repository.set_office_active(office_id, payload.active)
        except LookupError as exc:
            audit_platform("platform_office_status_failed", target_org=office_id,
                           purpose="alteração de estado recusada: escritório inexistente")
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            audit_platform("platform_office_status_failed", target_org=office_id,
                           purpose="alteração de estado recusada por regra operacional")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        audit_platform("platform_office_activate" if payload.active else "platform_office_deactivate",
                       target_org=office_id, purpose="alteração administrativa do estado do escritório")
        return result

    @app.get("/api/platform/users", summary="Consultar usuários e vínculos de todos os escritórios")
    async def platform_users(office_id: str | None = None):
        require_platform_admin()
        audit_platform("platform_users_list", target_org=office_id,
                       purpose="consulta administrativa de usuários e vínculos")
        sql = """SELECT u.id,u.email,u.full_name,u.active user_active,m.office_id,m.role,
            m.active membership_active,m.display_name FROM users u
            LEFT JOIN office_memberships m ON m.user_id=u.id"""
        args = ()
        user_filters = []
        if office_id:
            user_filters.append("m.office_id=?")
            args = (office_id,)
        if config.production:
            user_filters.append("m.office_id<>'local'")
        if user_filters:
            sql += " WHERE " + " AND ".join(user_filters)
        sql += " ORDER BY u.email,m.office_id"
        return {"items": [dict(row) for row in repository.db.execute(sql, args).fetchall()]}

    @app.get("/api/platform/jobs", summary="Consultar fila e histórico de jobs")
    async def platform_jobs(status: str | None = None, office_id: str | None = None,
                            number: str | None = None, collection_type: str | None = None,
                            source: str | None = None, job_kind: str | None = None,
                            created_after: datetime | None = None, created_before: datetime | None = None,
                            limit: int = Query(default=50, ge=1, le=200),
                            offset: int = Query(default=0, ge=0)):
        require_platform_admin()
        if status and status not in {"queued", "running", "completed", "partial", "failed", "awaiting_access", "cancelled"}:
            raise HTTPException(status_code=422, detail="Estado de job inválido.")
        if job_kind and job_kind not in {"collection", "oab"}:
            raise HTTPException(status_code=422, detail="Tipo de job inválido.")
        def utc_filter(value):
            if value is None:
                return None
            return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None
                    else value.astimezone(timezone.utc)).isoformat()
        after = utc_filter(created_after)
        before = utc_filter(created_before)
        if after and before and datetime.fromisoformat(after) > datetime.fromisoformat(before):
            raise HTTPException(status_code=422, detail="O início do período deve anteceder o fim.")
        result = repository.list_platform_jobs(
            office_id=office_id, number=number, collection_type=collection_type,
            status=status, source=source, job_kind=job_kind,
            created_after=after, created_before=before,
            limit=limit, offset=offset)
        audit_platform("platform_jobs_list", target_org=office_id,
                       purpose="consulta administrativa paginada da fila de jobs")
        return sanitize_job_diagnostics(result)

    @app.get("/api/platform/jobs/{job_kind}/{job_id}", summary="Consultar detalhes e histórico de um job")
    async def platform_job_detail(job_kind: str, job_id: int):
        require_platform_admin()
        if job_kind == "collection":
            row = repository.db.execute("SELECT * FROM collection_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                audit_platform("platform_job_detail_failed", purpose=f"consulta recusada: job collection {job_id} inexistente")
                raise HTTPException(status_code=404, detail="Job de coleta não encontrado.")
            job = dict(row)
            if is_local_office_blocked(config.environment, job["org"]):
                raise HTTPException(status_code=404, detail="Job de coleta não encontrado.")
            job.pop("lease_token", None)
            siblings = [dict(item) for item in repository.db.execute("""SELECT id,source,status,attempts,
                max_attempts,scheduled_at,started_at,completed_at,error,coverage,result
                FROM collection_jobs WHERE org=? AND number=? AND group_id=? ORDER BY stage_order,id""",
                (job["org"], job["number"], job["group_id"]))]
            runs = [dict(item) for item in repository.db.execute("""SELECT id,started_at,finished_at,status,error
                FROM runs WHERE org=? AND number=? ORDER BY id DESC LIMIT 50""",
                (job["org"], job["number"]))]
            checks = [dict(item) for item in repository.db.execute("""SELECT id,source,checked_at,status,
                period_start,period_end,count,error FROM source_checks
                WHERE org=? AND number=? ORDER BY checked_at DESC,id DESC LIMIT 100""",
                (job["org"], job["number"]))]
            audit_platform("platform_job_detail", target_org=job["org"], number=job["number"],
                           purpose=f"consulta do job collection id={job_id}")
            return sanitize_job_diagnostics({"job_kind": job_kind, "job": job, "group_jobs": siblings,
                    "process_runs": runs, "source_checks": checks})
        if job_kind == "oab":
            row = repository.db.execute("""SELECT j.*,m.oab,m.uf,m.lawyer_name,m.enabled monitor_enabled,
                m.interval_hours,m.last_attempt_at,m.last_success_at,m.next_run_at,m.status monitor_status,
                m.last_error monitor_error,m.cursor_end,m.baseline_completed
                FROM oab_monitor_jobs j JOIN oab_monitors m ON m.id=j.monitor_id WHERE j.id=?""",
                (job_id,)).fetchone()
            if not row:
                audit_platform("platform_job_detail_failed", purpose=f"consulta recusada: job OAB {job_id} inexistente")
                raise HTTPException(status_code=404, detail="Job de monitoramento OAB não encontrado.")
            job = dict(row)
            if is_local_office_blocked(config.environment, job["org"]):
                raise HTTPException(status_code=404, detail="Job de monitoramento OAB não encontrado.")
            history = [dict(item) for item in repository.db.execute("""SELECT id,window_start,window_end,
                run_kind,status,attempts,scheduled_at,started_at,completed_at,result,error
                FROM oab_monitor_jobs WHERE monitor_id=? ORDER BY id DESC LIMIT 50""",
                (job["monitor_id"],))]
            discoveries = [dict(item) for item in repository.db.execute("""SELECT id,period_start,period_end,
                checked_at,status,pages,reported,matched,error FROM discovery_runs
                WHERE org=? AND source='DJEN' AND criterion='OAB/UF' AND oab=? AND uf=?
                ORDER BY id DESC LIMIT 50""", (job["org"], job["oab"], job["uf"]))]
            audit_platform("platform_job_detail", target_org=job["org"],
                           purpose=f"consulta do job oab id={job_id}")
            return sanitize_job_diagnostics({"job_kind": job_kind, "job": job, "monitor_history": history,
                    "discovery_runs": discoveries})
        audit_platform("platform_job_detail_failed", purpose=f"consulta recusada: tipo de job inválido ({job_kind})")
        raise HTTPException(status_code=422, detail="Tipo de job inválido.")

    @app.post("/api/platform/jobs/collection/{job_id}/retry", status_code=202,
              summary="Reenfileirar etapa de coleta encerrada com falha")
    async def platform_retry_collection_job(job_id: int, payload: PlatformAccessPurposeRequest):
        actor = require_platform_admin()
        target = repository.db.execute("SELECT org FROM collection_jobs WHERE id=?", (job_id,)).fetchone()
        if target and is_local_office_blocked(config.environment, target["org"]):
            audit_platform("platform_collection_retry_failed", target_org="local",
                           purpose=f"retry recusado: job={job_id} pertence ao escritório legado")
            raise HTTPException(status_code=404, detail="Job de coleta não encontrado.")
        try:
            result = repository.retry_failed_collection_group(job_id)
        except LookupError as exc:
            audit_platform("platform_collection_retry_failed", purpose=f"retry recusado: job={job_id} inexistente")
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            audit_platform("platform_collection_retry_failed", purpose=f"retry recusado por estado/conflito: job={job_id}")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        audit_platform("platform_collection_retry", target_org=result["jobs"][0]["org"],
                       number=result["jobs"][0]["number"],
                       purpose=f"retry administrativo do job {job_id}; grupo={result['group_id']}; {payload.purpose}")
        return {**result, "requested_by": actor["email"]}

    @app.post("/api/platform/jobs/oab/{job_id}/retry", status_code=202,
              summary="Reenfileirar consulta OAB que terminou com falha")
    async def platform_retry_oab_job(job_id: int, payload: PlatformAccessPurposeRequest):
        actor = require_platform_admin()
        row = repository.db.execute("SELECT org,monitor_id,status FROM oab_monitor_jobs WHERE id=?",
                                    (job_id,)).fetchone()
        if not row:
            audit_platform("platform_oab_retry_failed", purpose=f"retry recusado: job OAB={job_id} inexistente")
            raise HTTPException(status_code=404, detail="Job de monitoramento OAB não encontrado.")
        if is_local_office_blocked(config.environment, row["org"]):
            audit_platform("platform_oab_retry_failed", target_org="local",
                           purpose=f"retry recusado: job OAB={job_id} pertence ao escritório legado")
            raise HTTPException(status_code=404, detail="Job de monitoramento OAB não encontrado.")
        if row["status"] != "failed":
            audit_platform("platform_oab_retry_failed", target_org=row["org"],
                           purpose=f"retry recusado por estado: job OAB={job_id}")
            raise HTTPException(status_code=409, detail="Somente jobs OAB com falha podem ser reenfileirados.")
        office = repository.office(row["org"])
        if not office or not office["active"]:
            audit_platform("platform_oab_retry_failed", target_org=row["org"],
                           purpose=f"retry recusado: escritório inativo; job OAB={job_id}")
            raise HTTPException(status_code=409, detail="O escritório está inativo; a fila não executará este job.")
        try:
            job = repository.enqueue_oab_monitor_job(row["org"], row["monitor_id"], manual=True)
        except ValueError as exc:
            audit_platform("platform_oab_retry_failed", target_org=row["org"],
                           purpose=f"retry recusado por conflito: job OAB={job_id}")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        audit_platform("platform_oab_retry", target_org=row["org"],
                       purpose=f"retry administrativo do job OAB {job_id}; novo_job={job['id']}; {payload.purpose}")
        return {"status": "scheduled", "previous_job_id": job_id, "job_id": job["id"],
                "monitor_id": job["monitor_id"], "run_kind": job["run_kind"],
                "window_start": job["window_start"], "window_end": job["window_end"],
                "requested_by": actor["email"]}

    @app.post("/api/platform/offices/{office_id}/oab-monitors/{monitor_id}/refresh", status_code=202,
              summary="Solicitar consulta manual de monitoramento OAB")
    async def platform_refresh_oab_monitor(office_id: str, monitor_id: int,
                                           payload: PlatformAccessPurposeRequest):
        actor = require_platform_admin()
        office = repository.office(office_id)
        if not office:
            audit_platform("platform_oab_refresh_failed", target_org=office_id,
                           purpose=f"atualização recusada: escritório inexistente; monitor={monitor_id}")
            raise HTTPException(status_code=404, detail="Escritório não encontrado.")
        if not office["active"]:
            audit_platform("platform_oab_refresh_failed", target_org=office_id,
                           purpose=f"atualização recusada: escritório inativo; monitor={monitor_id}")
            raise HTTPException(status_code=409, detail="Ative o escritório antes de solicitar coleta OAB.")
        try:
            job = repository.enqueue_oab_monitor_job(office_id, monitor_id, manual=True)
        except ValueError as exc:
            audit_platform("platform_oab_refresh_failed", target_org=office_id,
                           purpose=f"atualização recusada por conflito/monitor inválido; monitor={monitor_id}")
            raise HTTPException(status_code=404, detail=str(exc)) from None
        audit_platform("platform_oab_refresh", target_org=office_id,
                       purpose=f"consulta DJEN OAB solicitada; monitor_id={monitor_id}; job_id={job['id']}; {payload.purpose}")
        return {"status": job["status"], "job_id": job["id"], "monitor_id": monitor_id,
                "window_start": job["window_start"], "window_end": job["window_end"],
                "run_kind": job["run_kind"], "requested_by": actor["email"]}

    @app.get("/api/platform/integrations", summary="Estado observado das integrações judiciais")
    async def platform_integrations(office_id: str | None = None):
        require_platform_admin()
        audit_platform("platform_integrations_summary", target_org=office_id,
                       purpose="consulta administrativa da saúde das integrações")
        sql = """SELECT org,source,count(*) checks,
            sum(CASE WHEN status IN ('failed','timeout','court_unavailable','source_unavailable') THEN 1 ELSE 0 END) failures,
            max(checked_at) last_checked FROM source_checks"""
        args = ()
        if office_id:
            sql += " WHERE org=?"
            args = (office_id,)
        elif config.production:
            sql += " WHERE org<>'local'"
        sql += " GROUP BY org,source ORDER BY org,source"
        checks = [dict(row) for row in repository.db.execute(sql, args).fetchall()]
        latest_sql = """SELECT s.org,s.source,s.checked_at,s.status,s.error FROM source_checks s
            JOIN (SELECT org,source,max(id) id FROM source_checks GROUP BY org,source) latest
            ON latest.id=s.id"""
        latest_args = ()
        if office_id:
            latest_sql += " WHERE s.org=?"
            latest_args = (office_id,)
        elif config.production:
            latest_sql += " WHERE s.org<>'local'"
        latest = {(row["org"], row["source"]): dict(row)
                  for row in repository.db.execute(latest_sql, latest_args).fetchall()}
        def safe_error_summary(value):
            if not value:
                return None
            sanitized = sanitize_error(value)
            try:
                parsed = json.loads(sanitized) if isinstance(sanitized, str) else sanitized
            except (TypeError, ValueError):
                parsed = sanitized
            if isinstance(parsed, dict):
                parsed = parsed.get("code") or parsed.get("message") or "Falha registrada pela fonte."
            return str(parsed)[:300]
        for item in checks:
            observed = latest.get((item["org"], item["source"]), {})
            item["last_status"] = observed.get("status")
            item["last_error"] = safe_error_summary(observed.get("error"))
            try:
                checked_at = datetime.fromisoformat(item["last_checked"])
                item["recent"] = (datetime.now(timezone.utc) -
                    (checked_at.replace(tzinfo=timezone.utc) if checked_at.tzinfo is None else checked_at)).total_seconds() <= 86400
            except (TypeError, ValueError):
                item["recent"] = False
            item["observation"] = "observed" if item["last_checked"] else "not_observed"
            item["health"] = ("last_check_failed" if observed.get("status") in
                ("failed", "timeout", "court_unavailable", "source_unavailable", "error") else
                "last_check_succeeded" if observed.get("status") in
                ("success", "completed", "partial", "consulta concluída", "consulta parcial") else
                "last_check_state_unknown" if observed else "unknown")

        discovery_sql = """SELECT org,count(*) discovery_runs,
            sum(CASE WHEN status IN ('error','failed','timeout') THEN 1 ELSE 0 END) failures,
            max(checked_at) last_checked FROM discovery_runs
            WHERE source='DJEN' AND criterion='OAB/UF'"""
        discovery_args = ()
        if office_id:
            discovery_sql += " AND org=?"
            discovery_args = (office_id,)
        elif config.production:
            discovery_sql += " AND org<>'local'"
        discovery_sql += " GROUP BY org ORDER BY org"
        for row in repository.db.execute(discovery_sql, discovery_args).fetchall():
            item = next((value for value in checks if value["org"] == row["org"] and
                         value["source"] == "DJEN"), None)
            if item is None:
                item = {"org": row["org"], "source": "DJEN", "checks": 0, "failures": 0,
                        "last_checked": None, "last_status": None, "last_error": None,
                        "recent": False, "observation": "not_observed", "health": "unknown"}
                checks.append(item)
            item["oab_discovery_runs"] = row["discovery_runs"]
            item["oab_discovery_failures"] = row["failures"] or 0
            item["failures"] = (item.get("failures") or 0) + (row["failures"] or 0)
            latest_discovery_sql = """SELECT checked_at,status,error FROM discovery_runs
                WHERE org=? AND source='DJEN' AND criterion='OAB/UF' ORDER BY id DESC LIMIT 1"""
            latest_discovery = repository.db.execute(latest_discovery_sql, (row["org"],)).fetchone()
            discovery_at = latest_discovery["checked_at"] if latest_discovery else row["last_checked"]
            if not item.get("last_checked") or discovery_at > item["last_checked"]:
                item["last_checked"] = discovery_at
                item["last_status"] = latest_discovery["status"] if latest_discovery else None
                item["last_error"] = safe_error_summary(latest_discovery["error"]) if latest_discovery else None
                item["health"] = ("last_check_failed" if latest_discovery and
                    latest_discovery["status"] in ("error", "failed", "timeout") else
                    "last_check_succeeded" if latest_discovery and latest_discovery["status"] == "success" else
                    "last_check_state_unknown")
                item["observation"] = "observed"
                try:
                    at = datetime.fromisoformat(discovery_at)
                    if at.tzinfo is None:
                        at = at.replace(tzinfo=timezone.utc)
                    item["recent"] = (datetime.now(timezone.utc) - at.astimezone(timezone.utc)).total_seconds() <= 86400
                except (TypeError, ValueError):
                    item["recent"] = False

        failures_sql = """SELECT org,source,status,updated_at,error,'collection' job_kind,id
            FROM collection_jobs WHERE status IN ('failed','partial','awaiting_access')
            UNION ALL
            SELECT org,'DJEN' AS source,status,updated_at,error,'oab' AS job_kind,id
            FROM oab_monitor_jobs WHERE status='failed'"""
        failure_rows = repository.db.execute(
            "SELECT * FROM (" + failures_sql + ") failures" +
            (" WHERE org=?" if office_id else " WHERE org<>'local'" if config.production else "") +
            " ORDER BY updated_at DESC,id DESC LIMIT 100",
            (office_id,) if office_id else ()).fetchall()
        job_failures = []
        for row in failure_rows:
            try:
                error_value = json.loads(row["error"]) if row["error"] else None
            except (TypeError, ValueError):
                error_value = {"message": row["error"]} if row["error"] else None
            if isinstance(error_value, dict):
                summary = error_value.get("code") or error_value.get("message")
            else:
                summary = str(error_value) if error_value else None
            if summary:
                summary = safe_error_summary(summary)
            job_failures.append({"org": row["org"], "source": row["source"], "status": row["status"],
                                 "updated_at": row["updated_at"], "job_kind": row["job_kind"],
                                 "job_id": row["id"], "error_summary": summary})

        try:
            from importlib.util import find_spec
            pje_runtime = find_spec("playwright") is not None
            eproc_runtime = find_spec("pydoll") is not None
        except (ImportError, ValueError):
            pje_runtime = eproc_runtime = False
        inventory = [
            {"source": "DataJud", "implemented": True, "configured": bool(config.api_key),
             "enabled": None, "runtime_dependency_available": True},
            {"source": "DJEN", "implemented": True, "configured": True,
             "enabled": None, "runtime_dependency_available": True},
            {"source": "PJe/TRF1", "implemented": True, "configured": None,
             "enabled": None, "runtime_dependency_available": pje_runtime},
            {"source": "eproc/TRF4", "implemented": True, "configured": None,
             "enabled": None, "runtime_dependency_available": eproc_runtime},
        ]
        return {"items": checks, "sources": inventory, "job_failures": job_failures,
                "worker": _platform_worker_telemetry(), "worker_running": _scheduler_running(),
                "source_of_truth": "source_checks contém somente a última consulta persistida; não executa sondagens externas."}

    @app.get("/api/platform/processes", summary="Pesquisar carteiras de todos os escritórios")
    async def platform_processes(office_id: str | None = None, q: str | None = None,
                                 offset: int = Query(default=0, ge=0),
                                 limit: int = Query(default=50, ge=1, le=200)):
        require_platform_admin()
        clauses, args = [], []
        if office_id:
            clauses.append("org=?"); args.append(office_id)
        if q:
            clauses.append("number LIKE ?"); args.append(f"%{q.strip()}%")
        if config.production:
            clauses.append("org<>'local'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total = repository.db.execute("SELECT count(*) FROM watches" + where, args).fetchone()[0]
        args.extend((limit, offset))
        rows = repository.db.execute("SELECT org,number,court,created_at,last_checked,next_due,last_error FROM watches" +
                                     where + " ORDER BY org,number LIMIT ? OFFSET ?", args).fetchall()
        audit_platform("platform_processes_search", target_org=office_id,
                       purpose="consulta global da lista de processos")
        return {"items": [dict(row) for row in rows], "total": total, "offset": offset, "limit": limit}

    @app.get("/api/platform/processes/{office_id}/{number}", summary="Consultar detalhes e histórico global do processo")
    async def platform_process_detail(office_id: str, number: str):
        require_platform_admin()
        try:
            watch = repository.get(office_id, number)
        except LookupError:
            raise HTTPException(status_code=404, detail="Processo não encontrado nesta carteira.") from None
        audit_platform("platform_process_detail", target_org=office_id, number=number,
                       purpose="consulta global de detalhes e histórico do processo")
        sources = [row[0] for row in repository.db.execute(
            "SELECT DISTINCT source FROM source_checks WHERE org=? AND number=? ORDER BY source",
            (office_id, number)).fetchall()]
        return {"process": watch, "movements": repository.timeline(office_id, number),
                "communications": repository.communications(office_id, number),
                "runs": repository.runs(office_id, number),
                "source_checks": [repository.source_coverage(office_id, number, source)
                                  for source in sources]}

    @app.post("/api/platform/processes/{office_id}/{number}/refresh", status_code=202,
              summary="Solicitar coleta autorizada de um processo")
    async def platform_refresh_process(office_id: str, number: str, payload: PlatformAccessPurposeRequest):
        require_platform_admin()
        try:
            repository.get(office_id, number)
        except LookupError:
            audit_platform("platform_process_refresh_failed", target_org=office_id, number=number,
                           purpose="atualização recusada: processo inexistente na carteira")
            raise HTTPException(status_code=404, detail="Processo não encontrado nesta carteira.") from None
        try:
            group = schedule_sync(repository, office_id, number, config,
                                  collection_type="platform_admin_refresh", priority=30)
        except (ValueError, LookupError) as exc:
            audit_platform("platform_process_refresh_failed", target_org=office_id, number=number,
                           purpose="atualização recusada por estado operacional")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        audit_platform("platform_process_refresh", target_org=office_id, number=number, purpose=payload.purpose)
        return {"status": "scheduled", "group": group}

    @app.get("/api/platform/audit", summary="Consultar registros de auditoria")
    async def platform_audit(office_id: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
        require_platform_admin()
        audit_platform("platform_audit_read", target_org=office_id, purpose="consulta do registro de auditoria da plataforma")
        sql = "SELECT id,at,action,number,actor,purpose,org FROM access_log"
        args = ()
        if office_id:
            sql += " WHERE org=?"; args = (office_id,)
        elif config.production:
            sql += " WHERE org IS NULL OR org<>'local'"
        sql += " ORDER BY id DESC LIMIT ?"
        return {"items": [dict(row) for row in repository.db.execute(sql, (*args, limit)).fetchall()]}

    @app.post("/api/auth/login")
    async def account_login(payload: LoginRequest):
        if config.auth_mode != "accounts":
            raise HTTPException(status_code=404, detail="Login por conta não está habilitado.")
        user = repository.authenticate_user(payload.email, payload.password)
        if not user or not user["offices"]:
            raise HTTPException(status_code=401, detail="E-mail ou senha inválidos.")
        available_offices = [item for item in user["offices"]
                             if not is_local_office_blocked(config.environment, item["id"])]
        if not available_offices:
            raise HTTPException(status_code=403, detail="Esta conta não possui escritório operacional disponível.")
        office_id = payload.office_id or available_offices[0]["id"]
        if is_local_office_blocked(config.environment, office_id):
            raise HTTPException(status_code=403, detail="O escritório reservado 'local' está indisponível em produção.")
        if office_id not in {item["id"] for item in user["offices"]}:
            raise HTTPException(status_code=403, detail="Usuário não pertence a esse escritório.")
        raw_token = secrets.token_urlsafe(48)
        expiry = datetime.now(timezone.utc) + timedelta(days=config.session_days)
        session_id = repository.create_auth_session(user["id"], raw_token, office_id, expiry.isoformat())
        response = JSONResponse({"status": "ok", "user": {"id": user["id"], "email": user["email"]},
                                 "active_office_id": office_id, "offices": user["offices"]})
        response.set_cookie("processwatch_session", raw_token, max_age=config.session_days * 86400,
                            httponly=True, secure=config.session_cookie_secure, samesite="strict", path="/")
        return response

    @app.get("/api/auth/me")
    async def account_me():
        identity = current_identity()
        if not identity:
            raise HTTPException(status_code=404, detail="Endpoint disponível no modo de contas.")
        session = session_context.get()
        return {"user": {"id": identity["id"], "email": identity["email"],
                          "display_name": identity.get("display_name") or identity.get("full_name") or ""},
                "active_office_id": current_org(), "role": identity["role"],
                "offices": identity["offices"], "expires_at": session["expires_at"]}

    @app.get("/api/profile")
    async def profile():
        identity = current_identity()
        if not identity:
            raise HTTPException(status_code=404, detail="Perfil disponível no modo de contas.")
        return {"user": {"id": identity["id"], "email": identity["email"],
                         "display_name": identity.get("display_name") or identity.get("full_name") or ""},
                "office_id": current_org(), "role": identity["role"]}

    @app.put("/api/profile")
    async def update_profile(payload: ProfileUpdateRequest):
        identity = current_identity()
        if not identity:
            raise HTTPException(status_code=404, detail="Perfil disponível no modo de contas.")
        repository.update_membership_display_name(current_org(), identity["id"], payload.display_name)
        record_access("profile_name_update", None, "atualização do próprio perfil")
        return {"status": "updated", "display_name": payload.display_name.strip()}

    @app.post("/api/profile/password")
    async def change_password(payload: ChangePasswordRequest):
        identity = current_identity()
        if not identity:
            raise HTTPException(status_code=404, detail="Perfil disponível no modo de contas.")
        try:
            repository.change_user_password(identity["id"], payload.new_password,
                                            current_password=payload.current_password)
        except PermissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        record_access("password_change", None, "alteração de senha pelo usuário")
        response = JSONResponse({"status": "password_changed", "message": "Senha alterada. Entre novamente."})
        response.delete_cookie("processwatch_session", path="/", httponly=True,
                               secure=config.session_cookie_secure, samesite="strict")
        return response

    @app.delete("/api/profile", summary="Excluir a própria conta")
    async def delete_profile(payload: AccountDeleteRequest):
        identity = current_identity()
        if not identity:
            raise HTTPException(status_code=404, detail="Perfil disponível no modo de contas.")
        try:
            repository.delete_own_account(identity["id"], payload.current_password)
        except PermissionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        record_access("account_delete", None, "exclusão da própria conta")
        response = JSONResponse({"status": "deleted"})
        response.delete_cookie("processwatch_session", path="/", httponly=True,
                               secure=config.session_cookie_secure, samesite="strict")
        return response

    @app.post("/api/auth/recovery/request", status_code=202)
    async def recovery_request(payload: RecoveryRequest):
        # No SMTP is configured. The response deliberately reveals no account state and
        # never creates or returns a bearer recovery token through the public API.
        return {"status": "accepted", "message": "Se a conta existir, solicite a recuperação ao administrador do escritório."}

    @app.post("/api/auth/recovery/complete")
    async def recovery_complete(payload: RecoveryCompleteRequest):
        user = repository.find_user(payload.email)
        if not user or not user["active"]:
            raise HTTPException(status_code=400, detail="Token inválido ou expirado.")
        try:
            repository.change_user_password(user["id"], payload.new_password, recovery_token=payload.token)
        except PermissionError:
            raise HTTPException(status_code=400, detail="Token inválido ou expirado.") from None
        repository.record_access("password_recovery_complete", None,
            "redefinição de senha concluída com token de uso único",
            actor=f"user:{user['id']}", org=None)
        response = JSONResponse({"status": "password_changed", "message": "Senha redefinida. Entre novamente."})
        response.delete_cookie("processwatch_session", path="/", httponly=True,
                               secure=config.session_cookie_secure, samesite="strict")
        return response

    @app.get("/api/office", summary="Consultar dados do escritório ativo",
             description="Retorna nome, e-mail, telefone e endereço do escritório da sessão ou do escopo autorizado. Requer autenticação.")
    async def get_office():
        office = repository.office(current_org())
        if not office:
            raise HTTPException(status_code=404, detail="Escritório não encontrado.")
        return office

    @app.put("/api/office", summary="Atualizar dados do escritório ativo",
             description="Exige administrador do escritório em sessão ou administrador legado no modo token. Sessões por cookie exigem Origin válido.")
    async def update_office(payload: OfficeUpdateRequest):
        require_credential_administrator()
        try:
            office = repository.update_office(current_org(), **payload.model_dump())
        except ValueError as exc:
            record_access("office_update_failed", None, "edição de escritório recusada por validação")
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except LookupError as exc:
            record_access("office_update_failed", None, "edição de escritório recusada: escritório inexistente")
            raise HTTPException(status_code=404, detail=str(exc)) from None
        record_access("office_update", None, "atualização dos dados do escritório")
        return office

    @app.post("/api/office/deactivate", summary="Desativar o escritório ativo")
    async def deactivate_office(payload: OfficeLifecycleRequest):
        require_office_admin()
        if payload.office_id != current_org():
            record_access("office_deactivate_failed", None, "desativação recusada: identificador incompatível")
            raise HTTPException(status_code=403, detail="O identificador não corresponde ao escritório autenticado.")
        try:
            office = repository.set_office_active(current_org(), False)
        except ValueError as exc:
            record_access("office_deactivate_failed", None, "desativação recusada por regra operacional")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        record_access("office_deactivate", None, "desativação do próprio escritório")
        return office

    @app.post("/api/office/activate", summary="Reativar o próprio escritório")
    async def activate_office(payload: OfficeLifecycleRequest):
        require_office_admin()
        if payload.office_id != current_org():
            record_access("office_activate_failed", None, "reativação recusada: identificador incompatível")
            raise HTTPException(status_code=403, detail="O identificador não corresponde ao escritório autenticado.")
        try:
            office = repository.set_office_active(current_org(), True)
        except ValueError as exc:
            record_access("office_activate_failed", None, "reativação recusada por regra operacional")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        record_access("office_activate", None, "reativação do próprio escritório")
        return office

    @app.delete("/api/office", summary="Encerrar e excluir definitivamente o escritório ativo")
    async def delete_office(payload: OfficeLifecycleRequest):
        require_office_admin()
        if payload.office_id != current_org():
            record_access("office_delete_failed", None, "exclusão recusada: identificador incompatível")
            raise HTTPException(status_code=403, detail="O identificador não corresponde ao escritório autenticado.")
        try:
            repository.delete_office(current_org())
        except ValueError as exc:
            record_access("office_delete_failed", None, "exclusão recusada por job em andamento ou regra operacional")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except RuntimeError as exc:
            record_access("office_delete_failed", None, "exclusão revertida por política ou integridade")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except sqlite3.IntegrityError:
            record_access("office_delete_failed", None, "exclusão revertida por conflito de integridade")
            raise HTTPException(status_code=409, detail="A exclusão foi cancelada por vínculo de dados não resolvido.") from None
        repository.record_access("office_delete", None, "escritório encerrado: " + payload.office_id,
                                 actor=current_identity()["email"], org=None)
        response = JSONResponse({"status": "deleted", "office_id": payload.office_id})
        response.delete_cookie("processwatch_session", path="/", httponly=True,
                               secure=config.session_cookie_secure, samesite="strict")
        return response

    @app.get("/api/admin/users", summary="Listar usuários do escritório",
             description="Somente o administrador autenticado consulta os vínculos do escritório ativo.")
    async def office_users():
        require_office_admin()
        return {"office_id": current_org(), "items": repository.office_users(current_org())}

    @app.post("/api/admin/users", status_code=201)
    async def add_office_user(payload: OfficeUserCreateRequest):
        require_office_admin()
        try:
            result = repository.ensure_office_membership(current_org(), payload.email, payload.role,
                password=payload.temporary_password, full_name=payload.full_name)
        except ValueError as exc:
            record_access("office_user_add_failed", None, "criação/vínculo de usuário recusado por validação")
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except sqlite3.IntegrityError:
            record_access("office_user_add_failed", None, "criação/vínculo de usuário recusado por conflito")
            raise HTTPException(status_code=409, detail="Não foi possível vincular essa conta ao escritório.") from None
        record_access("office_user_add", None, "gestão de usuários do escritório")
        return result

    @app.post("/api/admin/users/{user_id}/recovery", status_code=201,
              summary="Emitir token de recuperação de senha",
              description="Somente administrador do escritório. Token único válido por 30 minutos, retornado uma vez. "
                          "A entrega é manual por canal privado; não há envio automático de e-mail.")
    async def office_issue_user_recovery(user_id: int):
        identity = require_office_admin()
        try:
            issued = repository.issue_office_password_recovery_token(
                user_id, current_org(), actor=f"office_admin:{identity['email']}", ttl_minutes=30)
        except LookupError:
            record_access("office_user_recovery_failed", None, f"recuperação recusada: user_id={user_id} inexistente")
            raise HTTPException(status_code=404, detail="Conta ativa não encontrada neste escritório.") from None
        record_access("office_user_recovery_issue", None, f"token emitido para user_id={user_id}")
        return {"status": "issued", "user_id": user_id, **issued,
                "message": "Token de uso único. Compartilhe somente em canal privado; não há envio por e-mail."}

    @app.get("/api/admin/users/{user_id}", summary="Consultar usuário do escritório")
    async def office_user(user_id: int):
        require_office_admin()
        user = repository.office_user(current_org(), user_id)
        if not user:
            record_access("office_user_detail_failed", None, f"consulta recusada: user_id={user_id} não vinculado")
            raise HTTPException(status_code=404, detail="Usuário não vinculado a este escritório.")
        record_access("office_user_detail", None, f"consulta de user_id={user_id}")
        return user

    @app.patch("/api/admin/users/{user_id}", summary="Atualizar parcialmente usuário do escritório",
               description="Aceita role, active e display_name separadamente. Campos omitidos preservam o valor atual.")
    async def update_office_user(user_id: int, payload: OfficeUserUpdateRequest):
        require_office_admin()
        try:
            repository.update_office_membership(current_org(), user_id, **payload.model_dump(exclude_unset=True))
        except LookupError as exc:
            record_access("office_user_update_failed", None, f"atualização recusada: user_id={user_id} não vinculado")
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            record_access("office_user_update_failed", None, f"atualização recusada por regra: user_id={user_id}")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        record_access("office_user_update", None, "gestão de usuários do escritório")
        return {"status": "updated", "user_id": user_id}

    @app.delete("/api/admin/users/{user_id}")
    async def remove_office_user(user_id: int):
        require_office_admin()
        try:
            removed = repository.remove_office_membership(current_org(), user_id)
        except ValueError as exc:
            record_access("office_user_remove_failed", None, f"remoção recusada por regra: user_id={user_id}")
            raise HTTPException(status_code=409, detail=str(exc)) from None
        if not removed:
            record_access("office_user_remove_failed", None, f"remoção recusada: user_id={user_id} não vinculado")
            raise HTTPException(status_code=404, detail="Usuário não vinculado a este escritório.")
        record_access("office_user_remove", None, "remoção de vínculo do escritório")
        return {"status": "removed", "user_id": user_id}

    @app.post("/api/service-credentials", status_code=201,
              summary="Criar credencial de serviço",
              description="Somente administradores. O token secreto aparece apenas nesta resposta; guarde-o com segurança.")
    async def create_service_credential(payload: ServiceCredentialCreateRequest):
        actor = require_credential_administrator()
        permissions = set(payload.permissions)
        unknown = permissions - SERVICE_PERMISSIONS
        if unknown:
            raise HTTPException(status_code=400, detail="A solicitação contém permissões desconhecidas.")
        if any(ord(character) < 32 for character in payload.name):
            raise HTTPException(status_code=400, detail="O nome do serviço contém caracteres inválidos.")
        expires_at = (datetime.now(timezone.utc) + timedelta(days=payload.expires_in_days)).isoformat()
        metadata, raw_token = repository.create_service_credential(current_org(), payload.name,
            permissions, expires_at, created_by=actor)
        record_access("service_credential_create", None, "criação de credencial de serviço")
        return {"credential": metadata, "token": raw_token,
                "warning": "Copie agora: o token completo não será exibido novamente."}

    @app.get("/api/service-credentials", summary="Listar metadados das credenciais",
             description="Somente administradores do escritório ativo. A listagem nunca inclui o token secreto.")
    async def list_service_credentials():
        require_credential_administrator()
        return {"items": repository.service_credentials(current_org())}

    @app.post("/api/service-credentials/{credential_id}/revoke", summary="Revogar credencial",
              description="Invalida imediatamente uma credencial ativa do escritório autenticado.")
    async def revoke_service_credential(credential_id: str):
        require_credential_administrator()
        if not repository.revoke_service_credential(current_org(), credential_id):
            raise HTTPException(status_code=404, detail="Credencial ativa não encontrada neste escopo.")
        record_access("service_credential_revoke", None, "revogação de credencial de serviço")
        return {"status": "revoked", "credential_id": credential_id}

    @app.post("/api/service-credentials/{credential_id}/rotate", summary="Rotacionar credencial",
              description="Revoga a credencial antiga e cria outra com as mesmas permissões. O novo segredo aparece somente nesta resposta.")
    async def rotate_service_credential(credential_id: str,
                                        payload: ServiceCredentialRotateRequest | None = None):
        actor = require_credential_administrator()
        ttl = payload.expires_in_days if payload else 90
        expires_at = (datetime.now(timezone.utc) + timedelta(days=ttl)).isoformat()
        rotated = repository.rotate_service_credential(current_org(), credential_id, expires_at,
                                                       created_by=actor)
        if not rotated:
            raise HTTPException(status_code=404, detail="Credencial ativa não encontrada neste escopo.")
        metadata, raw_token = rotated
        record_access("service_credential_rotate", None, "rotação de credencial de serviço")
        return {"credential": metadata, "token": raw_token,
                "warning": "Copie agora: o token completo não será exibido novamente. A credencial anterior foi revogada."}

    @app.post("/api/auth/office")
    async def select_office(payload: SelectOfficeRequest):
        session = session_context.get()
        if is_local_office_blocked(config.environment, payload.office_id):
            raise HTTPException(status_code=403, detail="O escritório reservado 'local' está indisponível em produção.")
        if not session or not repository.set_session_office(session["session_id"], session["user_id"], payload.office_id):
            raise HTTPException(status_code=403, detail="Usuário não pertence a esse escritório.")
        return {"status": "ok", "active_office_id": payload.office_id}

    @app.post("/api/auth/logout")
    async def account_logout():
        session = session_context.get()
        if session:
            repository.revoke_auth_session(session["session_id"])
        response = JSONResponse({"status": "ok"})
        response.delete_cookie("processwatch_session", path="/", httponly=True,
                               secure=config.session_cookie_secure, samesite="strict")
        return response

    @app.get("/api/auth/verify")
    async def verify_access():
        identity = current_identity()
        return {"status": "ok", "mode": config.auth_mode,
                "user": {"id": identity["id"], "email": identity["email"]} if identity else None,
                "organization": current_org()}

    @app.post("/api/extension/trf4/nonce")
    async def extension_trf4_nonce(request: Request):
        _require_extension_origin(request)
        stamp = time.monotonic()
        app.state.extension_nonces = {key: expiry for key, expiry in app.state.extension_nonces.items()
                                      if expiry > stamp}
        nonce = secrets.token_urlsafe(32)
        app.state.extension_nonces[nonce] = stamp + 120
        return {"nonce": nonce, "expires_in_seconds": 120}

    @app.post("/api/extension/trf4/import", status_code=201)
    async def extension_trf4_import(request: Request):
        _require_extension_origin(request)
        nonce = request.headers.get("x-processwatch-nonce", "")
        expiry = app.state.extension_nonces.pop(nonce, None)
        if not expiry or expiry <= time.monotonic():
            raise HTTPException(status_code=403, detail="Confirmação da extensão expirada; capture novamente.")
        content_length = request.headers.get("content-length")
        if content_length and (not content_length.isdigit() or int(content_length) > 1_000_000):
            raise HTTPException(status_code=413, detail="Captura acima do limite de 1 MB.")
        body = await request.body()
        if len(body) > 1_000_000:
            raise HTTPException(status_code=413, detail="Captura acima do limite de 1 MB.")
        if "application/json" not in request.headers.get("content-type", "").casefold():
            raise HTTPException(status_code=415, detail="Envie a captura como application/json.")
        try:
            payload = json.loads(body)
            number = normalize(payload.get("process_number", ""))
            _, watch = registered(number)
            normalized = normalize_capture(payload, number)
        except HTTPException:
            raise
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc) or "Captura inválida.") from None
        if tribunal(number) != "trf4":
            raise HTTPException(status_code=422, detail="A importação assistida está restrita ao TRF4.")
        previous = repository.latest_court_snapshot(current_org(), number, TRF4_ASSISTED_SOURCE)
        merged = merge_capture(previous["data"] if previous else None, normalized)
        baseline = previous is None
        snapshot_id = repository.save_court_snapshot(
            current_org(), number, TRF4_ASSISTED_SOURCE, merged,
            status="captura assistida concluída", coverage=merged.get("coverage"),
            baseline_if_first=baseline)
        record_access("trf4_assisted_import", number, "importação assistida confirmada pelo usuário")
        return {"status": "imported", "number": number, "court": "TRF4",
                "source": TRF4_ASSISTED_SOURCE, "snapshot_id": snapshot_id,
                "movement_count": len(merged["movements"]), "newly_observed_count":
                    0 if baseline else sum(1 for item in normalized["movements"]
                                           if not (previous and any(old.get("source_id") == item.get("source_id")
                                                                    for old in previous["data"].get("movements", [])))),
                "baseline": baseline, "pagination_status": merged["coverage"]["pagination_status"],
                "complete": False, "message": "Foram importados somente os movimentos presentes no DOM durante a captura manual."}

    @app.post("/api/extension/trf4/queue-import", status_code=202)
    async def extension_trf4_queue_import(request: Request):
        """Validate a user-confirmed browser capture and hand persistence to the shared worker."""
        _require_extension_origin(request)
        nonce = request.headers.get("x-processwatch-nonce", "")
        expiry = app.state.extension_nonces.pop(nonce, None)
        if not expiry or expiry <= time.monotonic():
            raise HTTPException(status_code=403, detail="Confirmação da extensão expirada; capture novamente.")
        content_length = request.headers.get("content-length")
        if content_length and (not content_length.isdigit() or int(content_length) > 1_000_000):
            raise HTTPException(status_code=413, detail="Captura acima do limite de 1 MB.")
        body = await request.body()
        if len(body) > 1_000_000:
            raise HTTPException(status_code=413, detail="Captura acima do limite de 1 MB.")
        if "application/json" not in request.headers.get("content-type", "").casefold():
            raise HTTPException(status_code=415, detail="Envie a captura como application/json.")
        try:
            payload = json.loads(body)
            number = normalize(payload.get("process_number", ""))
            _, watch = registered(number)
            normalized = normalize_capture(payload, number)
        except HTTPException:
            raise
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc) or "Captura inválida.") from None
        if tribunal(number) != "trf4":
            raise HTTPException(status_code=422, detail="A captura assistida está restrita ao TRF4.")
        group = repository.enqueue_collection_group(current_org(), number, "authorized_browser_capture",
            include_datajud=False, include_djen=False, additional_sources=(TRF4_EPROC_SOURCE,),
            job_parameters={"authorized_capture": normalized})
        court_job = next(item for item in group["jobs"] if item["source"] == TRF4_EPROC_SOURCE)
        return {"status": "queued", "number": number, "source": TRF4_ASSISTED_SOURCE,
                "group_id": group["group_id"], "job_id": court_job["id"],
                "job_status": court_job["status"], "movement_count": len(normalized["movements"]),
                "pagination_status": normalized["coverage"]["pagination_status"],
                "complete": False,
                "message": "Captura recebida; o worker local processará os movimentos. A captura assistida não comprova o histórico completo."}

    @app.get("/api/discovery/capabilities")
    async def discovery_capabilities():
        return {
            "available": [{"criterion": "oab_uf", "label": "OAB + UF", "source": "DJEN",
                           "status": "automated_limited",
                           "coverage": "Publicações/comunicações retornadas no período; não equivale à lista completa de processos do advogado."}],
            "unavailable": [
                {"criterion": "party_name", "label": "Nome da parte", "status": "not_automated"},
                {"criterion": "cpf_cnpj", "label": "CPF/CNPJ", "status": "not_automated"},
                {"criterion": "lawyer_name", "label": "Nome do advogado", "status": "not_automated"},
            ],
            "note": "Portais de alguns tribunais permitem pesquisa manual por esses dados; uma interface pública não comprova integração automatizada autorizada.",
        }

    def valid_oab(oab, uf, start, end):
        try:
            criterion = by_oab(oab, uf, "DJEN")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if end < start or (end - start).days > 30:
            raise HTTPException(status_code=400, detail="Informe período explícito de até 31 dias.")
        return criterion

    def discovery_page(criterion, start, end, page, size):
        result = repository.oab_discovery_page(current_org(), criterion.oab, criterion.uf,
                                               start.isoformat(), end.isoformat(), page, size)
        for item in result["items"]:
            item["formatted_number"] = format_number(item["number"])
        return {"source": "DJEN", "coverage": repository.latest_oab_discovery_run(
                    current_org(), criterion.oab, criterion.uf, start.isoformat(), end.isoformat()),
                "period_start": start.isoformat(), "period_end": end.isoformat(),
                "warning": "Processos identificados por publicações no período; isso não representa todos os processos do advogado.",
                **result}

    @app.post("/api/discovery/oab/search")
    async def search_oab(payload: OabSearchRequest):
        criterion = valid_oab(payload.oab, payload.uf, payload.inicio, payload.fim)
        try:
            await discover_oab(repository, djen_client, current_org(), criterion.oab, criterion.uf,
                               payload.inicio, payload.fim)
        except DjenError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        record_access("oab_discovery", None, "descoberta de publicações por OAB/UF")
        return discovery_page(criterion, payload.inicio, payload.fim, 1, 20)

    @app.get("/api/discovery/oab/results")
    async def oab_results(oab: str, uf: str, inicio: date, fim: date,
                          page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=50)):
        criterion = valid_oab(oab, uf, inicio, fim)
        record_access("oab_discovery_results", None, "consulta de publicações por OAB/UF")
        return discovery_page(criterion, inicio, fim, page, size)

    @app.post("/api/discovery/oab/add")
    async def add_oab_selection(payload: OabAddRequest):
        criterion = valid_oab(payload.oab, payload.uf, payload.inicio, payload.fim)
        portfolio = payload.portfolio.strip()
        if not portfolio:
            raise HTTPException(status_code=400, detail="Informe o nome da carteira.")
        numbers = []
        try:
            for value in payload.numbers:
                normalized = normalize(value)
                if normalized not in numbers:
                    numbers.append(normalized)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        for number in numbers:
            if not repository.was_discovered_by_oab(current_org(), criterion.oab, criterion.uf, number,
                    payload.inicio.isoformat(), payload.fim.isoformat()):
                raise HTTPException(status_code=400, detail="Seleção contém processo sem publicação comprovada neste filtro.")
        results = []
        for number in numbers:
            try:
                existing = bool(repository.db.execute("SELECT 1 FROM watches WHERE org=? AND number=?", (current_org(), number)).fetchone())
                if not existing:
                    engine.register(number, current_org(), payload.interval or config.interval, config.max_processes)
                repository.add_to_portfolio(current_org(), portfolio, number)
                attached = repository.attach_oab_communications(
                    current_org(), criterion.oab, criterion.uf, number,
                    payload.inicio.isoformat(), payload.fim.isoformat())
                sync = schedule_sync(repository, current_org(), number, config, collection_type="oab_add", priority=40)
                results.append({"number": number, "status": "já monitorado" if existing else "adicionado",
                                "sync_group_id": sync.get("group_id"), "sync_status": sync["status"],
                                "sync_jobs": sync.get("jobs", []),
                                "attached_communications": attached})
            except ValueError as exc:
                results.append({"number": number, "status": "erro", "error": str(exc)})
        record_access("oab_discovery_add", None, "inclusão explícita de processos em carteira")
        return {"portfolio": portfolio, "results": results,
                "added": sum(item["status"] == "adicionado" for item in results),
                "failed": sum(item["status"] == "erro" for item in results),
                "synchronization": "jobs DataJud e DJEN enfileirados quando os dados estavam ausentes ou vencidos"}

    @app.post("/api/monitoramentos/oab", status_code=201)
    async def create_oab_monitor(payload: OabMonitorCreateRequest):
        try:
            criterion = by_oab(payload.oab, payload.uf, "DJEN")
            monitor = repository.create_oab_monitor(current_org(), criterion.oab, criterion.uf,
                lawyer_name=payload.lawyer_name, enabled=payload.enabled,
                interval_hours=payload.interval_hours)
        except ValueError as exc:
            raise HTTPException(status_code=409 if "já está cadastrada" in str(exc) else 400,
                                detail=str(exc)) from None
        record_access("oab_monitor_create", None, "cadastro de monitoramento recorrente DJEN por OAB/UF")
        return monitor

    @app.get("/api/monitoramentos/oab")
    async def list_oab_monitors():
        return {"items": repository.list_oab_monitors(current_org()),
                "scheduler_running": _scheduler_running()}

    @app.get("/api/monitoramentos/oab/{monitor_id}")
    async def get_oab_monitor(monitor_id: int):
        monitor = repository.oab_monitor(current_org(), monitor_id)
        if not monitor:
            raise HTTPException(status_code=404, detail="Monitoramento OAB não encontrado.")
        monitor["scheduler_running"] = _scheduler_running()
        return monitor

    @app.patch("/api/monitoramentos/oab/{monitor_id}")
    async def update_oab_monitor(monitor_id: int, payload: OabMonitorUpdateRequest):
        try:
            monitor = repository.update_oab_monitor(current_org(), monitor_id,
                enabled=payload.enabled, interval_hours=payload.interval_hours,
                lawyer_name=payload.lawyer_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        if not monitor:
            raise HTTPException(status_code=404, detail="Monitoramento OAB não encontrado.")
        record_access("oab_monitor_update", None, "alteração de monitoramento recorrente DJEN por OAB/UF")
        return monitor

    @app.delete("/api/monitoramentos/oab/{monitor_id}")
    async def delete_oab_monitor(monitor_id: int):
        if not repository.delete_oab_monitor(current_org(), monitor_id):
            raise HTTPException(status_code=404, detail="Monitoramento OAB não encontrado.")
        record_access("oab_monitor_delete", None, "remoção do agendamento OAB; comunicações preservadas")
        return {"deleted": True, "monitor_id": monitor_id, "communications_preserved": True}

    @app.post("/api/monitoramentos/oab/{monitor_id}/atualizar", status_code=202)
    async def refresh_oab_monitor(monitor_id: int):
        try:
            job = repository.enqueue_oab_monitor_job(current_org(), monitor_id, manual=True)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        record_access("oab_monitor_refresh", None, "consulta DJEN por OAB/UF enfileirada manualmente")
        return {"monitor_id": monitor_id, "job_id": job["id"], "status": job["status"],
                "window_start": job["window_start"], "window_end": job["window_end"],
                "run_kind": job["run_kind"]}

    @app.get("/api/monitoramentos/oab/{monitor_id}/comunicacoes")
    async def oab_monitor_communications(monitor_id: int,
        inicio: date | None = None, fim: date | None = None,
        page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100)):
        if inicio and fim and fim < inicio:
            raise HTTPException(status_code=400, detail="Período final anterior ao inicial.")
        result = repository.oab_monitor_communications(current_org(), monitor_id,
            start=inicio.isoformat() if inicio else None, end=fim.isoformat() if fim else None,
            page=page, size=size)
        if result is None:
            raise HTTPException(status_code=404, detail="Monitoramento OAB não encontrado.")
        for item in result["items"]:
            item["payload"] = json.loads(item["payload"])
            item["formatted_number"] = format_number(item["number"])
            item["is_read"] = bool(item["read_at"])
        return result

    @app.get("/api/processes")
    async def list_processes():
        return [
            {
                "number": row["number"],
                "formatted_number": format_number(row["number"]),
                "court": row["court"],
                "interval": row["interval"],
                "created_at": row["created_at"],
                "last_checked": row["last_checked"],
                "next_due": row["next_due"],
                "last_error": row["last_error"],
                "schedule": repository.monitor_schedule(current_org(), row["number"]),
            }
            for row in repository.list_watches(current_org())
        ]

    @app.get("/api/processes/{number}/schedule")
    async def get_process_schedule(number: str):
        try:
            normalized = normalize(number)
            schedule = repository.monitor_schedule(current_org(), normalized)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "não cadastrado" in str(exc) else 400,
                                detail=str(exc)) from None
        jobs = repository.scheduled_job_group(current_org(), normalized)
        schedule["current_job_status"] = next((job["status"] for job in jobs
            if job["status"] in ("queued", "running")), None)
        schedule["scheduler_running"] = _scheduler_running()
        return schedule

    @app.put("/api/processes/{number}/schedule")
    async def update_process_schedule(number: str, payload: MonitorScheduleRequest):
        try:
            normalized = normalize(number)
            repository.get(current_org(), normalized)
            schedule = repository.configure_monitor_schedule(
                current_org(), normalized, enabled=payload.enabled, interval_hours=payload.interval_hours)
        except ValueError as exc:
            status = 404 if "não cadastrado" in str(exc) else 400
            raise HTTPException(status_code=status, detail=str(exc)) from None
        record_access("configure_process_schedule", normalized,
                                 "configuração de atualização automática local")
        schedule["scheduler_running"] = _scheduler_running()
        return schedule

    @app.get("/api/processes/overview")
    async def process_overview(q: str | None = None, court: str | None = None,
                               state: str | None = None, sort: str = "recent",
                               unread_only: bool = False,
                               lawyer: str | None = None,
                               situation_filter: str | None = None,
                               monitoring_status: str | None = None,
                               page: int = Query(default=1, ge=1),
                               size: int = Query(default=10, ge=1, le=100)):
        """Authenticated read-only portfolio summary; never schedules or expires work."""
        if state not in (None, "updated", "pending", "running", "failed", "human", "not_queried"):
            raise HTTPException(status_code=400, detail="Estado de filtro inválido.")
        if sort not in ("recent", "number", "court", "situation", "movement"):
            raise HTTPException(status_code=400, detail="Ordenação inválida.")
        if monitoring_status not in (None, "active", "inactive"):
            raise HTTPException(status_code=400, detail="Status de monitoramento inválido.")
        for label, value in (("advogado", lawyer), ("situação", situation_filter)):
            if value is not None and (len(value) > 160 or not value.strip()):
                raise HTTPException(status_code=400, detail=f"Filtro de {label} inválido.")
        result = build_overview(repository, current_org(), q=(q or "").strip() or None,
                                court=(court or "").strip() or None, state=state,
                                unread_only=unread_only,
                                sort=sort, page=page, size=size,
                                lawyer=(lawyer or "").strip() or None,
                                situation_filter=(situation_filter or "").strip() or None,
                                monitoring_status=monitoring_status)
        result["scheduler"] = _scheduler_summary()
        return result

    @app.get("/api/calendar")
    async def calendar_items(item_type: str | None = None, status: str | None = None,
                             start: str | None = None, end: str | None = None,
                             number: str | None = None):
        allowed_types = {"hearing", "meeting", "task", "deadline"}
        allowed_statuses = {"open", "confirmed", "discarded", "scheduled", "completed", "cancelled"}
        if item_type and item_type not in allowed_types:
            raise HTTPException(status_code=400, detail="Tipo de compromisso inválido.")
        if status and status not in allowed_statuses:
            raise HTTPException(status_code=400, detail="Situação inválida.")
        normalized = None
        if number:
            normalized, _ = registered(number)
        return {"items": repository.list_calendar_items(current_org(), item_type=item_type,
                    status=status, start=start, end=end, number=normalized),
                "timezone": "UTC no armazenamento; apresentação local no navegador"}

    @app.get("/api/communications")
    async def portfolio_communications(oab: str | None = None, uf: str | None = None,
        number: str | None = None, q: str | None = None, inicio: date | None = None, fim: date | None = None,
        read: bool | None = None, monitored: bool | None = None, kind: str | None = None,
        page: int = Query(default=1, ge=1), size: int = Query(default=20, ge=1, le=100)):
        """Read the shared local DJEN inbox, including communications outside the portfolio."""
        normalized = None
        if number:
            try:
                normalized = normalize(number)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
        normalized_oab = normalized_uf = None
        if oab or uf:
            if not oab or not uf:
                raise HTTPException(status_code=400, detail="Informe OAB e UF juntos.")
            try:
                criterion = by_oab(oab, uf, "DJEN")
                normalized_oab, normalized_uf = criterion.oab, criterion.uf
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
        if inicio and fim and fim < inicio:
            raise HTTPException(status_code=400, detail="Período final anterior ao inicial.")
        if kind not in (None, "intimation", "other"):
            raise HTTPException(status_code=400, detail="Tipo de comunicação inválido.")
        if q is not None and len(q) > 160:
            raise HTTPException(status_code=400, detail="Busca de comunicação acima do limite.")
        result = repository.inbox_communications(current_org(), oab=normalized_oab,
            uf=normalized_uf, number=normalized, q=q.strip() if q else None,
            start=inicio.isoformat() if inicio else None,
            end=fim.isoformat() if fim else None, read=read, monitored=monitored,
            kind=kind, page=page, size=size)
        for item in result["items"]:
            payload = item["payload"]
            item["number"] = item["number"]
            item["formatted_number"] = format_number(item["number"])
            item["court"] = item.get("court")
            item["communication"] = payload
            item["is_read"] = bool(item.get("read_at"))
        record_access("portfolio_communications", None, "leitura local da caixa de comunicações DJEN")
        return result

    @app.get("/api/communications/{number}/{source_id}")
    async def communication_detail(number: str, source_id: str):
        try:
            normalized = normalize(number)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        result = repository.communication_detail(current_org(), normalized, source_id)
        if not result:
            raise HTTPException(status_code=404, detail="Comunicação não encontrada.")
        record_access("communication_detail", normalized, "leitura local de publicação DJEN")
        return result

    @app.patch("/api/communications/{number}/{source_id}/read")
    async def set_communication_read(number: str, source_id: str, payload: CommunicationReadRequest):
        try:
            normalized = normalize(number)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        changed = repository.set_communication_read(current_org(), normalized, source_id, read=payload.read)
        if not changed:
            raise HTTPException(status_code=404, detail="Comunicação não encontrada.")
        record_access("communication_read_state", normalized,
                      "marcação interna de leitura; não equivale a ciência jurídica oficial")
        return {"number": normalized, "source_id": source_id, "read": payload.read,
                "legal_notice": "Estado interno do ProcessWatch; não representa ciência jurídica formal."}

    @app.post("/api/calendar", status_code=201)
    async def create_calendar_item(payload: CalendarItemCreateRequest):
        allowed_types = {"hearing", "meeting", "task", "deadline"}
        if payload.item_type not in allowed_types:
            raise HTTPException(status_code=400, detail="Tipo de compromisso inválido.")
        allowed_statuses = ({"open", "confirmed", "discarded"} if payload.item_type == "deadline"
                            else {"scheduled", "completed", "cancelled"})
        status = payload.status or ("open" if payload.item_type == "deadline" else "scheduled")
        if status not in allowed_statuses:
            raise HTTPException(status_code=400, detail="Situação incompatível com o tipo de registro.")
        def normalized_datetime(value):
            if value is None:
                return None
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=400, detail="Data ou horário inválido.") from None
            if parsed.tzinfo is None:
                raise HTTPException(status_code=400, detail="Informe o fuso horário da data.")
            return parsed.astimezone(timezone.utc).isoformat()
        try:
            number = normalize(payload.number) if payload.number else None
            item = repository.create_calendar_item(current_org(), item_type=payload.item_type,
                title=payload.title, starts_at=normalized_datetime(payload.starts_at),
                ends_at=normalized_datetime(payload.ends_at), number=number, status=status,
                responsible=payload.responsible, notes=payload.notes,
                created_by=(current_identity() or {}).get("email"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        record_access("calendar_item_create", number, "registro manual de agenda ou prazo")
        return item

    @app.patch("/api/calendar/{item_id}")
    async def update_calendar_item(item_id: str, payload: CalendarItemUpdateRequest):
        current = repository.calendar_item(current_org(), item_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Registro não encontrado neste escritório.")
        allowed = {"open", "confirmed", "discarded"} if current["item_type"] == "deadline" else {"scheduled", "completed", "cancelled"}
        if payload.status is not None and payload.status not in allowed:
            raise HTTPException(status_code=400, detail="Situação incompatível com o tipo de registro.")
        def normalized_datetime(value):
            if value is None:
                return None
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=400, detail="Data ou horário inválido.") from None
            if parsed.tzinfo is None:
                raise HTTPException(status_code=400, detail="Informe o fuso horário da data.")
            return parsed.astimezone(timezone.utc).isoformat()
        try:
            item = repository.update_calendar_item(current_org(), item_id,
                title=payload.title, starts_at=normalized_datetime(payload.starts_at),
                ends_at=normalized_datetime(payload.ends_at), status=payload.status,
                responsible=payload.responsible, notes=payload.notes,
                number=normalize(payload.number) if payload.number else None)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        record_access("calendar_item_update", item.get("number"), "atualização manual de agenda ou prazo")
        return item

    @app.get("/api/reports/summary")
    async def report_summary(start: date | None = None, end: date | None = None):
        if start and end and start > end:
            raise HTTPException(status_code=400, detail="Período inicial posterior ao período final.")
        overview = build_overview(repository, current_org(), page=1, size=100)
        active_schedules = sum(bool(item.get("schedule", {}).get("enabled")) for item in overview["items"])
        source_failures = sum(source.get("status") in ("failed", "partial", "human")
                              for item in overview["items"] for source in item["sources"] if source.get("applicable"))
        communication_count = sum(source.get("records_count", 0) for item in overview["items"]
                                  for source in item["sources"] if source.get("source") == "DJEN")
        calendar = repository.list_calendar_items(current_org(), item_type="deadline")
        today = date.today()
        upcoming = sum(item["status"] in ("open", "confirmed") and
                       today <= datetime.fromisoformat(item["starts_at"].replace("Z", "+00:00")).date()
                       <= today + timedelta(days=7) for item in calendar)
        return {"processes": overview["summary"]["total"], "active_schedules": active_schedules,
                "unread_movements": overview["summary"]["unread_total"],
                "communications": communication_count, "upcoming_deadlines": upcoming,
                "source_failures": source_failures,
                "by_court": [{"court": court, "count": sum(item["court"] == court for item in overview["items"])}
                              for court in sorted({item["court"] for item in overview["items"]})],
                "generated_at": now(), "period": {"start": start.isoformat() if start else None,
                                                     "end": end.isoformat() if end else None},
                "notes": {"communications": "Contagem coletada; o motor não possui leitura separada de comunicações.",
                          "deadlines": "Somente prazos cadastrados manualmente; não há cálculo jurídico automático."}}

    @app.get("/api/processes/activity")
    async def portfolio_activity(start: str, end: str, kind: str = "movement",
                                 court: str | None = None, limit: int = Query(default=100, ge=1, le=1000)):
        """Batch local activity for dashboard, reports, and calendar without N browser requests."""
        if kind not in ("movement", "hearing"):
            raise HTTPException(status_code=400, detail="Tipo de atividade inválido.")
        try:
            start_at = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_at = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="Período de atividade inválido.") from None
        if start_at.tzinfo is None or end_at.tzinfo is None or start_at > end_at:
            raise HTTPException(status_code=400, detail="Informe um período válido com fuso horário.")
        start_at = start_at.astimezone(timezone.utc)
        end_at = end_at.astimezone(timezone.utc)
        result = []
        wanted_type = "movimentacao" if kind == "movement" else "audiencia"
        for watch in repository.list_watches(current_org()):
            if court and watch["court"].casefold() != court.casefold():
                continue
            number = watch["number"]
            routing = _routing(number, watch)
            snapshots = _timeline_court_snapshots(number, include_pje=_pje_applies(routing))
            events = consolidate(number, repository.timeline(current_org(), number),
                                 repository.communications(current_org(), number), repository,
                                 current_org(), court_snapshots=snapshots)
            for event in events:
                if event.get("tipo") != wanted_type or not event.get("occurred_at"):
                    continue
                try:
                    occurred = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
                    if occurred.tzinfo is None:
                        occurred = occurred.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if not start_at <= occurred.astimezone(timezone.utc) <= end_at:
                    continue
                result.append({"id": event["id"], "tipo": event["tipo"], "titulo": event["titulo"],
                               "occurred_at": event["occurred_at"], "fonte": event.get("fonte"),
                               "fontes": event.get("fontes", []), "number": number,
                               "formatted_number": format_number(number), "court": watch["court"]})
        result.sort(key=lambda item: (item["occurred_at"], item["id"]), reverse=True)
        record_access("portfolio_activity", None, "leitura local de atividades processuais")
        return {"items": result[:limit], "total": len(result), "truncated": len(result) > limit,
                "source": "SQLite local"}

    @app.post("/api/processes", status_code=201)
    async def register_process(payload: RegisterRequest):
        try:
            number = engine.register(
                payload.number,
                current_org(),
                payload.interval or config.interval,
                config.max_processes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        sync = schedule_sync(repository, current_org(), number, config, collection_type="cnj_add", priority=40)
        return {
            "number": number,
            "formatted_number": format_number(number),
            "court": repository.get(current_org(), number)["court"],
            "check": None,
            "synchronization": sync,
        }

    @app.post("/api/processes/check-all", status_code=202)
    async def check_all():
        results = []
        for watch in repository.list_watches(current_org()):
            number = watch["number"]
            sync = schedule_sync(repository, current_org(), number, config,
                                 collection_type="manual_refresh", priority=10, force=True)
            results.append({"number": number, "formatted_number": format_number(number),
                            "result": None, "error": None,
                            "group_id": sync.get("group_id"), "status": sync.get("status")})
        return {
            "checked": len(results),
            "failed": 0,
            "status": "accepted",
            "results": results,
        }

    @app.post("/api/processes/{number}/check")
    async def check_process(number: str):
        try:
            normalized = normalize(number)
            result = engine.check(normalized, current_org())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from None
        return {"number": normalized, "result": asdict(result)}

    @app.get("/api/processes/{number}/timeline")
    async def process_timeline(number: str, revisions: bool = False):
        try:
            normalized = normalize(number)
            rows = repository.timeline(current_org(), normalized, revisions)
            watch = repository.get(current_org(), normalized)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "não cadastrado" in str(exc) else 400, detail=str(exc)) from None
        grades = {
            meta["_scope"]: meta.get("grau", "?")
            for meta in json.loads(watch["metadata"] or "[]")
        }
        return {
            "number": normalized,
            "formatted_number": format_number(normalized),
            "court": watch["court"],
            "last_checked": watch["last_checked"],
            "movements": [
                {
                    "grade": grades.get(row["scope"], "?"),
                    "when": row["when_at"],
                    "code": row["code"],
                    "name": row["name"],
                    "complements": json.loads(row["complements"]),
                    "original": json.loads(row["raw"]),
                    "kind": row["kind"],
                    "active": bool(row["active"]),
                }
                for row in rows
            ],
        }

    @app.get("/api/processes/{number}/events")
    async def process_events(number: str, after: int = Query(default=0, ge=0)):
        try:
            normalized = normalize(number)
            rows = repository.events(current_org(), normalized, after)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "não cadastrado" in str(exc) else 400, detail=str(exc)) from None
        return [
            {
                "id": row["id"],
                "kind": row["kind"],
                "detected_at": row["detected_at"],
                "run_id": row["run_id"],
                "replaces": row["replaces"],
                "movement": {
                    "code": row["movement_code"],
                    "name": row["movement_name"],
                    "when": row["movement_when"],
                    "complements": json.loads(row["movement_complements"]),
                },
            }
            for row in rows
        ]

    @app.get("/api/processes/{number}/runs")
    async def process_runs(number: str):
        try:
            normalized = normalize(number)
            rows = repository.runs(current_org(), normalized)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "não cadastrado" in str(exc) else 400, detail=str(exc)) from None
        return [
            {key: value for key, value in dict(row).items() if key != "raw"}
            for row in rows
        ]

    @app.get("/api/processes/{number}/sync")
    async def process_sync_status(number: str):
        normalized, watch = registered(number)
        routing = _routing(normalized, watch)
        # This endpoint is a passive status read. Human-session expiry is handled by
        # the worker; polling must not write audit/expiry state or start collection.
        rows = repository.latest_collection_group(current_org(), normalized)
        states = {"queued": "pending", "running": "running", "completed": "completed",
                  "partial": "partial", "failed": "failed", "awaiting_access": "awaiting_access"}
        jobs = []
        human = repository.latest_human_verification(current_org(), normalized)
        for row in rows:
            coverage = json.loads(row["coverage"]) if row["coverage"] else None
            if row["source"] == "DJEN" and row["status"] in ("queued", "running"):
                checks = [item for item in repository.source_coverage(current_org(), normalized, "DJEN")
                          if item["checked_at"] >= row["created_at"]]
                if checks:
                    coverage = {"windows_completed": sum(item["status"] == "período consultado" for item in checks),
                                "records_seen": sum(item["count"] or 0 for item in checks),
                                "status": "in_progress"}
            public_status = states.get(row["status"], row["status"])
            verification_for_job = (human if human and human["job_id"] == row["id"] else None)
            if verification_for_job and verification_for_job["status"] in ("queued", "aguardando_verificacao_humana"):
                public_status = "aguardando_verificacao_humana"
            is_court_job = row["source"].startswith("TRF1-PJE") or row["source"] == TRF4_EPROC_SOURCE
            status_source = ("PJe/TRF1" if row["source"].startswith("TRF1-PJE") else
                             TRF4_EPROC_LABEL if row["source"] == TRF4_EPROC_SOURCE else row["source"])
            snapshot_for_job = repository.latest_court_snapshot(current_org(), normalized, status_source) if is_court_job else None
            latest_good_check = next((check for check in reversed(repository.source_coverage(current_org(), normalized, status_source))
                                      if check["status"] in ("consulta concluída", "consulta parcial")), None) if snapshot_for_job else None
            current_result = json.loads(row["result"]) if row["result"] else None
            pje_check = repository.latest_source_check(current_org(), normalized, status_source) if is_court_job else None
            pje_data = (snapshot_for_job or {}).get("data", {})
            available = {"movements": len(pje_data.get("movements", [])),
                         "participants": len(pje_data.get("participants", [])),
                         "documents": len(pje_data.get("documents", [])),
                         "hearings": len(pje_data.get("hearings", []))} if snapshot_for_job else None
            jobs.append({"id": row["id"], "source": row["source"],
                         "display_source": status_source,
                         "collection_type": row["collection_type"],
                         "status": public_status,
                         "attempts": row["attempts"], "max_attempts": row["max_attempts"],
                         "scheduled_at": row["scheduled_at"], "started_at": row["started_at"],
                         "completed_at": row["completed_at"],
                         "error": json.loads(row["error"]) if row["error"] else None,
                         "coverage": coverage,
                         "result": current_result,
                         "records_seen": (current_result or {}).get("records_seen",
                             ((snapshot_for_job or {}).get("data", {}).get("coverage", {}).get("movement_count", 0)
                              + (snapshot_for_job or {}).get("data", {}).get("coverage", {}).get("participants", 0))),
                         "counts": ({key: current_result[key] for key in
                                     ("movements_seen", "participants_seen", "documents_seen") if key in current_result}
                                    if current_result else None),
                         "last_successful_at": latest_good_check["checked_at"] if latest_good_check else None,
                         "last_attempt_at": pje_check["checked_at"] if pje_check else row["started_at"],
                         "current_source_status": pje_check["status"] if pje_check else public_status,
                         "next_attempt_at": row["scheduled_at"] if row["status"] == "queued" else None,
                         "records_available": available,
                         "human_verification": ({"id": verification_for_job["id"],
                             "status": verification_for_job["status"], "expires_at": verification_for_job["expires_at"]}
                             if verification_for_job else None)})
        is_trf4 = routing.get("tribunal_id") == "TRF4"
        status_source = TRF4_EPROC_LABEL if is_trf4 else "PJe/TRF1"
        direct_check = repository.latest_source_check(current_org(), normalized, status_source) if (is_trf4 or _pje_applies(routing)) else None
        direct_snapshot = repository.latest_court_snapshot(current_org(), normalized, status_source) if (is_trf4 or _pje_applies(routing)) else None
        direct_status = _direct_court_status(routing, direct_check, direct_snapshot)
        court_job_present = any(job["source"] in ("TRF1-PJE-1G", "TRF1-PJE-JE", TRF4_EPROC_SOURCE) for job in jobs)
        if not court_job_present:
            jobs.append({"id": None, "source": direct_status["source"], "collection_type": "public_case",
                         "status": direct_status["status"], "attempts": 0, "max_attempts": 0,
                         "message": direct_status["message"],
                         "last_attempt_at": direct_check["checked_at"] if direct_check else None,
                         "last_successful_at": next((item["checked_at"] for item in reversed(
                             repository.source_coverage(current_org(), normalized, status_source))
                             if item["status"] in ("consulta concluída", "consulta parcial")), None)
                             if (_pje_applies(routing) or is_trf4) else None,
                         "next_attempt_at": None,
                         "records_available": ({"movements": len(direct_snapshot["data"].get("movements", [])),
                             "participants": len(direct_snapshot["data"].get("participants", [])),
                             "documents": len(direct_snapshot["data"].get("documents", [])),
                             "hearings": len(direct_snapshot["data"].get("hearings", []))}
                             if direct_snapshot else None),
                         "error": json.loads(direct_check["error"]) if direct_check and direct_check.get("error") else None,
                         "coverage": direct_snapshot["data"].get("coverage") if direct_snapshot else None,
                         "result": direct_snapshot["data"].get("coverage") if direct_snapshot else None,
                         "routing": routing})
        active = any(job["status"] in ("pending", "running", "aguardando_verificacao_humana") for job in jobs)
        failed = any(job["status"] in ("partial", "failed", "awaiting_access", "not_started")
                     for job in jobs if job["status"] != "not_implemented")
        status = "aguardando_verificacao_humana" if any(job["status"] == "aguardando_verificacao_humana" for job in jobs) else (
            "running" if any(job["status"] == "running" for job in jobs) else (
            "pending" if active else "partial" if failed else "completed" if rows else "not_started")
        )
        return {"number": normalized, "group_id": rows[0]["group_id"] if rows else None,
                "status": status, "jobs": jobs,
                "source_coverage": repository.source_coverage(current_org(), normalized, "DJEN"),
                "human_verification": ({"id": human["id"], "status": human["status"],
                    "expires_at": human["expires_at"], "error_code": human["error_code"]} if human else None),
                "routing": routing, "court_source": direct_status}

    def registered(number):
        try:
            normalized = normalize(number)
            return normalized, repository.get(current_org(), normalized)
        except ValueError as exc:
            raise HTTPException(status_code=404 if "não cadastrado" in str(exc) else 400, detail=str(exc)) from None

    @app.get("/api/processos/{number}")
    async def case_details(number: str):
        normalized, watch = registered(number)
        record_access("process_details", normalized, "acompanhamento processual")
        routing = _routing(normalized, watch)
        pje_applies = _pje_applies(routing)
        pje = _trf1_snapshot(normalized)
        trf4_import = _trf4_snapshot(normalized)
        trf4_assisted = _trf4_assisted_snapshot(normalized)
        is_trf4 = routing.get("tribunal_id") == "TRF4"
        pje_check = repository.latest_source_check(current_org(), normalized,
                    TRF4_EPROC_LABEL if is_trf4 else "PJe/TRF1") if (pje_applies or is_trf4 or pje) else None
        direct_status = _direct_court_status(routing, pje_check, trf4_import if is_trf4 else pje)
        pje_data = pje["data"] if pje else None
        metadata_snapshot = pje if pje_data else None
        metadata_source = "PJe/TRF1" if pje_data else None
        if is_trf4:
            trf4_metadata = [("eproc/TRF4", trf4_import),
                             (TRF4_ASSISTED_SOURCE, trf4_assisted)]
            trf4_metadata = [(label, item) for label, item in trf4_metadata if item]
            if trf4_metadata:
                metadata_source, metadata_snapshot = max(
                    trf4_metadata, key=lambda pair: str(pair[1].get("collected_at") or ""))
        public_consultation_url = public_query_url(routing)
        general = process_metadata(watch, repository.timeline(current_org(), normalized),
                    communications=repository.communications(current_org(), normalized),
                    source_data=(metadata_snapshot or {}).get("data") if metadata_snapshot else None,
                    source_label=metadata_source,
                    source_collected_at=metadata_snapshot.get("collected_at") if metadata_snapshot else None)
        return dict(general,
                    formatted_number=format_number(normalized),
                    acesso_publico_observado=bool(metadata_snapshot),
                    acesso_publico_fonte=metadata_source if metadata_snapshot else None,
                    acesso_publico_observado_em=(metadata_snapshot.get("collected_at")
                        if metadata_snapshot else None),
                    pje_publico={**{key: pje_data.get(key) for key in (
                                   "class", "subjects", "distribution_date", "jurisdiction", "court_organ",
                                   "situation", "coverage", "case_value", "case_value_evidence",
                                   "judicial_secrecy", "judicial_secrecy_original", "judicial_secrecy_evidence",
                                   "legal_aid", "legal_aid_original")
                                   if not key.startswith("judicial_secrecy") or pje_data.get("judicial_secrecy") is not None},
                                  "documents": _safe_documents(pje_data.get("documents", []))}
                                  if pje_data else None,
                    divergencias=({"classe": {"DataJud": general.get("classe"), "PJe/TRF1": pje_data.get("class")}}
                                  if pje_data and general.get("classe") and pje_data.get("class")
                                  and general.get("classe") != pje_data.get("class") else {}),
                    consulta_direta=direct_status,
                    trf4_importacao_assistida=({"status": (repository.latest_source_check(current_org(), normalized, TRF4_ASSISTED_SOURCE) or {}).get("status"),
                        "collected_at": trf4_assisted.get("collected_at"),
                        "movement_count": len(trf4_assisted["data"].get("movements", [])),
                        "pagination_status": trf4_assisted["data"].get("coverage", {}).get("pagination_status"),
                        "automatic": False} if trf4_assisted else {"status": "not_imported", "automatic": False}),
                    fontes={"DataJud": "consultado" if watch["metadata"] else "não consultado",
                            "DJEN": (repository.latest_source_check(current_org(), normalized, "DJEN") or {}).get("status", "não consultado"),
                            "Consulta direta ao tribunal": direct_status["status"],
                            "TRF4 — importação assistida": "capturado" if trf4_assisted else "não importado"},
                    public_consultation_url=public_consultation_url,
                    public_consultation_status=("official_url_confirmed" if public_consultation_url else "not_verified"))

    @app.get("/api/processos/{number}/partes")
    async def case_parties(number: str):
        normalized, watch = registered(number)
        record_access("process_parties", normalized, "acompanhamento processual")
        routing = _routing(normalized)
        participant_snapshots = []
        if tribunal(normalize(normalized)) == "trf1":
            # Older partial captures may contain parties omitted by a later
            # response. Canonical consolidation receives the full snapshot
            # history (newest first), with PJe remaining the highest authority.
            participant_snapshots.extend(repository.court_snapshots(
                current_org(), normalized, "PJe/TRF1"))
        if routing.get("tribunal_id") == "TRF4":
            for source_name in (TRF4_EPROC_LABEL, TRF4_ASSISTED_SOURCE):
                participant_snapshots.extend(repository.court_snapshots(current_org(), normalized, source_name))
        participants = canonical_participants(
            repository, current_org(), normalized,
            court_snapshots=participant_snapshots,
            metadata_records=json.loads(watch.get("metadata") or "[]"))
        participants = presentation_participants(participants)
        parties = [item for item in participants if "parte" in set(item["kinds"]) and not professional_kind(item)]
        # PJe is the display authority when it has a participant with the same
        # normalized name. A DJEN/DataJud name-only match remains a separate
        # candidate in `participants`, while the primary party list avoids
        # showing two apparent copies of the same person.
        pje_party_names = {item.get("normalized_name") for item in parties
                           if "PJe/TRF1" in (item.get("sources") or [item.get("source")])}
        display_parties = []
        for item in parties:
            is_pje = "PJe/TRF1" in (item.get("sources") or [item.get("source")])
            if not is_pje and item.get("normalized_name") in pje_party_names:
                continue
            display_parties.append(item)
        lawyers = [item for item in participants if professional_kind(item)]
        recipients = [item for item in participants if "destinatário" in item["kinds"]
                      and "advogado" not in item["kinds"]]
        return {"status": "dados de fontes públicas" if participants else "indisponível",
                "source": "DataJud/DJEN/PJe/TRF4 conforme proveniência", "participants": participants,
                "partes": [{"nome": item["original_name"], "polo": item.get("polo"),
                            "funcao_processual": item.get("procedural_function"),
                            "funcao_original": item.get("role_original"),
                            "identidade_status": item.get("identity_status"),
                            "polo_code_original": item.get("polo_code_original"),
                            "polo_normalized": item.get("polo_normalized"),
                            "polo_mapping_status": item.get("polo_mapping_status"),
                            "polo_mapping_reference": item.get("polo_mapping_reference"),
                            "communication_role": item.get("communication_role"),
                            "cpf": item.get("cpf"), "cnpj": item.get("cnpj"),
                            "document_type": item.get("document_type"),
                            "document_number": item.get("document_number"),
                            "polo_canonico": item.get("procedural_pole") or item.get("polo_normalized"),
                            "escopo_funcao": item.get("function_scope"),
                            "fontes": item.get("sources") or [item.get("source")],
                            "correspondencia_apresentacao": item.get("presentation_match"),
                            "identidades_origem": item.get("presentation_members") or [],
                            "correspondencias_candidatas": [
                                {"participant_id": candidate.get("participant_id"),
                                 "fonte": candidate.get("source"),
                                 "papel": candidate.get("procedural_function") or candidate.get("role_original"),
                                 "evidence_ids": candidate.get("evidence_ids") or []}
                                for candidate in parties if candidate is not item
                                and candidate.get("normalized_name") == item.get("normalized_name")
                                and "PJe/TRF1" not in (candidate.get("sources") or [candidate.get("source")])
                            ],
                            "papel": item.get("procedural_function") or
                                     ("Polo ativo" if item.get("procedural_pole") == "ativo" else
                                      "Polo passivo" if item.get("procedural_pole") == "passivo" else
                                      "Terceiro" if item.get("procedural_pole") == "terceiro" else
                                     item.get("communication_role") if "destinatário" in item["kinds"]
                                      and "parte" not in item["kinds"] else "Parte identificada"),
                            "fonte": item["source"], "participant_id": item["participant_id"],
                            "evidence_ids": item["evidence_ids"]} for item in display_parties],
                "advogados": lawyers, "destinatarios": recipients,
                "message": "Partes, destinatários e demais participantes são retornados separadamente, com proveniência.",
                "coverage": (repository.latest_source_check(current_org(), normalized, "PJe/TRF1")
                             if tribunal(normalize(normalized)) == "trf1" else None)}

    @app.get("/api/processos/{number}/movimentacoes")
    async def case_movements(number: str):
        normalized, watch = registered(number)
        record_access("process_movements", normalized, "acompanhamento processual")
        metadata = json.loads(watch["metadata"] or "[]")
        organs = {meta.get("_scope"): meta.get("orgaoJulgador") for meta in metadata if meta.get("_scope")}
        rows = repository.timeline(current_org(), normalized)
        routing = _routing(normalized, watch)
        snapshots = _timeline_court_snapshots(normalized, include_pje=_pje_applies(routing))
        consolidated = consolidate(normalized, rows, repository.communications(current_org(), normalized), repository,
                                   current_org(), court_snapshots=snapshots)
        movements = [{
            "id": row["id"], "when": row["occurred_at"], "code": row.get("code"),
            "name": row.get("original_title") or row["titulo"], "title": row["titulo"],
            "original_name": row.get("original_title"), "source": row["fonte"],
            "sources": row.get("fontes") or [row["fonte"]],
            "preferred_source": row.get("preferred_source"),
            "orgao": row.get("orgao") or organs.get(row.get("scope")),
            "complements": row.get("complements", []), "complement_display": row.get("complement_display", []),
            "original": row.get("original"), "tpu": row.get("tpu"),
            "source_details": row.get("source_details", []), "documents": row.get("documents", []),
            "equivalence": row.get("equivalence"),
        } for row in consolidated if row["tipo"] == "movimentacao"]
        trf4_import = _trf4_snapshot(normalized)
        return {"status": "ok" if watch["metadata"] or trf4_import else
                ((repository.latest_source_check(current_org(), normalized, "PJe/TRF1") or {}).get("status", "não consultado")
                 if _pje_applies(routing) else "Consulta direta ao tribunal ainda não implementada"),
                "items": movements}

    @app.get("/api/processos/{number}/intimacoes")
    async def case_communications(number: str):
        normalized, _ = registered(number)
        record_access("process_communications", normalized, "acompanhamento processual")
        check = repository.latest_source_check(current_org(), normalized, "DJEN")
        items = repository.communications(current_org(), normalized)
        return {"status": check["status"] if check else "publicações da descoberta OAB" if items else "não consultado",
                "checked_at": check["checked_at"] if check else None,
                "period_start": check["period_start"] if check else None,
                "period_end": check["period_end"] if check else None,
                "error": check["error"] if check else None,
                "coverage": repository.source_coverage(current_org(), normalized, "DJEN"),
                "discovery_evidence": bool(items and not check), "items": items}

    @app.get("/api/processos/{number}/timeline")
    async def consolidated_timeline(number: str, unread_only: bool = False):
        normalized, _ = registered(number)
        record_access("process_timeline", normalized, "acompanhamento processual")
        routing = _routing(normalized)
        court_snapshots = _timeline_court_snapshots(normalized, include_pje=_pje_applies(routing))
        items = consolidate(normalized, repository.timeline(current_org(), normalized),
                            repository.communications(current_org(), normalized), repository, current_org(),
                            court_snapshots=court_snapshots)
        movements = [item for item in items if item.get("tipo") == "movimentacao"]
        decorated = {item["id"]: item for item in repository.decorate_read_state(current_org(), normalized, movements)}
        items = [decorated.get(item["id"], item) if item.get("tipo") == "movimentacao" else item
                 for item in items]
        if unread_only:
            items = [item for item in items if item.get("tipo") == "movimentacao" and item.get("is_unread")]
        summary = repository.unread_summary(current_org(), normalized)
        return {"status": "ok", "items": items, "unread_count": summary["unread_count"],
                "relations": repository.event_relations(current_org(), normalized)}

    @app.get("/api/processos/{number}/eventos-nao-lidos")
    async def unread_events(number: str):
        normalized, _ = registered(number)
        record_access("process_unread_events", normalized, "acompanhamento processual")
        events = [item for item in repository.current_movement_events(current_org(), normalized) if item["is_unread"]]
        return {"number": normalized, "count": len(events), "items": events}

    @app.post("/api/processos/{number}/eventos-nao-lidos/ler-todos")
    async def mark_all_events_read(number: str):
        normalized, _ = registered(number)
        changed = repository.mark_process_unread_read(current_org(), normalized)
        record_access("mark_process_events_read", normalized, "acompanhamento processual")
        return {"number": normalized, "marked_read": changed,
                **repository.unread_summary(current_org(), normalized)}

    @app.post("/api/processos/{number}/eventos/{event_id}/ler")
    async def mark_event_read(number: str, event_id: str):
        normalized, _ = registered(number)
        current = next((item for item in repository.current_movement_events(current_org(), normalized)
                        if item["id"] == event_id), None)
        if current is None or not current.get("logical_event_id"):
            raise HTTPException(status_code=404, detail="Movimentação não encontrada neste processo.")
        changed = repository.mark_logical_event_read(current_org(), normalized, current["logical_event_id"])
        record_access("mark_process_event_read", normalized, "acompanhamento processual")
        updated = next((item for item in repository.current_movement_events(current_org(), normalized)
                        if item["id"] == event_id), current)
        return {"number": normalized, "event": updated, "changed": changed,
                **repository.unread_summary(current_org(), normalized)}

    @app.post("/api/processos/{number}/atualizar", status_code=202)
    async def refresh_case(number: str, payload: RefreshRequest):
        normalized, watch = registered(number)
        end = payload.fim or date.today()
        start = payload.inicio
        if start is None and end < config.djen_initial_date:
            raise HTTPException(status_code=400, detail="Data final anterior ao início histórico configurado.")
        if start and start > end:
            raise HTTPException(status_code=400, detail="Data inicial posterior à final.")
        group = schedule_sync(repository, current_org(), normalized, config, collection_type="manual_refresh",
                              priority=10, force=True, start=start, end=end)
        routing = group.get("routing") or _routing(normalized, watch)
        court_state = _direct_court_status(routing)
        if any(item["source"] in ("TRF1-PJE-1G", "TRF1-PJE-JE", TRF4_EPROC_SOURCE) for item in group.get("jobs", [])):
            court_state = {**court_state, "status": "pending", "message": None}
        return {"status": "accepted", "number": normalized,
                "group_id": group.get("group_id"), "synchronization": group,
                "sources": {"DataJud": {"status": "pending"}, "DJEN": {"status": "pending"},
                            "Consulta direta ao tribunal": court_state}}

    @app.post("/api/processos/{number}/pje/retomar", status_code=202)
    async def resume_pje_with_human_verification(number: str):
        normalized, _ = registered(number)
        rows = repository.latest_collection_group(current_org(), normalized)
        resumable_codes = {"captcha_identified", "human_verification_expired",
                           "human_session_expired", "human_verification_cancelled"}
        job = next((row for row in rows if row["source"] in ("TRF1-PJE-1G", "TRF1-PJE-JE")
                    and row["status"] == "awaiting_access"
                    and json.loads(row["error"] or "{}").get("code") in resumable_codes), None)
        if not job:
            raise HTTPException(status_code=409, detail="Não há coleta PJe aguardando retomada assistida.")
        verification_id = repository.create_human_verification(current_org(), normalized, job["id"], ttl_seconds=180)
        record_access("pje_human_verification_resume", normalized,
                                 "retomada local assistida de consulta pública")
        return {"status": "accepted", "number": normalized, "job_id": job["id"],
                "verification_id": verification_id, "expires_in_seconds": 180,
                "message": "A retomada abrirá uma janela local do navegador se o worker estiver ativo."}

    @app.post("/api/processos/{number}/pje/retomada/{verification_id}/cancelar")
    async def cancel_pje_human_verification(number: str, verification_id: str):
        normalized, _ = registered(number)
        verification = repository.human_verification(verification_id)
        if not verification or verification["org"] != current_org() or verification["number"] != normalized:
            raise HTTPException(status_code=404, detail="Retomada não encontrada.")
        repository.cancel_human_verification(verification_id)
        record_access("pje_human_verification_cancel", normalized,
                                 "cancelamento da retomada assistida")
        return {"status": "cancelled"}

    @app.delete("/api/processos/{number}")
    async def delete_case(number: str):
        normalized, _ = registered(number)
        record_access("delete_process", normalized, "exclusão solicitada pelo usuário")
        repository.delete_watch(current_org(), normalized)
        return {"status": "deleted", "number": normalized}

    @app.get("/docs", include_in_schema=False)
    async def swagger_docs():
        return HTMLResponse("""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="/assets/logo.png"><title>ProcessWatch API — Swagger UI</title>
<link rel="stylesheet" href="/assets/docs/swagger-ui/swagger-ui.css">
<link rel="stylesheet" href="/assets/docs/docs.css"></head><body><div id="swagger-ui"></div>
<script src="/assets/docs/swagger-ui/swagger-ui-bundle.js"></script>
<script src="/assets/docs/swagger-ui/swagger-ui-standalone-preset.js"></script>
<script src="/assets/docs/swagger-ui/swagger-initialize.js"></script>
</body></html>""")

    @app.get("/redoc", include_in_schema=False)
    async def redoc_docs():
        return HTMLResponse("""<!doctype html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><link rel="icon" href="/assets/logo.png"><title>ProcessWatch API — ReDoc</title>
<link rel="stylesheet" href="/assets/docs/docs.css"></head><body><redoc spec-url="/openapi.json"></redoc>
<script src="/assets/docs/redoc/redoc.standalone.js"></script></body></html>""")

    @app.get("/assets/docs/{asset_path:path}", include_in_schema=False)
    async def documentation_asset(asset_path: str):
        allowed_assets = {
            "swagger-ui/swagger-ui.css": ("swagger-ui", "swagger-ui.css", "text/css"),
            "swagger-ui/swagger-ui-bundle.js": ("swagger-ui", "swagger-ui-bundle.js", "application/javascript"),
            "swagger-ui/swagger-ui-standalone-preset.js": ("swagger-ui", "swagger-ui-standalone-preset.js", "application/javascript"),
            "redoc/redoc.standalone.js": ("redoc", "redoc.standalone.js", "application/javascript"),
            "docs.css": (".", "docs.css", "text/css"),
            "swagger-ui/swagger-initialize.js": ("swagger-ui", "swagger-initialize.js", "application/javascript"),
            "redoc/logo-mini.svg": ("redoc", "logo-mini.svg", "image/svg+xml"),
        }
        entry = allowed_assets.get(asset_path)
        if not entry:
            raise HTTPException(status_code=404, detail="Asset de documentação não encontrado.")
        folder, filename, media_type = entry
        content = (Path(__file__).parent / "web" / "vendor" / "docs" / folder / filename).read_bytes()
        return Response(content=content, media_type=media_type)

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schemes = schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schemes["ServiceBearer"] = {"type": "http", "scheme": "bearer",
                                     "bearerFormat": "ProcessWatch service token",
                                     "description": "Use a pwsvc_ credential with the permissions required by the operation. In token mode, the configured legacy bearer remains accepted by the existing API contract."}
        schemes["LegacyAdminBearer"] = {"type": "http", "scheme": "bearer",
                                         "bearerFormat": "Legacy ProcessWatch administrator token",
                                         "description": "Used only for administrator credential management in token mode."}
        schemes["ProcessWatchSession"] = {"type": "apiKey", "in": "cookie",
                                          "name": "processwatch_session"}
        schemes["PlatformAdminSession"] = {"type": "apiKey", "in": "cookie",
                                           "name": "processwatch_platform_session",
                                           "description": "Sessão independente de administrador global; obtida em /api/platform/auth/login."}
        for path, operations in schema.get("paths", {}).items():
            if not path.startswith("/api/"):
                continue
            for method, operation in operations.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete", "head", "options"}:
                    continue
                public_in_accounts = config.auth_mode == "accounts" and path in {
                    "/api/auth/login", "/api/auth/recovery/request", "/api/auth/recovery/complete"}
                always_public = path in {"/api/health", "/api/auth/mode"}
                if always_public or public_in_accounts:
                    operation["security"] = []
                    continue
                if path == "/api/platform/auth/login":
                    operation["security"] = []
                    continue
                if path.startswith("/api/platform/"):
                    operation["security"] = [{"PlatformAdminSession": []}]
                    continue
                if path.startswith("/api/service-credentials") or path == "/api/office":
                    operation["security"] = ([{"ProcessWatchSession": []}] if config.auth_mode == "accounts"
                                              else [{"LegacyAdminBearer": []}])
                    if path != "/api/office" or method.lower() == "put":
                        operation["x-processwatch-administrator-required"] = True
                    continue
                if path.startswith("/api/admin/") and config.auth_mode == "accounts":
                    operation["security"] = [{"ProcessWatchSession": []}]
                    operation["x-processwatch-administrator-required"] = True
                    continue
                required = service_permissions_for(path, method)
                if config.auth_mode == "accounts":
                    accepted = []
                    if required:
                        accepted.append({"ServiceBearer": []})
                    accepted.append({"ProcessWatchSession": []})
                else:
                    accepted = [{"LegacyAdminBearer": []}]
                    if required:
                        accepted.insert(0, {"ServiceBearer": []})
                operation["security"] = accepted
                if required:
                    operation["x-processwatch-required-permissions"] = sorted(required)
                if path.startswith("/api/service-credentials") or path.startswith("/api/admin/"):
                    operation["x-processwatch-administrator-required"] = True
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi

    return app

