import argparse
import json
import logging
import sys
import asyncio
from pathlib import Path
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from datetime import datetime, timezone
from .cnj import normalize, format_number
from .config import Config
from .engine import Engine
from .repository import SQLiteRepository
from .providers.datajud import DataJud
from .worker import run_due, watch_forever
from .monitoring import import_numbers, sync_djen
from .source_matrix import load_document
from . import api_cli
from .environment_policy import require_operational_office


class JSONLogs(logging.Formatter):
    def format(self, record):
        data = {"time": datetime.now(timezone.utc).isoformat(), "level": record.levelname, "event": getattr(record, "event", record.getMessage())}
        for key in ("org", "run_id", "error", "result"):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        return json.dumps(data, ensure_ascii=False)


def show_result(result):
    print(f"Histórico importado: {result.imported} | Novas: {result.new} | Correções prováveis: {result.corrected} | Revisão: {result.review}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="ProcessWatch: monitoramento processual e administração da API")
    parser.add_argument("--db", help="Arquivo SQLite (ou PROCESSWATCH_DB)")
    parser.add_argument("--org", default="local", help="Namespace local; não substitui autenticação")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="Cadastrar e agendar sincronização inicial")
    add.add_argument("number")
    add.add_argument("--interval", type=int, help="Intervalo em segundos")
    add.add_argument("--no-fetch", action="store_true", help="Somente cadastrar, sem enfileirar coleta")
    batch = sub.add_parser("import-list", help="Importar lista de CNJ para uma carteira")
    batch.add_argument("file", help="Arquivo UTF-8 com um CNJ por linha")
    batch.add_argument("--portfolio", required=True)
    batch.add_argument("--interval", type=int)
    batch.add_argument("--fetch", action="store_true", help="Priorizar a coleta inicial na fila sem bloquear a importação")
    djen = sub.add_parser("sync-djen", help="Consultar janelas históricas do DJEN")
    djen.add_argument("number")
    djen.add_argument("--start", type=date.fromisoformat)
    djen.add_argument("--end", type=date.fromisoformat)
    discover = sub.add_parser("discover-oab", help="Descobrir processos em publicações DJEN por OAB/UF")
    discover.add_argument("oab")
    discover.add_argument("uf")
    discover.add_argument("--start", required=True, type=date.fromisoformat)
    discover.add_argument("--end", required=True, type=date.fromisoformat)
    reprocess = sub.add_parser("reprocess-djen", help="Reprocessar comunicações brutas sem consultar a rede")
    reprocess.add_argument("number")
    tpu_update = sub.add_parser("tpu-update", help="Importar códigos oficiais do SGT/CNJ")
    tpu_update.add_argument("--codes", nargs="+", default=["246", "51", "1051", "581", "85", "982", "12750"])
    tpu_update.add_argument("--export", help="Gravar exportação verificável para importação offline")
    tpu_import = sub.add_parser("tpu-import", help="Importar exportação SGT com XML de origem")
    tpu_import.add_argument("file")
    tpu_bulk = sub.add_parser("tpu-import-sheet", help="Importar planilha oficial SGT de movimentos")
    tpu_bulk.add_argument("file")
    tpu_bulk.add_argument("--version", default="SGT-2026-10-06", help="Versão oficial SGT-AAAA-MM-DD")
    tpu_reprocess = sub.add_parser("tpu-reprocess", help="Reprocessar movimentos já armazenados")
    tpu_reprocess.add_argument("number")
    tpu_show = sub.add_parser("tpu-show", help="Consultar código no catálogo local")
    tpu_show.add_argument("code")
    sub.add_parser("sources", help="Consultar matriz de capacidades de fontes judiciais")
    reconcile = sub.add_parser("reconcile", help="Sugerir vínculos explícitos entre fontes sem fundir eventos")
    reconcile.add_argument("number")
    review = sub.add_parser("review-link", help="Confirmar ou rejeitar um vínculo sugerido")
    review.add_argument("number")
    review.add_argument("left_id")
    review.add_argument("right_id")
    review.add_argument("status", choices=("confirmed", "rejected"))
    for command in ("check", "timeline", "runs", "events"):
        p = sub.add_parser(command)
        p.add_argument("number")
        if command == "timeline":
            p.add_argument("--revisions", action="store_true")
            p.add_argument("--timezone", default="America/Sao_Paulo")
        if command == "events":
            p.add_argument("--after", type=int, default=0, help="Cursor do último ID consumido")
    sub.add_parser("list")
    sub.add_parser("check-all", help="Consultar todos os processos cadastrados")
    sub.add_parser("run-due", help="Consultar processos vencidos uma vez")
    worker = sub.add_parser("watch", help="Executar worker até Ctrl+C")
    worker.add_argument("--poll", type=float, default=5)
    worker.add_argument("--djen", action="store_true", help="Sincronizar DJEN também, com intervalo próprio")
    web = sub.add_parser("web", help="Iniciar API e interface local")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    api_cli.register(sub)
    args = parser.parse_args(argv)
    if args.command in api_cli.COMMANDS:
        try:
            return api_cli.run(args)
        except api_cli.CLIError as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            return 1
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JSONLogs())
    logger = logging.getLogger("processwatch")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.INFO)
    repo = None
    try:
        config = Config.from_env()
        db_path = args.db or config.db
        if args.command == "web":
            if config.production and args.org == "local":
                # Authenticated requests select a tenant from their account; no
                # default legacy tenant is exposed by the production app.
                app_org = None
            else:
                app_org = args.org
        else:
            require_operational_office(config.environment, args.org)
            app_org = args.org
        repo = SQLiteRepository(db_path, environment=config.environment,
                                primary_path=config.primary_db or None)
        engine = Engine(repo, DataJud(config))
        org = args.org
        if args.command == "add":
            number = engine.register(args.number, org, args.interval if args.interval is not None else config.interval, config.max_processes)
            if not args.no_fetch:
                from .orchestrator import schedule_sync
                queued = schedule_sync(repo, org, number, config, collection_type="cnj_add", priority=40)
                print(f"Cadastrado: {format_number(number)} ({repo.get(org, number)['court'].upper()}); "
                      f"sincronização {queued['status']} ({queued.get('group_id') or 'dados recentes'})")
            else:
                print(f"Cadastrado sem coleta: {format_number(number)} ({repo.get(org, number)['court'].upper()})")
        elif args.command == "import-list":
            values = [line.strip() for line in Path(args.file).read_text(encoding="utf-8").splitlines()
                      if line.strip() and not line.lstrip().startswith("#")]
            results = import_numbers(engine, repo, org, values, portfolio=args.portfolio,
                                     interval=args.interval or config.interval,
                                     limit=config.max_processes, fetch=args.fetch, config=config)
            for item in results:
                print(json.dumps(item, ensure_ascii=False))
            if any(item["status"] == "erro" for item in results):
                return 1
        elif args.command == "sync-djen":
            from .providers.djen import DjenClient
            report = asyncio.run(sync_djen(repo, DjenClient(), org, args.number,
                                           initial=config.djen_initial_date, start=args.start, end=args.end,
                                           window_days=config.djen_window_days,
                                           overlap_days=config.djen_overlap_days))
            print(json.dumps(report.__dict__, ensure_ascii=False))
            if report.status == "parcial":
                return 1
        elif args.command == "discover-oab":
            from .discovery import discover_oab
            from .providers.djen import DjenClient
            report = asyncio.run(discover_oab(repo, DjenClient(), org, args.oab,
                                             args.uf, args.start, args.end))
            print(json.dumps(report, ensure_ascii=False))
        elif args.command == "reprocess-djen":
            number = normalize(args.number)
            print(f"Comunicações reprocessadas: {repo.reprocess_djen(org, number)}")
        elif args.command == "tpu-update":
            from .sgt_catalog import SgtCatalogService, export_catalog
            version, items = SgtCatalogService().import_codes(repo, args.codes)
            if args.export:
                export_catalog(args.export, version, items)
            print(json.dumps({"version": version, "items": len(items)}, ensure_ascii=False))
        elif args.command == "tpu-import":
            from .sgt_catalog import import_export
            version, count = import_export(repo, args.file)
            print(json.dumps({"version": version, "items": count}, ensure_ascii=False))
        elif args.command == "tpu-import-sheet":
            from .sgt_catalog import import_official_spreadsheet
            version, total, added, digest = import_official_spreadsheet(repo, args.file, version=args.version)
            print(json.dumps({"version": version, "codes_in_sheet": total,
                              "new_codes": added, "sha256": digest}, ensure_ascii=False))
        elif args.command == "tpu-reprocess":
            number = normalize(args.number)
            print(json.dumps({"number": number, "enriched": repo.reprocess_tpu(org, number)}, ensure_ascii=False))
        elif args.command == "tpu-show":
            item = repo.tpu_item(args.code)
            if not item:
                raise ValueError("Código não localizado na versão local do catálogo.")
            print(json.dumps({**{key: value for key, value in item.items() if key != "raw"},
                              "hierarchy": repo.tpu_hierarchy(args.code)}, ensure_ascii=False))
        elif args.command == "sources":
            print(json.dumps(load_document(), ensure_ascii=False, indent=2))
        elif args.command == "reconcile":
            from .reconciliation import propose_explicit_links
            number = normalize(args.number)
            count = propose_explicit_links(repo, org, number)
            print(json.dumps({"number": number, "candidates": count,
                              "relations": repo.event_relations(org, number)}, ensure_ascii=False))
        elif args.command == "review-link":
            number = normalize(args.number)
            repo.review_relation(org, number, args.left_id, args.right_id, args.status, "operador-local")
            print("Relação revisada; os eventos originais permanecem separados.")
        elif args.command == "check":
            show_result(engine.check(args.number, org))
        elif args.command == "timeline":
            number = normalize(args.number)
            watch = repo.get(org, number)
            print(f"{format_number(number)} | {watch['court'].upper()} | Última consulta: {watch['last_checked'] or 'nenhuma'}")
            metadata = json.loads(watch["metadata"] or "[]")
            grades = {meta["_scope"]: meta["grau"] for meta in metadata}
            try:
                display_zone = ZoneInfo(args.timezone)
            except ZoneInfoNotFoundError:
                if args.timezone not in ("America/Sao_Paulo", "UTC"):
                    raise ValueError("Fuso desconhecido ou indisponível nesta instalação.") from None
                display_zone = timezone.utc
                print("Base de fusos indisponível; timeline exibida em UTC. Instale tzdata para conversão local.", file=sys.stderr)
            for row in repo.timeline(org, number, args.revisions):
                dt = datetime.fromisoformat(row["when_at"])
                display_date = dt.astimezone(display_zone).strftime("%d/%m/%Y %H:%M:%S %Z") if dt.tzinfo else dt.strftime("%d/%m/%Y %H:%M:%S (fuso não informado)")
                status = "substituído" if not row["active"] else row["kind"]
                print(f"{display_date} | {grades.get(row['scope'], '?')} | {row['code']} — {row['name']} [{status}]")
                for complement in json.loads(row["complements"]):
                    print("  " + "; ".join(f"{k}: {v}" for k, v in complement.items()))
                enrichment = repo.movement_enrichment(org, number, row["fingerprint"])
                if enrichment:
                    print(f"  TPU {enrichment['version']}: {enrichment['official_name']}"
                          + (f" — {enrichment['official_description']}" if enrichment.get("official_description") else ""))
                    for complement in enrichment["complements"]:
                        if complement["validated"]:
                            print(f"    Complemento oficial: {complement['definition']} = {complement['official_value']}")
        elif args.command == "list":
            for watch in repo.list_watches(org):
                print(f"{format_number(watch['number'])} | {watch['court']} | Próxima: {watch['next_due']} | Erro: {watch['last_error'] or '-'}")
        elif args.command == "check-all":
            failed = False
            for number, result, error in engine.check_all(org):
                print(format_number(number))
                if error:
                    failed = True
                    print(error, file=sys.stderr)
                else:
                    show_result(result)
            if failed:
                return 1
        elif args.command in ("runs", "events"):
            number = normalize(args.number)
            rows = repo.runs(org, number) if args.command == "runs" else repo.events(org, number, args.after)
            # Raw é auditável pelo repositório; não despejar dados brutos no terminal.
            for row in rows:
                print(json.dumps({k: v for k, v in row.items() if k != "raw"}, ensure_ascii=False))
        elif args.command == "run-due":
            failed = False
            for number, result, error in run_due(engine, repo, org):
                print(format_number(number))
                if error:
                    failed = True
                    print(error, file=sys.stderr)
                else:
                    show_result(result)
            if failed:
                return 1
        elif args.command == "watch":
            from .providers.djen import DjenClient
            from dataclasses import replace
            worker_config = replace(config, djen_auto_sync=args.djen or config.djen_auto_sync)
            watch_forever(engine, repo, org, args.poll,
                          djen_client=DjenClient(min_interval=config.djen_min_interval),
                          config=worker_config, queue_mode=True)
        elif args.command == "web":
            import ipaddress
            try:
                import uvicorn
            except ImportError:
                raise RuntimeError("Instale a interface web com: python -m pip install -e '.[web]'.") from None
            from .api import create_app
            from .worker import start_background_worker
            try:
                address = ipaddress.ip_address(args.host)
            except ValueError:
                raise ValueError("Por segurança, a interface só pode escutar em 127.0.0.1 ou ::1 nesta fase.") from None
            if not address.is_loopback and not (config.auth_mode == "accounts" and config.session_cookie_secure):
                raise ValueError("Para escutar fora do computador local, habilite PROCESSWATCH_AUTH_MODE=accounts e cookies Secure atrás de HTTPS.")
            worker_org = None if config.auth_mode == "accounts" else org
            worker_stop, worker_thread = start_background_worker(db_path, config, worker_org)
            try:
                uvicorn.run(create_app(repo, engine, app_org, config, scheduler_active=True,
                    scheduler_status_provider=lambda: worker_thread.is_alive()),
                    host=args.host, port=args.port, log_level="info")
            finally:
                worker_stop.set()
                worker_thread.join(timeout=5)
        return 0
    except KeyboardInterrupt:
        print("Worker encerrado.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 1
    finally:
        if repo is not None:
            repo.close()


if __name__ == "__main__":
    raise SystemExit(main())
