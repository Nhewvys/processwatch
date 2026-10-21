r"""Export selected official SGT class definitions without opening ProcessWatch SQLite.

Usage: .\.venv\Scripts\python.exe scripts\sgt_classes.py fetch --codes 2 268 --out data\legal\classes.json
"""
import argparse
import json
from pathlib import Path

from processwatch.legal_catalog import SgtClassCatalogService, find_class, validate_class_catalog


def main(argv=None):
    parser = argparse.ArgumentParser(description="Catálogo versionado de classes SGT/CNJ")
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="Consultar códigos de classes no WebService oficial")
    fetch.add_argument("--codes", nargs="+", required=True)
    fetch.add_argument("--out", required=True)
    inspect = commands.add_parser("inspect", help="Consultar exportação SGT já validada")
    inspect.add_argument("file")
    inspect.add_argument("query")
    args = parser.parse_args(argv)
    if args.command == "fetch":
        document = validate_class_catalog(SgtClassCatalogService().fetch_codes(args.codes))
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"path": str(target), "version": document["version"],
                          "coverage": document["coverage"]}, ensure_ascii=False))
    else:
        document = json.loads(Path(args.file).read_text(encoding="utf-8"))
        print(json.dumps(find_class(document, args.query), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
