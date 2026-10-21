from getpass import getpass
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from processwatch.config import Config
from processwatch.repository import SQLiteRepository


def issue_password_recovery_token(repo, admin_email, admin_password, office_id, user_email):
    admin = repo.authenticate_user(admin_email, admin_password)
    if not admin or not any(item["id"] == office_id and item["role"] == "office_admin"
                            for item in admin["offices"]):
        raise PermissionError("Administrador inválido ou sem papel neste escritório.")
    target = repo.find_user(user_email)
    if not target:
        raise LookupError("Conta ativa não encontrada neste escritório.")
    return repo.issue_office_password_recovery_token(
        target["id"], office_id, actor=f"office_admin:{admin['email']}", ttl_minutes=30)["token"]


def main():
    parser = argparse.ArgumentParser(description="Emitir token de recuperação local, autenticado por administrador do escritório.")
    parser.add_argument("--admin-email", required=True)
    parser.add_argument("--office-id", required=True)
    parser.add_argument("--user-email", required=True)
    parser.add_argument("--db", help="Banco SQLite; por padrão usa PROCESSWATCH_DB.")
    args = parser.parse_args()
    config = Config.from_env()
    repo = SQLiteRepository(args.db or config.db, environment=config.environment,
                            primary_path=config.primary_db or None)
    try:
        try:
            token = issue_password_recovery_token(
                repo, args.admin_email, getpass("Senha do administrador: "),
                args.office_id, args.user_email)
        except (PermissionError, LookupError) as exc:
            raise SystemExit(str(exc)) from None
        print("Token de uso único (válido por 30 minutos). Compartilhe somente em canal privado com o usuário:")
        print(token)
        print("O token não foi gravado em logs; somente seu hash foi armazenado no banco.")
    finally:
        repo.close()


if __name__ == "__main__":
    main()
