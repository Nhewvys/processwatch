"""Client for existing administrative HTTP operations; never opens a SQLite file."""

import ctypes
import getpass
import json
import os
import sys
import sqlite3
from ctypes import wintypes
from pathlib import Path
from urllib.parse import quote, urlsplit


PERMISSIONS = {
    "processes:read": "Consultar processos e movimentações",
    "communications:read": "Consultar comunicações e intimações",
    "oab:read": "Consultar monitoramentos OAB",
    "oab:manage": "Administrar monitoramentos OAB",
    "sync:request": "Solicitar atualizações",
    "jobs:read": "Consultar jobs e estado de execução",
    "communications:read_state:write": "Alterar estado de leitura das comunicações",
}
COOKIE_NAME = "processwatch_session"


class CLIError(Exception):
    pass


def config_path():
    root = os.getenv("APPDATA")
    if not root:
        raise CLIError("APPDATA não está disponível; não é possível guardar a configuração local.")
    return Path(root) / "ProcessWatch" / "cli.json"


def read_url(override=None):
    if override:
        value = override
    else:
        path = config_path()
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get("api_url", "") if path.exists() else "http://127.0.0.1:8000"
        except (OSError, ValueError, AttributeError) as exc:
            raise CLIError("Não foi possível ler a configuração local da API; informe --url no login.") from None
    if not isinstance(value, str):
        raise CLIError("Endereço de API inválido na configuração local.")
    parsed = urlsplit(value.rstrip("/"))
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise CLIError("Endereço de API inválido. Use a origem, por exemplo http://127.0.0.1:8000.")
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise CLIError("Use HTTPS para uma API fora deste computador.")
    return value.rstrip("/")


def save_url(url):
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"api_url": url}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        raise CLIError("Não foi possível salvar o endereço da API em APPDATA.") from None


def _credential_target(url, *, platform=False):
    return "ProcessWatch CLI platform session " + url if platform else "ProcessWatch CLI session " + url


def _win_credentials():
    if sys.platform != "win32":
        raise CLIError("O armazenamento seguro da sessão requer o Gerenciador de Credenciais do Windows.")

    class FILETIME(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
                    ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
                    ("LastWritten", FILETIME), ("CredentialBlobSize", wintypes.DWORD),
                    ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                    ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                    ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                    ("UserName", wintypes.LPWSTR)]

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    return api, CREDENTIAL


def store_session(url, token, *, platform=False):
    api, credential_type = _win_credentials()
    encoded = token.encode("utf-16-le")
    blob = (ctypes.c_ubyte * len(encoded)).from_buffer_copy(encoded)
    credential = credential_type()
    credential.Type = 1  # CRED_TYPE_GENERIC
    credential.TargetName = _credential_target(url, platform=platform)
    credential.CredentialBlobSize = len(encoded)
    credential.CredentialBlob = blob
    credential.Persist = 2  # CRED_PERSIST_LOCAL_MACHINE; protected by Windows user account
    credential.UserName = "ProcessWatch"
    if not api.CredWriteW(ctypes.byref(credential), 0):
        raise CLIError(f"Não foi possível salvar a sessão no Gerenciador de Credenciais do Windows (erro {ctypes.get_last_error()}).")


def load_session(url, *, platform=False):
    api, credential_type = _win_credentials()
    pointer = ctypes.POINTER(credential_type)()
    if not api.CredReadW(_credential_target(url, platform=platform), 1, 0, ctypes.byref(pointer)):
        if ctypes.get_last_error() == 1168:  # ERROR_NOT_FOUND
            return None
        raise CLIError(f"Não foi possível ler a sessão no Gerenciador de Credenciais do Windows (erro {ctypes.get_last_error()}).")
    try:
        raw = ctypes.string_at(pointer.contents.CredentialBlob, pointer.contents.CredentialBlobSize)
        return raw.decode("utf-16-le")
    finally:
        api.CredFree(pointer)


def delete_session(url, *, platform=False):
    api, _ = _win_credentials()
    if not api.CredDeleteW(_credential_target(url, platform=platform), 1, 0) and ctypes.get_last_error() != 1168:
        raise CLIError(f"Não foi possível remover a sessão do Gerenciador de Credenciais do Windows (erro {ctypes.get_last_error()}).")


def request(url, method, path, *, payload=None, session=None, cookie_name=COOKIE_NAME):
    try:
        import httpx
    except ImportError as exc:
        raise CLIError("Instale as dependências web: python -m pip install -e '.[web]'.") from exc
    headers = {"Origin": url} if method != "GET" else {}
    cookies = {cookie_name: session} if session else None
    try:
        with httpx.Client(base_url=url, timeout=15, follow_redirects=False, cookies=cookies) as client:
            response = client.request(method, path, json=payload, headers=headers)
    except httpx.RequestError as exc:
        raise CLIError(f"Falha de conexão com a API em {url}: {exc.__class__.__name__}.") from None
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", "")
        except ValueError:
            detail = ""
        labels = {401: "Sessão ausente ou expirada", 403: "Acesso não autorizado", 404: "Recurso não encontrado"}
        raise CLIError(f"HTTP {response.status_code}: {detail or labels.get(response.status_code, 'Erro da API')}")
    try:
        return response.json(), response.cookies
    except ValueError:
        raise CLIError("A API retornou uma resposta que não é JSON.") from None


