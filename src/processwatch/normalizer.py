import hashlib
import json
from datetime import datetime, timezone
from typing import Any
from .models import Movement, Snapshot


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def timestamp(value: str) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Sem timezone: não inventar o fuso. Preservar como horário não qualificado.
    return dt.astimezone(timezone.utc).isoformat() if dt.tzinfo else dt.isoformat()


def normalize_response(raw: dict, number: str, court: str) -> Snapshot:
    if raw.get("timed_out") or raw.get("_shards", {}).get("failed", 0):
        raise ValueError("DataJud retornou resposta parcial; linha de base preservada.")
    hits = raw["hits"]["hits"]
    if not isinstance(hits, list) or not hits:
        raise ValueError("Processo não localizado; ausência não significa encerramento.")
    movements, metadata, scopes = {}, [], set()
    for hit in hits:
        source = hit["_source"]
        if source["numeroProcesso"] != number:
            raise ValueError("Número retornado não corresponde à consulta.")
        if str(source.get("tribunal", "")).lower().replace("-", "") != court.replace("-", ""):
            raise ValueError("Tribunal retornado não corresponde ao conector.")
        grade = source["grau"]
        # ID da fonte mantém documentos de graus/sistemas distintos separados.
        scope = digest(["datajud", number, court, grade,
                        source.get("id") or hit.get("_id") or source.get("sistema", {}).get("codigo")])
        scopes.add(scope)
        meta = {k: v for k, v in source.items() if k != "movimentos"}
        meta["_scope"] = scope
        metadata.append(meta)
        for item in source.get("movimentos", []):
            when = timestamp(item["dataHora"])
            code = str(item["codigo"])
            complements = sorted(item.get("complementosTabelados") or [], key=canonical)
            body = dict(item, codigo=code, dataHora=when, complementosTabelados=complements)
            # ID do movimento, quando disponível, permite correção de data/código.
            anchor = digest([scope, "id", item["id"]]) if item.get("id") is not None else digest([scope, code, when])
            fp = digest([scope, body])
            movements[fp] = Movement(scope, anchor, fp, when, code,
                                     str(item.get("nome", "Sem descrição")), complements, item)
    return Snapshot(metadata, list(movements.values()), sorted(scopes), raw)
