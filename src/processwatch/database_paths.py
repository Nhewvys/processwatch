"""Database path resolution and environment isolation checks."""

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRIMARY_DB = PROJECT_ROOT / "processwatch.sqlite3"
_PRIMARY_ENVIRONMENTS = {"principal", "production"}
_KNOWN_ENVIRONMENTS = _PRIMARY_ENVIRONMENTS | {"development", "test", "homologation"}


def _resolve(path):
    if path == ":memory:":
        return path
    if not path or str(path).startswith("file:"):
        raise ValueError("Informe um caminho de arquivo SQLite válido; URI file: não é aceita.")
    return Path(path).expanduser().resolve(strict=False)


def _same_file(left, right):
    if os.path.normcase(str(left)) == os.path.normcase(str(right)):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def validate_database_path(path, *, environment="development", primary_path=None):
    """Resolve a database path and reject physical aliases to the principal DB."""
    environment = (environment or "development").strip().lower()
    if environment not in _KNOWN_ENVIRONMENTS:
        raise ValueError(f"Ambiente ProcessWatch desconhecido: {environment!r}.")

    resolved = _resolve(str(path))
    if resolved == ":memory:":
        if environment == "production":
            raise ValueError("Produção exige um arquivo SQLite persistente em caminho absoluto.")
        return resolved

    if environment == "production" and not Path(path).expanduser().is_absolute():
        raise ValueError("Produção exige PROCESSWATCH_DB em caminho absoluto.")

    primary = _resolve(str(primary_path or os.getenv("PROCESSWATCH_PRIMARY_DB") or DEFAULT_PRIMARY_DB))
    if environment not in _PRIMARY_ENVIRONMENTS and _same_file(resolved, primary):
        raise ValueError(
            f"Banco recusado: o ambiente '{environment}' aponta para o banco principal "
            f"({primary}). Configure PROCESSWATCH_DB para um arquivo isolado."
        )
    return str(resolved)