def _session(url, *, platform=False):
    token = load_session(url, platform=True) if platform else load_session(url)
    if not token:
        raise CLIError("Entre primeiro com 'processwatch login'.")
    return token


def _select_permissions():
    entries = list(PERMISSIONS.items())
    for index, (code, description) in enumerate(entries, 1):
        print(f"{index}. {description} ({code})")
    raw = input("Números das permissões, separados por vírgula: ").strip()
    try:
        chosen = [int(item.strip()) for item in raw.split(",")]
        if not chosen or any(item < 1 or item > len(entries) for item in chosen):
            raise ValueError
    except ValueError:
        raise CLIError("Seleção de permissões inválida.") from None
    return list(dict.fromkeys(entries[item - 1][0] for item in chosen))


def _days(value):
    try:
        days = int(value if value is not None else input("Validade em dias [90]: ").strip() or "90")
    except ValueError:
        raise CLIError("Validade inválida; informe um número de 1 a 365 dias.") from None
    if not 1 <= days <= 365:
        raise CLIError("Validade inválida; informe um número de 1 a 365 dias.")
    return days


def _print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _confirm(message, expected):
    return input(f"{message} Digite {expected} para confirmar: ").strip() == expected


def _without_secrets(value):
    if isinstance(value, dict):
        return {key: _without_secrets(item) for key, item in value.items()
                if key.casefold() not in ("token", "token_hash", "secret", "password")}
    if isinstance(value, list):
        return [_without_secrets(item) for item in value]
    return value


