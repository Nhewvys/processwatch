"""Validated national court/source registry loaded from the project data file."""
import json
from pathlib import Path


_PROJECT_MATRIX = Path(__file__).resolve().parents[2] / "data" / "sources" / "courts.json"
_BUNDLED_MATRIX = Path(__file__).resolve().parent / "_data" / "sources" / "courts.json"
MATRIX = _PROJECT_MATRIX if _PROJECT_MATRIX.is_file() else _BUNDLED_MATRIX


def load_document(path=MATRIX):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != 2:
        raise ValueError("Matriz nacional de fontes com versão desconhecida.")
    sources, tribunals = document.get("sources"), document.get("tribunals")
    if not isinstance(sources, list) or not isinstance(tribunals, list):
        raise ValueError("Matriz nacional de fontes inválida.")
    ids, combos = set(), set()
    for item in sources:
        required = ("id", "court", "justice_segment", "system", "degree", "integration_state")
        if not all(item.get(key) for key in required):
            raise ValueError("Fonte sem tribunal, sistema, grau ou estado.")
        if item["id"] in ids:
            raise ValueError("Identificador de fonte duplicado.")
        ids.add(item["id"])
        combo = (item["court"].casefold(), item["system"].casefold(), item["degree"].casefold())
        if combo in combos:
            raise ValueError("Combinação tribunal/sistema/grau duplicada.")
        combos.add(combo)
        for key in ("official_url", "query_url"):
            value = item.get(key)
            if value is not None and not value.startswith("https://"):
                raise ValueError("URL oficial deve usar HTTPS ou ser nula quando não verificada.")
        if item["integration_state"] == "operational" and not item.get("real_validation"):
            raise ValueError("Fonte marcada operacional sem validação real.")
        if item["automated_access"].startswith("autorizado") and not item.get("real_validation"):
            raise ValueError("Acesso automatizado autorizado sem evidência de validação.")
    tribunal_ids, tribunal_cnjs, aliases = set(), set(), set()
    for item in tribunals:
        if not all(item.get(key) for key in ("id", "official_name", "justice_segment", "cnj_segment", "cnj_tribunal_code")):
            raise ValueError("Tribunal sem identificação CNJ ou segmento.")
        if item["id"].casefold() in tribunal_ids:
            raise ValueError("Identificador de tribunal duplicado.")
        tribunal_ids.add(item["id"].casefold())
        combo = (item["cnj_segment"], item["cnj_tribunal_code"])
        if combo in tribunal_cnjs:
            raise ValueError("Identificador CNJ de tribunal duplicado.")
        tribunal_cnjs.add(combo)
        alias = item.get("datajud_alias")
        if alias:
            if alias.casefold() in aliases:
                raise ValueError("Alias DataJud duplicado.")
            aliases.add(alias.casefold())
            expected = f"https://api-publica.datajud.cnj.jus.br/api_publica_{alias}/_search"
            if item.get("datajud_endpoint") != expected:
                raise ValueError("Endpoint DataJud não corresponde ao alias oficial registrado.")
        elif item.get("datajud_endpoint"):
            raise ValueError("Endpoint DataJud informado sem alias.")
    return document


def load_matrix(path=MATRIX):
    """Return court-specific source combinations, keeping the legacy CLI shape."""
    return load_document(path)["sources"]


def load_tribunals(path=MATRIX):
    return load_document(path)["tribunals"]


def get_source(source_id, path=MATRIX):
    sources = load_matrix(path)
    exact = next((item for item in sources if item["id"] == source_id), None)
    if exact:
        return exact
    # Preserve IDs exposed by the original four-entry matrix to older callers.
    legacy = {"TRF1-PJE": "TRF1-PJE-1G", "TRF4-EPROC": "TRF4-EPROC-1G",
              "TJSP-ESAJ": "TJSP-ESAJ-1G", "TJPR-PROJUDI": "TJPR-PROJUDI-1G"}
    return next((item for item in sources if item["id"] == legacy.get(source_id)), None)


def get_tribunal(court_id, path=MATRIX):
    return next((item for item in load_tribunals(path)
                 if item["id"].casefold() == str(court_id).casefold()
                 or (item.get("datajud_alias") or "").casefold() == str(court_id).casefold()), None)


def datajud_alias(court_id, path=MATRIX):
    item = get_tribunal(court_id, path)
    return item.get("datajud_alias") if item else None
