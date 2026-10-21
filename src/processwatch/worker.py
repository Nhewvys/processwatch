"""Worker síncrono: uma consulta por vez, falhas isoladas por processo."""
import time
import math
import asyncio
import json
from datetime import date, datetime, timezone
from threading import Event, Thread
import uuid
from .environment_policy import require_operational_office


def _worker_orgs(repository, org, config):
    environment = getattr(config, "environment", getattr(repository, "environment", "development"))
    if environment != "production":
        return [org]
    if org is None:
        return [row["id"] for row in repository.list_offices()
                if row.get("active") and row["id"] != "local"]
    require_operational_office(environment, org)
    return [org]


def run_due(engine, repository, org="local"):
    require_operational_office(getattr(repository, "environment", "development"), org)
    results = []
    for watch in repository.list_watches(org, due_only=True):
        try:
            result = engine.check(watch["number"], org)
            results.append((watch["number"], result, None))
        except RuntimeError as exc:
            results.append((watch["number"], None, str(exc)))
    return results


def run_djen_due(repository, client, config, org="local"):
    require_operational_office(config.environment, org)
    from .monitoring import sync_djen
    reports = []
    for watch in repository.list_watches(org):
        check = repository.latest_source_check(org, watch["number"], "DJEN")
        if check:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(check["checked_at"])).total_seconds()
            if elapsed < config.djen_sync_interval:
                continue
        try:
            report = asyncio.run(sync_djen(repository, client, org, watch["number"],
                                          initial=config.djen_initial_date,
                                          window_days=config.djen_window_days,
                                          overlap_days=config.djen_overlap_days))
            reports.append(report)
        except Exception:
            repository.save_communications(org, watch["number"], [], status="erro",
                                           error="Falha interna na sincronização DJEN.")
    return reports


def run_collection_cycle(engine, repository, djen_client, config, org="local", worker_id=None,
                         pje_client=None):
    """Schedule due sources and drain eligible persistent jobs serially."""
    from .jobs import SQLiteCollectionJobRunner
    from .orchestrator import schedule_due

    if org is None and config.environment == "production":
        raise ValueError("O worker de produção deve executar um escritório por ciclo.")
    if org is not None:
        require_operational_office(config.environment, org)

    worker_id = worker_id or "worker-" + uuid.uuid4().hex
    schedule_due(repository, config, org)
    if pje_client is None:
        from .providers.pje import default_pje_provider
        pje_client = default_pje_provider()
    runner = SQLiteCollectionJobRunner(engine, repository, djen_client, config, pje_client=pje_client)
    completed = []
    def dispatch_human_job(job):
        databases = repository.db.execute("PRAGMA database_list").fetchall()
        db_path = next((row[2] for row in databases if row[1] == "main"), "")
        if not db_path or db_path == ":memory:":
            # In-memory repositories are test-only; run inline to preserve their semantics.
            completed.append((job["id"], runner.run(job)))
            return
        def execute_isolated():
            from .repository import SQLiteRepository
            from .engine import Engine
            isolated = SQLiteRepository(db_path, environment=config.environment,
                                         primary_path=config.primary_db or None)
            try:
                isolated_runner = SQLiteCollectionJobRunner(
                    Engine(isolated, engine.provider), isolated, djen_client, config,
                    pje_client=None)
                isolated_runner.run(job)
            finally:
                isolated.close()
        Thread(target=execute_isolated, name=f"processwatch-human-{job['id']}", daemon=True).start()
        completed.append((job["id"], "human_assist_dispatched"))
    while True:
        job = repository.claim_collection_job(org, worker_id,
                                             lease_seconds=config.collection_lease_seconds)
        if not job:
            break
        if job["source"] == "DataJud":
            interval = config.min_interval
        elif job["source"] in ("TRF1-PJE-1G", "TRF1-PJE-JE", "TRF4-EPROC-DIRECT"):
            interval = config.court_min_interval
        else:
            interval = config.djen_min_interval
        wait = repository.source_rate_wait(job["source"], interval)
        while wait > 0:
            time.sleep(wait)
            wait = repository.source_rate_wait(job["source"], interval)
        if (job["source"] in ("TRF1-PJE-1G", "TRF1-PJE-JE") and
                json.loads(job.get("params") or "{}").get("human_assist")):
            dispatch_human_job(job)
        else:
            completed.append((job["id"], runner.run(job)))
    # OAB subscriptions share the existing worker thread and scheduler tick.
    # Each complete DJEN result commits atomically before its cursor advances.
    while True:
        job = repository.claim_oab_monitor_job(org)
        if not job:
            break
        wait = repository.source_rate_wait("DJEN", config.djen_min_interval)
        while wait > 0:
            time.sleep(wait)
            wait = repository.source_rate_wait("DJEN", config.djen_min_interval)
        monitor = job["monitor"]
        begin = date.fromisoformat(job["window_start"])
        finish = date.fromisoformat(job["window_end"])
        try:
            fetched = asyncio.run(djen_client.fetch_by_oab(
                monitor["oab"], monitor["uf"], begin, finish))
            if (fetched.period_start != begin.isoformat() or
                    fetched.period_end != finish.isoformat() or fetched.pages < 1):
                raise RuntimeError("DJEN não confirmou a cobertura completa da janela OAB.")
            baseline = not bool(monitor["baseline_completed"])
            discovery_kind = "historical_baseline" if baseline else "new_publication"
            run_id = repository.save_oab_discovery(
                job["org"], monitor["oab"], monitor["uf"], fetched,
                monitor_id=monitor["id"], discovery_kind=discovery_kind)
            result = {"run_id": run_id, "source": "DJEN", "oab": monitor["oab"],
                      "uf": monitor["uf"], "period_start": fetched.period_start,
                      "period_end": fetched.period_end, "pages": fetched.pages,
                      "reported": fetched.reported, "matched": len(fetched.items),
                      "baseline": baseline}
            repository.finish_oab_monitor_job(job, success=True, result=result)
            completed.append((job["id"], "completed", result))
        except Exception as exc:
            repository.record_oab_discovery_failure(job["org"], monitor["oab"], monitor["uf"],
                job["window_start"], job["window_end"], exc)
            retry_seconds = min(3600, 900 * (2 ** max(int(job["attempts"])-1, 0)))
            repository.finish_oab_monitor_job(job, success=False, error=exc,
                                               retry_seconds=retry_seconds)
            completed.append((job["id"], "failed", str(exc)))
    return completed