def register(sub):
    config = sub.add_parser("config", help="Configurar o endereço da API")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show", help="Mostrar o endereço da API configurado")
    set_url = config_sub.add_parser("set-url", help="Salvar novo endereço da API")
    set_url.add_argument("url", help="Origem HTTPS da API ou http://127.0.0.1:8000")
    login = sub.add_parser("login", help="Entrar na API com uma conta existente")
    login.add_argument("--url", help="Endereço da API; guardado para os próximos comandos")
    sub.add_parser("logout", help="Invalidar e remover a sessão local")
    sub.add_parser("whoami", help="Mostrar a conta e o escritório ativos")
    office = sub.add_parser("office", help="Dados do escritório")
    office_sub = office.add_subparsers(dest="office_command", required=True)
    office_sub.add_parser("show", help="Consultar dados do escritório")
    update = office_sub.add_parser("update", help="Atualizar dados do escritório")
    for field in ("name", "email", "phone", "address"):
        update.add_argument("--" + field)
    for action in ("activate", "deactivate", "delete"):
        office_sub.add_parser(action, help={"activate": "Reativar o escritório",
                                           "deactivate": "Desativar o escritório",
                                           "delete": "Excluir definitivamente o escritório"}[action])
    users = sub.add_parser("users", help="Usuários do escritório")
    users_sub = users.add_subparsers(dest="users_command", required=True)
    users_sub.add_parser("list", help="Listar usuários vinculados")
    for action, description in (("show", "Consultar usuário"), ("activate", "Reativar usuário"),
                                ("deactivate", "Desativar usuário"), ("delete", "Excluir usuário")):
        command = users_sub.add_parser(action, help=description)
        command.add_argument("user_id", type=int)
    issue_recovery = users_sub.add_parser("issue-recovery", help="Emitir token privado de recuperação de senha")
    issue_recovery.add_argument("user_id", type=int)
    create_user = users_sub.add_parser("create", help="Cadastrar usuário")
    create_user.add_argument("--email")
    create_user.add_argument("--name")
    create_user.add_argument("--role", choices=("lawyer", "office_admin"), default="lawyer")
    update_user = users_sub.add_parser("update", help="Atualizar apenas os campos informados")
    update_user.add_argument("user_id", type=int)
    update_user.add_argument("--name")
    update_user.add_argument("--role", choices=("lawyer", "office_admin"))
    account = sub.add_parser("account", help="Gerenciar a própria conta")
    account_sub = account.add_subparsers(dest="account_command", required=True)
    account_sub.add_parser("show", help="Consultar o próprio perfil")
    account_update = account_sub.add_parser("update", help="Atualizar o próprio nome")
    account_update.add_argument("--name")
    account_sub.add_parser("change-password", help="Alterar senha e encerrar sessões")
    account_sub.add_parser("delete", help="Excluir a própria conta após confirmar senha")
    keys = sub.add_parser("api-key", help="Credenciais de serviço")
    keys_sub = keys.add_subparsers(dest="key_command", required=True)
    create = keys_sub.add_parser("create")
    create.add_argument("--name")
    create.add_argument("--permissions", help="Códigos separados por vírgula; omita para seleção interativa")
    create.add_argument("--expires-in-days", type=int)
    keys_sub.add_parser("list")
    for action in ("rotate", "revoke"):
        command = keys_sub.add_parser(action)
        command.add_argument("credential_id")
        if action == "rotate":
            command.add_argument("--expires-in-days", type=int)
    platform = sub.add_parser("platform-admin", help="Administração global da plataforma")
    platform_sub = platform.add_subparsers(dest="platform_command", required=True)
    bootstrap = platform_sub.add_parser("bootstrap", help="Criar o primeiro administrador localmente no servidor")
    bootstrap.add_argument("--db", required=True, help="Caminho explícito do banco operacional/configurado no servidor")
    bootstrap.add_argument("--email")
    recover_owner = platform_sub.add_parser("reset-owner-password",
        help="Recuperar localmente a senha do proprietário usando acesso autorizado ao servidor")
    recover_owner.add_argument("--db", required=True, help="Caminho explícito do banco v18 no servidor")
    recover_owner.add_argument("--email", help="E-mail do proprietário; se omitido, será solicitado")
    platform_login = platform_sub.add_parser("login", help="Entrar na administração global")
    platform_login.add_argument("--url", help="Endereço da API; guardado para os próximos comandos")
    platform_sub.add_parser("logout", help="Encerrar a sessão global")
    platform_sub.add_parser("whoami", help="Mostrar a identidade global autenticada")
    platform_sub.add_parser("change-password", help="Alterar a própria senha global; requer a senha atual")
    offices = platform_sub.add_parser("offices", help="Administrar escritórios globalmente")
    offices_sub = offices.add_subparsers(dest="platform_office_command", required=True)
    offices_sub.add_parser("list", help="Listar escritórios e métricas disponíveis")
    offices_sub.add_parser("create", help="Provisionar escritório e primeiro administrador")
    office_update = offices_sub.add_parser("update", help="Atualizar campos cadastrais informados")
    office_update.add_argument("office_id")
    for field, help_text in (("name", "Nome cadastral"), ("email", "E-mail de contato"),
                             ("phone", "Telefone"), ("address", "Endereço")):
        office_update.add_argument("--" + field, help=help_text)
    for action in ("show", "activate", "deactivate"):
        command = offices_sub.add_parser(action)
        command.add_argument("office_id")
    users = platform_sub.add_parser("users", help="Listar e administrar contas e vínculos dos escritórios")
    users.add_argument("--office-id", help="Filtrar a listagem global por escritório")
    users_sub = users.add_subparsers(dest="platform_users_action")
    users_list = users_sub.add_parser("list", help="Listar usuários globalmente ou por escritório")
    users_list.add_argument("--office-id", dest="action_office_id")
    for action, description in (("show", "Consultar conta e vínculos"), ("create", "Criar conta ou vincular conta existente"),
                                ("update", "Alterar campos da conta/vínculo"), ("activate", "Ativar a conta global"),
                                ("deactivate", "Desativar a conta global"), ("unlink", "Remover vínculo do escritório"),
                                ("issue-recovery", "Emitir token de recuperação para usuário deste escritório")):
        command = users_sub.add_parser(action, help=description)
        if action == "create":
            command.add_argument("office_id")
        elif action == "show":
            command.add_argument("office_id")
            command.add_argument("user_id", type=int)
        elif action == "update":
            command.add_argument("office_id")
            command.add_argument("user_id", type=int)
            command.add_argument("--name", help="Nome exibido somente neste escritório")
            command.add_argument("--role", choices=("office_admin", "lawyer"))
            command.add_argument("--membership-status", choices=("active", "inactive"))
            command.add_argument("--account-status", choices=("active", "inactive"),
                                 help="Estado global da conta; afeta todos os escritórios")
        else:
            command.add_argument("office_id")
            command.add_argument("user_id", type=int)
    admins = platform_sub.add_parser("admins", help="Administrar identidades globais da plataforma")
    admins_sub = admins.add_subparsers(dest="platform_admins_action", required=True)
    admins_sub.add_parser("list", help="Listar administradores globais")
    admins_sub.add_parser("create", help="Criar administrador global delegado")
    for action, description in (("show", "Consultar administrador"), ("activate", "Ativar administrador"),
                                ("deactivate", "Desativar administrador"),
                                ("revoke-sessions", "Revogar sessões administrativas")):
        command = admins_sub.add_parser(action, help=description)
        command.add_argument("admin_id", type=int)
    update_admin = admins_sub.add_parser("update", help="Atualizar e-mail de acesso")
    update_admin.add_argument("admin_id", type=int)
    update_admin.add_argument("--email", required=True)
    reset_admin = admins_sub.add_parser("reset-password", help="Redefinir a senha de administrador delegado (somente proprietário)")
    reset_admin.add_argument("admin_id", type=int)
    jobs = platform_sub.add_parser("jobs", help="Consultar e operar jobs persistidos")
    jobs.add_argument("--office-id")
    jobs.add_argument("--process", dest="number")
    jobs.add_argument("--type", dest="collection_type")
    jobs.add_argument("--status", choices=("queued", "running", "completed", "partial", "failed", "awaiting_access", "cancelled"))
    jobs.add_argument("--source")
    jobs.add_argument("--kind", choices=("collection", "oab"))
    jobs.add_argument("--created-after", help="Data/hora ISO-8601 inclusiva")
    jobs.add_argument("--created-before", help="Data/hora ISO-8601 inclusiva")
    jobs.add_argument("--limit", type=int, default=50)
    jobs.add_argument("--offset", type=int, default=0)
    jobs_sub = jobs.add_subparsers(dest="platform_jobs_action")
    jobs_show = jobs_sub.add_parser("show", help="Consultar detalhes e histórico de um job")
    jobs_show.add_argument("kind", choices=("collection", "oab"))
    jobs_show.add_argument("job_id", type=int)
    jobs_retry = jobs_sub.add_parser("retry", help="Reenfileirar etapa falha e reconsolidar o processo")
    jobs_retry.add_argument("kind", choices=("collection", "oab"))
    jobs_retry.add_argument("job_id", type=int)
    integrations = platform_sub.add_parser("integrations", help="Consultar estado observado das integrações")
    integrations.add_argument("--office-id")
    processes = platform_sub.add_parser("processes", help="Pesquisar processos entre escritórios")
    processes.add_argument("--office-id")
    processes.add_argument("--query")
    process_actions = processes.add_subparsers(dest="platform_processes_action")
    platform_refresh = process_actions.add_parser("refresh", help="Solicitar coleta de processo (ação auditada)")
    platform_refresh.add_argument("office_id")
    platform_refresh.add_argument("number")
    oab_monitor = platform_sub.add_parser("oab-monitor", help="Solicitar consulta de monitoramento OAB existente")
    oab_monitor_sub = oab_monitor.add_subparsers(dest="platform_oab_action", required=True)
    oab_refresh = oab_monitor_sub.add_parser("refresh", help="Enfileirar consulta DJEN para um monitor OAB")
    oab_refresh.add_argument("office_id")
    oab_refresh.add_argument("monitor_id", type=int)
    platform_sub.add_parser("audit", help="Consultar auditoria administrativa")


