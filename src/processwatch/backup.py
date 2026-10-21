"""Create and verify an online SQLite backup, including committed WAL data."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import tempfile


def backup_database(database: Path, directory: Path) -> Path:
    # Path.resolve() can fail on Windows when SQLite has an open WAL handle;
    # the backup uses SQLite's read-only connection and does not need to
    # canonicalize the source through a filesystem realpath lookup.
    database = Path(os.path.abspath(database.expanduser()))
    directory = Path(os.path.abspath(directory.expanduser()))
    if not database.is_file():
        raise ValueError("O banco de origem não é um arquivo regular.")
    if database.parent == directory:
        raise ValueError("O diretório de backup deve ser separado do banco de origem.")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"processwatch-{stamp}.sqlite3"
    if target.exists():
        raise FileExistsError("Já existe um backup com este identificador de horário.")
    fd, temporary_name = tempfile.mkstemp(prefix=".processwatch-backup-", suffix=".sqlite3", dir=directory)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        source_uri = database.as_uri() + "?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source:
            with closing(sqlite3.connect(temporary, timeout=30)) as destination:
                source.backup(destination)
                if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("A cópia de segurança não passou no integrity_check.")
                if destination.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise RuntimeError("A cópia de segurança contém violações de chave estrangeira.")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        return target
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="Caminho absoluto do SQLite operacional")
    parser.add_argument("--directory", required=True, type=Path, help="Diretório restrito e separado para backups")
    args = parser.parse_args(argv)
    if not args.database.is_absolute() or not args.directory.is_absolute():
        parser.error("Informe caminhos absolutos para origem e destino.")
    try:
        result = backup_database(args.database, args.directory)
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as exc:
        parser.exit(1, f"Backup não concluído: {exc}\n")
    print(f"Backup verificado: {result}")
    return 0
