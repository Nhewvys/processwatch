"""Executa uma única tentativa direta do conector eproc/TRF4 sem gravar no SQLite."""
import argparse
import asyncio
import json

from processwatch.providers.contracts import CollectionRequest
from processwatch.providers.eproc_trf4 import EprocTRF4Connector, SOURCE_ID


async def main(number):
    result = await asyncio.wait_for(EprocTRF4Connector(timeout_seconds=45).collect(
        CollectionRequest(number=number, source=SOURCE_ID, collection_type="public_case",
                          tribunal="TRF4", system="eproc")), timeout=75)
    print(json.dumps({"source": result.source, "state": result.state.value,
        "found": result.found, "stage": result.error.stage if result.error else None,
        "error_code": result.error.code if result.error else None,
        "message": result.error.message if result.error else None,
        "movement_count": len(result.data.get("movements", [])),
        "coverage": result.coverage, "limitations": list(result.limitations)},
        ensure_ascii=False, indent=2))
    return 0 if result.data else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cnj", required=True)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.cnj)))