COMMANDS = {"config", "login", "logout", "whoami", "office", "users", "account", "api-key", "platform-admin"}


def run(args):
    if args.command == "config":
        if args.config_command == "set-url":
            url = read_url(args.url)
            save_url(url)
            print(f"API configurada: {url}. Entre novamente se mudou de servidor.")
        else:
            print(read_url())
        return 0
    if args.command == "platform-admin" and args.platform_command == "bootstrap":
        from .config import Config
        from .repository import SQLiteRepository
        email = args.email or input("E-mail do administrador global: ").strip()
        password = getpass.getpass("Senha (mínimo 12 caracteres): ")
        confirmation = getpass.getpass("Confirme a senha: ")
        if password != confirmation:
            raise CLIError("As senhas não coincidem.")
        config = Config.from_env()
        repository = SQLiteRepository(args.db, environment=config.environment,
                                      primary_path=config.primary_db or None)
        try:
            result = repository.bootstrap_platform_admin(email, password)
        except (ValueError, sqlite3.IntegrityError) as exc:
            raise CLIError(str(exc)) from None
        finally:
            repository.close()
        print(f"Administrador da plataforma criado: {result['email']} (ID {result['id']}).")
        print("A identidade global é independente dos escritórios; autentique com 'processwatch platform-admin login'.")
        return 0
    if args.command == "platform-admin" and args.platform_command == "reset-owner-password":
        from getpass import getuser
        from .config import Config
        from .repository import SQLiteRepository
        email = (args.email or input("E-mail do proprietário bootstrap: ").strip()).strip()
        if not email:
            raise CLIError("Informe o e-mail do proprietário.")
        confirmation = input(f"Digite {email} para confirmar a recuperação local: ").strip()
        if confirmation.casefold() != email.casefold():
            print("Operação cancelada; o e-mail não confere.")
            return 0
        password = getpass.getpass("Nova senha do proprietário (mínimo 12 caracteres): ")
        repeat = getpass.getpass("Confirme a nova senha: ")
        if password != repeat:
            raise CLIError("As senhas não coincidem.")
        config = Config.from_env()
        repository = SQLiteRepository(args.db, environment=config.environment,
                                      primary_path=config.primary_db or None)
        try:
            result = repository.recover_platform_owner_password_locally(
                email, password, actor=f"local_operator:{getuser()}")
        except (ValueError, LookupError, sqlite3.IntegrityError) as exc:
            raise CLIError(str(exc)) from None
        finally:
            repository.close()
        print(f"Acesso local do proprietário {result['email']} restaurado; sessões anteriores revogadas.")
        return 0
    if args.command == "platform-admin" and args.platform_command == "login":
        url = read_url(args.url)
        email = input("E-mail do administrador global: ").strip()
        password = getpass.getpass("Senha: ")
        result, cookies = request(url, "POST", "/api/platform/auth/login",
                                  payload={"email": email, "password": password},
                                  cookie_name="processwatch_platform_session")
        token = cookies.get("processwatch_platform_session")
        if not token:
            raise CLIError("Login sem cookie administrativo; confira a versão da API.")
        store_session(url, token, platform=True)
        save_url(url)
        print(f"Autenticado como administrador global: {result['administrator']['email']}.")
        return 0
    if args.command == "platform-admin":
        url = read_url()
        token = _session(url, platform=True)
        action = args.platform_command
        if action == "logout":
            try:
                request(url, "POST", "/api/platform/auth/logout", session=token,
                        cookie_name="processwatch_platform_session")
            finally:
                delete_session(url, platform=True)
            print("Sessão global encerrada.")
        elif action == "change-password":
            current = getpass.getpass("Senha atual: ")
            new = getpass.getpass("Nova senha (mínimo 12 caracteres): ")
            repeat = getpass.getpass("Confirme a nova senha: ")
            if new != repeat:
                raise CLIError("As novas senhas não coincidem.")
            _print_json(request(url, "POST", "/api/platform/auth/password",
                payload={"current_password": current, "new_password": new}, session=token,
                cookie_name="processwatch_platform_session")[0])
            delete_session(url, platform=True)
        elif action == "whoami":
            _print_json(request(url, "GET", "/api/platform/auth/me", session=token,
                                cookie_name="processwatch_platform_session")[0])
        elif action == "offices":
            operation = args.platform_office_command
            if operation == "list":
                result = request(url, "GET", "/api/platform/offices", session=token,
                                 cookie_name="processwatch_platform_session")[0]
            elif operation == "create":
                office_id = input("Identificador do escritório: ").strip()
                name = input("Nome do escritório: ").strip()
                office_email = input("E-mail de contato do escritório (opcional): ").strip()
                phone = input("Telefone (opcional): ").strip()
                address = input("Endereço (opcional): ").strip()
                administrator_email = input("E-mail do primeiro administrador: ").strip()
                administrator_name = input("Nome do primeiro administrador: ").strip()
                password = getpass.getpass("Senha do administrador (mínimo 12 caracteres): ")
                confirmation = getpass.getpass("Confirme a senha: ")
                if password != confirmation:
                    raise CLIError("As senhas não coincidem.")
                result = request(url, "POST", "/api/platform/offices", payload={
                    "office_id": office_id, "name": name, "email": office_email, "phone": phone,
                    "address": address, "administrator_email": administrator_email,
                    "administrator_name": administrator_name, "administrator_password": password},
                    session=token, cookie_name="processwatch_platform_session")[0]
            else:
                office_id = args.office_id
                if operation == "show":
                    result = request(url, "GET", f"/api/platform/offices/{quote(office_id, safe='')}",
                                     session=token, cookie_name="processwatch_platform_session")[0]
                elif operation == "update":
                    payload = {field: getattr(args, field) for field in ("name", "email", "phone", "address")
                               if getattr(args, field) is not None}
                    if not payload:
                        raise CLIError("Informe ao menos um campo: --name, --email, --phone ou --address.")
                    result = request(url, "PATCH",
                                     f"/api/platform/offices/{quote(office_id, safe='')}",
                                     payload=payload, session=token,
                                     cookie_name="processwatch_platform_session")[0]
                else:
                    result = request(url, "PATCH", f"/api/platform/offices/{quote(office_id, safe='')}/status",
                                     payload={"active": operation == "activate"}, session=token,
                                     cookie_name="processwatch_platform_session")[0]
            _print_json(result)
        elif action == "users":
            operation = args.platform_users_action
            if operation in (None, "list"):
                office_id = getattr(args, "action_office_id", None) or args.office_id
                if office_id:
                    path = "/api/platform/offices/" + quote(office_id, safe="") + "/users"
                else:
                    path = "/api/platform/users"
            elif operation == "create":
                office_id = args.office_id
                email = input("E-mail da conta: ").strip()
                full_name = input("Nome completo: ").strip()
                role = input("Papel [lawyer/office_admin] (lawyer): ").strip() or "lawyer"
                if role not in ("lawyer", "office_admin"):
                    raise CLIError("Papel inválido; escolha lawyer ou office_admin.")
                password = getpass.getpass("Senha temporária (Enter para vincular conta existente): ")
                payload = {"email": email, "full_name": full_name, "role": role}
                if password:
                    confirmation = getpass.getpass("Confirme a senha: ")
                    if password != confirmation:
                        raise CLIError("As senhas não coincidem.")
                    payload["temporary_password"] = password
                path = "/api/platform/offices/" + quote(office_id, safe="") + "/users"
                result = request(url, "POST", path, payload=payload, session=token,
                                 cookie_name="processwatch_platform_session")[0]
                _print_json(result)
                return 0
            elif operation == "issue-recovery":
                if not _confirm(
                    f"Emitir token de recuperação de uso único para user_id={args.user_id} no escritório {args.office_id}.",
                    str(args.user_id),
                ):
                    print("Operação cancelada.")
                    return 0
                path = (f"/api/platform/offices/{quote(args.office_id, safe='')}/users/"
                        f"{args.user_id}/recovery")
                result = request(url, "POST", path, session=token,
                                 cookie_name="processwatch_platform_session")[0]
                print("Este token é secreto e aparece uma única vez. Entregue-o somente por canal privado.")
                _print_json(result)
                return 0
            elif operation == "show":
                path = (f"/api/platform/offices/{quote(args.office_id, safe='')}/users/"
                        f"{args.user_id}")
            elif operation == "update":
                payload = {}
                if args.name is not None:
                    payload["display_name"] = args.name
                if args.role is not None:
                    payload["role"] = args.role
                if args.membership_status is not None:
                    payload["active"] = args.membership_status == "active"
                if args.account_status is not None:
                    payload["account_active"] = args.account_status == "active"
                if not payload:
                    raise CLIError("Informe ao menos um campo para atualizar.")
                if payload.get("account_active") is False and not _confirm(
                    f"Desativar globalmente a conta do usuário {args.user_id}; isso revoga sessões e chaves de serviço.",
                    str(args.user_id),
                ):
                    print("Operação cancelada.")
                    return 0
                path = (f"/api/platform/offices/{quote(args.office_id, safe='')}/users/"
                        f"{args.user_id}")
                result = request(url, "PATCH", path, payload=payload, session=token,
                                 cookie_name="processwatch_platform_session")[0]
                _print_json(result)
                return 0
            elif operation in ("activate", "deactivate"):
                if operation == "deactivate" and not _confirm(
                    f"Desativar globalmente a conta do usuário {args.user_id}; isso afeta todos os escritórios.",
                    str(args.user_id),
                ):
                    print("Operação cancelada.")
                    return 0
                path = (f"/api/platform/offices/{quote(args.office_id, safe='')}/users/"
                        f"{args.user_id}")
                result = request(url, "PATCH", path,
                    payload={"account_active": operation == "activate"}, session=token,
                    cookie_name="processwatch_platform_session")[0]
                _print_json(result)
                return 0
            else:
                if not _confirm(
                    f"Remover o vínculo do usuário {args.user_id} com o escritório {args.office_id}.",
                    str(args.user_id),
                ):
                    print("Operação cancelada.")
                    return 0
                path = (f"/api/platform/offices/{quote(args.office_id, safe='')}/users/"
                        f"{args.user_id}")
                result = request(url, "DELETE", path, session=token,
                                 cookie_name="processwatch_platform_session")[0]
                _print_json(result)
                return 0
            _print_json(request(url, "GET", path, session=token,
                                cookie_name="processwatch_platform_session")[0])
            return 0
        elif action == "admins":
            operation = args.platform_admins_action
            if operation == "list":
                result = request(url, "GET", "/api/platform/admins", session=token,
                                 cookie_name="processwatch_platform_session")[0]
            elif operation == "create":
                email = input("E-mail do administrador global: ").strip()
                password = getpass.getpass("Senha inicial (mínimo 12 caracteres): ")
                confirmation = getpass.getpass("Confirme a senha: ")
                if password != confirmation:
                    raise CLIError("As senhas não coincidem.")
                result = request(url, "POST", "/api/platform/admins",
                    payload={"email": email, "password": password}, session=token,
                    cookie_name="processwatch_platform_session")[0]
            elif operation == "reset-password":
                if not _confirm(
                    f"Redefinir a senha do administrador global delegado {args.admin_id}; as sessões serão revogadas.",
                    str(args.admin_id),
                ):
                    print("Operação cancelada.")
                    return 0
                password = getpass.getpass("Nova senha (mínimo 12 caracteres): ")
                confirmation = getpass.getpass("Confirme a nova senha: ")
                if password != confirmation:
                    raise CLIError("As senhas não coincidem.")
                result = request(url, "POST",
                    f"/api/platform/admins/{args.admin_id}/password/reset",
                    payload={"new_password": password}, session=token,
                    cookie_name="processwatch_platform_session")[0]
            elif operation == "show":
                result = request(url, "GET", f"/api/platform/admins/{args.admin_id}", session=token,
                                 cookie_name="processwatch_platform_session")[0]
            elif operation == "update":
                result = request(url, "PATCH", f"/api/platform/admins/{args.admin_id}",
                    payload={"email": args.email}, session=token,
                    cookie_name="processwatch_platform_session")[0]
            elif operation in ("activate", "deactivate"):
                if operation == "deactivate" and not _confirm(
                    f"Desativar o administrador global {args.admin_id} e revogar suas sessões.",
                    str(args.admin_id),
                ):
                    print("Operação cancelada.")
                    return 0
                result = request(url, "PATCH", f"/api/platform/admins/{args.admin_id}/status",
                    payload={"active": operation == "activate"}, session=token,
                    cookie_name="processwatch_platform_session")[0]
            else:
                if not _confirm(f"Revogar as sessões do administrador global {args.admin_id}.",
                                str(args.admin_id)):
                    print("Operação cancelada.")
                    return 0
                result = request(url, "POST",
                    f"/api/platform/admins/{args.admin_id}/sessions/revoke", session=token,
                    cookie_name="processwatch_platform_session")[0]
            _print_json(result)
            return 0
        elif action == "jobs":
            from urllib.parse import urlencode
            operation = getattr(args, "platform_jobs_action", None)
            if operation == "show":
                path = f"/api/platform/jobs/{args.kind}/{args.job_id}"
                result = request(url, "GET", path, session=token,
                                 cookie_name="processwatch_platform_session")[0]
            elif operation == "retry":
                if not _confirm(f"Reenfileirar a coleta {args.kind} do job {args.job_id}.", str(args.job_id)):
                    print("Operação cancelada.")
                    return 0
                purpose = input("Motivo operacional para registrar na auditoria: ").strip()
                if len(purpose) < 8:
                    raise CLIError("Informe um motivo com pelo menos 8 caracteres.")
                path = f"/api/platform/jobs/{args.kind}/{args.job_id}/retry"
                result = request(url, "POST", path, payload={"purpose": purpose}, session=token,
                                 cookie_name="processwatch_platform_session")[0]
            else:
                params = {key: value for key, value in {
                    "office_id": args.office_id, "number": args.number,
                    "collection_type": args.collection_type, "status": args.status,
                    "source": args.source, "job_kind": args.kind,
                    "created_after": args.created_after, "created_before": args.created_before,
                    "limit": args.limit, "offset": args.offset}.items() if value is not None}
                path = "/api/platform/jobs?" + urlencode(params)
                result = request(url, "GET", path, session=token,
                                 cookie_name="processwatch_platform_session")[0]
            _print_json(result)
        elif action == "integrations":
            from urllib.parse import urlencode
            params = {"office_id": args.office_id} if args.office_id else {}
            path = "/api/platform/integrations" + ("?" + urlencode(params) if params else "")
            _print_json(request(url, "GET", path, session=token,
                                cookie_name="processwatch_platform_session")[0])
        elif action == "oab-monitor":
            if args.platform_oab_action == "refresh":
                if not _confirm(
                    f"Solicitar consulta DJEN do monitor {args.monitor_id} no escritório {args.office_id}.",
                    str(args.monitor_id),
                ):
                    print("Operação cancelada.")
                    return 0
                purpose = input("Motivo operacional para registrar na auditoria: ").strip()
                if len(purpose) < 8:
                    raise CLIError("Informe um motivo com pelo menos 8 caracteres.")
                path = (f"/api/platform/offices/{quote(args.office_id, safe='')}/oab-monitors/"
                        f"{args.monitor_id}/refresh")
                result = request(url, "POST", path, payload={"purpose": purpose}, session=token,
                                 cookie_name="processwatch_platform_session")[0]
                _print_json(result)
        elif action == "processes" and getattr(args, "platform_processes_action", None) == "refresh":
            if not _confirm(
                f"Solicitar nova coleta do processo {args.number} no escritório {args.office_id}.",
                args.number,
            ):
                print("Operação cancelada.")
                return 0
            number = quote(args.number, safe="")
            office_id = quote(args.office_id, safe="")
            purpose = input("Motivo operacional para registrar na auditoria: ").strip()
            if len(purpose) < 8:
                raise CLIError("Informe um motivo com pelo menos 8 caracteres.")
            path = f"/api/platform/processes/{office_id}/{number}/refresh"
            _print_json(request(url, "POST", path, payload={"purpose": purpose}, session=token,
                                cookie_name="processwatch_platform_session")[0])
        else:
            paths = {"jobs": "/api/platform/jobs",
                     "integrations": "/api/platform/integrations", "audit": "/api/platform/audit"}
            if action == "processes":
                from urllib.parse import urlencode
                query = urlencode({key: value for key, value in {
                    "office_id": args.office_id, "q": args.query}.items() if value})
                path = "/api/platform/processes?" + query
            else:
                path = paths[action]
            _print_json(request(url, "GET", path, session=token,
                                cookie_name="processwatch_platform_session")[0])
        return 0
    url = read_url(args.url if args.command == "login" else None)
    if args.command == "login":
        email = input("E-mail: ").strip()
        password = getpass.getpass("Senha: ")
        result, cookies = request(url, "POST", "/api/auth/login", payload={"email": email, "password": password})
        token = cookies.get(COOKIE_NAME)
        if not token:
            raise CLIError("Login sem cookie de sessão; verifique o modo accounts da API.")
        store_session(url, token)
        save_url(url)
        print(f"Autenticado no escritório {result['active_office_id']}.")
        return 0
    token = _session(url)
    if args.command == "logout":
        try:
            request(url, "POST", "/api/auth/logout", session=token)
        finally:
            delete_session(url)
        print("Sessão encerrada.")
        return 0
    if args.command == "whoami":
        _print_json(request(url, "GET", "/api/auth/me", session=token)[0])
    elif args.command == "office":
        office = request(url, "GET", "/api/office", session=token)[0]
        if args.office_command == "show":
            _print_json(office)
        elif args.office_command == "update":
            payload = {field: getattr(args, field) if getattr(args, field) is not None else office.get(field, "")
                       for field in ("name", "email", "phone", "address")}
            _print_json(request(url, "PUT", "/api/office", payload=payload, session=token)[0])
        else:
            office_id = office["id"]
            if not _confirm(f"{args.office_command} do escritório {office_id}.", office_id):
                print("Operação cancelada.")
                return 0
            method = "DELETE" if args.office_command == "delete" else "POST"
            path = "/api/office" if method == "DELETE" else f"/api/office/{args.office_command}"
            _print_json(request(url, method, path, payload={"office_id": office_id}, session=token)[0])
            if args.office_command == "delete":
                delete_session(url)
    elif args.command == "users":
        action = args.users_command
        if action == "list":
            _print_json(request(url, "GET", "/api/admin/users", session=token)[0])
        elif action == "issue-recovery":
            if not _confirm(f"Emitir token de recuperação de uso único para o usuário {args.user_id}.",
                            str(args.user_id)):
                print("Operação cancelada.")
                return 0
            result = request(url, "POST", f"/api/admin/users/{args.user_id}/recovery",
                             session=token)[0]
            print("Este token é secreto e aparece uma única vez. Entregue-o somente por canal privado.")
            _print_json(result)
        elif action == "show":
            _print_json(request(url, "GET", f"/api/admin/users/{args.user_id}", session=token)[0])
        elif action == "create":
            email = args.email or input("E-mail: ").strip()
            name = args.name or input("Nome: ").strip()
            password = getpass.getpass("Senha temporária (mínimo 12 caracteres; Enter para conta existente): ")
            payload = {"email": email, "full_name": name, "role": args.role}
            if password:
                payload["temporary_password"] = password
            _print_json(request(url, "POST", "/api/admin/users", payload=payload, session=token)[0])
        elif action == "update":
            payload = {key: value for key, value in (("display_name", args.name), ("role", args.role))
                       if value is not None}
            if not payload:
                raise CLIError("Informe --name ou --role para atualizar o usuário.")
            _print_json(request(url, "PATCH", f"/api/admin/users/{args.user_id}", payload=payload, session=token)[0])
        elif action in ("activate", "deactivate"):
            _print_json(request(url, "PATCH", f"/api/admin/users/{args.user_id}",
                                payload={"active": action == "activate"}, session=token)[0])
        elif action == "delete":
            if not _confirm(f"Excluir o usuário {args.user_id} deste escritório.", str(args.user_id)):
                print("Operação cancelada.")
                return 0
            _print_json(request(url, "DELETE", f"/api/admin/users/{args.user_id}", session=token)[0])
    elif args.command == "account":
        action = args.account_command
        if action == "show":
            _print_json(request(url, "GET", "/api/profile", session=token)[0])
        elif action == "update":
            name = args.name or input("Novo nome: ").strip()
            _print_json(request(url, "PUT", "/api/profile", payload={"display_name": name}, session=token)[0])
        elif action == "change-password":
            current = getpass.getpass("Senha atual: ")
            new = getpass.getpass("Nova senha (mínimo 12 caracteres): ")
            repeat = getpass.getpass("Confirme a nova senha: ")
            if new != repeat:
                raise CLIError("As novas senhas não coincidem.")
            _print_json(request(url, "POST", "/api/profile/password", payload={"current_password": current,
                                "new_password": new}, session=token)[0])
            delete_session(url)
        elif action == "delete":
            profile = request(url, "GET", "/api/profile", session=token)[0]
            email = profile["user"]["email"]
            if not _confirm("Excluir definitivamente a própria conta.", email):
                print("Operação cancelada.")
                return 0
            password = getpass.getpass("Senha atual: ")
            _print_json(request(url, "DELETE", "/api/profile", payload={"current_password": password}, session=token)[0])
            delete_session(url)
    elif args.command == "api-key":
        if args.key_command == "list":
            _print_json(_without_secrets(request(url, "GET", "/api/service-credentials", session=token)[0]))
        elif args.key_command == "create":
            name = args.name or input("Nome do serviço: ").strip()
            permissions = ([item.strip() for item in args.permissions.split(",")]
                           if args.permissions else _select_permissions())
            if not permissions or any(item not in PERMISSIONS for item in permissions):
                raise CLIError("Permissões inválidas.")
            result = request(url, "POST", "/api/service-credentials",
                             payload={"name": name, "permissions": permissions,
                                      "expires_in_days": _days(args.expires_in_days)}, session=token)[0]
            _print_json(result)  # API exposes the secret only in this one-time response.
        else:
            if input(f"Confirmar {args.key_command} da credencial {args.credential_id}? [s/N] ").strip().lower() != "s":
                print("Operação cancelada.")
                return 0
            path = f"/api/service-credentials/{quote(args.credential_id, safe='')}/{args.key_command}"
            payload = {"expires_in_days": _days(args.expires_in_days)} if args.key_command == "rotate" else None
            result = request(url, "POST", path, payload=payload, session=token)[0]
            _print_json(result if args.key_command == "rotate" else _without_secrets(result))
    return 0
