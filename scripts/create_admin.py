from getpass import getpass
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from processwatch.config import Config
from processwatch.repository import SQLiteRepository


def main():
    parser = argparse.ArgumentParser(description="Criar usuário do ProcessWatch sem senha na linha de comando.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--office-id", default="local")
    parser.add_argument("--office-name", default="Escritório inicial")
    parser.add_argument("--role", choices=("office_admin", "lawyer"), default="office_admin")
    parser.add_argument("--db", help="Banco SQLite; por padrão usa PROCESSWATCH_DB.")
    args = parser.parse_args()
    config = Config.from_env()
    repo = SQLiteRepository(args.db or config.db)
    try:
        if not repo.db.execute("SELECT 1 FROM offices WHERE id=?", (args.office_id,)).fetchone():
            repo.create_office(args.office_id, args.office_name)
        password = getpass("Nova senha (mínimo 12 caracteres): ")
        confirmation = getpass("Confirme a senha: ")
        if password != confirmation:
            raise ValueError("As senhas não coincidem.")
        user = repo.create_user(args.email, password, args.office_id, role=args.role)
        label = "Administrador" if args.role == "office_admin" else "Advogado"
        print(f"{label} criado para {user['email']} no escritório {args.office_id}.")
        print("Configure PROCESSWATCH_AUTH_MODE=accounts para habilitar login por conta.")
    finally:
        repo.close()


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        raise SystemExit(f"Erro: {exc}") from None