def watch_forever(engine, repository, org="local", poll=5, stop=None, djen_client=None, config=None,
                  queue_mode=False, pje_client=None):
    if not math.isfinite(poll) or poll <= 0:
        raise ValueError("poll deve ser positivo.")
    if config and config.environment == "production" and org == "local":
        raise ValueError("O worker de produção não pode usar o escritório reservado 'local'.")
    while stop is None or not stop.is_set():
        organizations = _worker_orgs(repository, org, config) if config else [org]
        for worker_org in organizations:
            if queue_mode and djen_client is not None and config is not None:
                run_collection_cycle(engine, repository, djen_client, config, worker_org,
                                     pje_client=pje_client)
            else:
                run_due(engine, repository, worker_org)
                if djen_client is not None and config is not None:
                    run_djen_due(repository, djen_client, config, worker_org)
        if stop is not None:
            stop.wait(poll)
        else:
            time.sleep(poll)


def start_background_worker(db_path, config, org="local", poll=1):
    """Consume the durable collection queue in a separate SQLite connection."""
    if db_path == ":memory:":
        raise ValueError("A consulta automática da interface precisa de um arquivo SQLite persistente.")
    from .engine import Engine
    from .providers.datajud import DataJud
    from .repository import SQLiteRepository
    from .database_paths import validate_database_path

    if org is not None:
        require_operational_office(config.environment, org)

    db_path = validate_database_path(db_path, environment=config.environment,
                                     primary_path=config.primary_db or None)

    stop = Event()

    def run():
        repository = SQLiteRepository(db_path, environment=config.environment,
                                      primary_path=config.primary_db or None)
        try:
            from .providers.djen import DjenClient
            client = DjenClient(min_interval=config.djen_min_interval)
            watch_forever(Engine(repository, DataJud(config)), repository, org, poll, stop,
                          djen_client=client, config=config, queue_mode=True)
        finally:
            repository.close()

    thread = Thread(target=run, name="processwatch-collection-worker", daemon=True)
    thread.start()
    return stop, thread
