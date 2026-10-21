"""Evidence-based routing from CNJ/official metadata to source capabilities."""
from dataclasses import asdict, dataclass, field
from urllib.parse import urlsplit

from .cnj import normalize
from .source_matrix import MATRIX, load_matrix, load_tribunals


def _system_name(value):
    if isinstance(value, dict):
        label = value.get("nome") or value.get("name") or value.get("sigla")
        if label:
            value = label
        else:
            # DataJud's official FAQ documents these dscSistema codes in
            # transmissions. Accept code-only metadata without guessing from CNJ.
            code = str(value.get("codigo") or value.get("code") or "").strip()
            value = {
                "1": "pje", "2": "projudi", "3": "e-saj", "4": "eproc",
                "5": "apolo", "6": "themis", "7": "libra", "8": "outros",
            }.get(code, code)
    normalized = " ".join(str(value or "").casefold().replace("_", " ").split())
    aliases = {"saj": "e-saj", "esaj": "e-saj", "e saj": "e-saj",
               "e proc": "eproc", "projudi": "projudi", "pje": "pje"}
    return aliases.get(normalized, normalized)


def _degree(value):
    text = " ".join(str(value or "").casefold().replace("º", "o").replace("°", "o").split())
    mapping = {"1g": "1º grau", "g1": "1º grau", "1ºg": "1º grau", "1o grau": "1º grau", "1 grau": "1º grau",
               "2g": "2º grau", "g2": "2º grau", "2ºg": "2º grau", "2o grau": "2º grau", "2 grau": "2º grau",
               "je": "JE", "juizado especial": "JE", "tr": "TR", "tru": "TRU", "tnu": "TNU", "sup": "SUP"}
    return mapping.get(text)


def _source_state(item, *, connector_available=False):
    matrix_state = item.get("integration_state")
    if matrix_state == "operational" and item.get("real_validation") and connector_available:
        return "operational"
    if item.get("authentication", "").casefold().find("obrigat") >= 0:
        return "authentication_required"
    if "captcha" in item.get("captcha", "").casefold() and "identificado" in item.get("captcha", "").casefold():
        return "captcha_identified"
    if not item.get("official_url"):
        return "not_verified"
    if matrix_state in ("blocked_credentials", "authentication_required"):
        return "authentication_required"
    if matrix_state == "layout_incompatible":
        return "layout_incompatible"
    return "not_implemented"


@dataclass(frozen=True)
class RoutingDecision:
    number: str
    tribunal_id: str | None
    justice_segment: str | None
    datajud_alias: str | None
    datajud_status: str
    system_names: list[str] = field(default_factory=list)
    grades: list[str] = field(default_factory=list)
    court_candidates: list[dict] = field(default_factory=list)
    selected_connector: str | None = None
    status: str = "system_unconfirmed"
    evidence: list[dict] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def as_dict(self):
        return asdict(self)


def route_process(number, metadata=None, *, matrix_path=MATRIX, connector_ids=None):
    """Choose only connectors with exact court/system/grade evidence and live registration.

    `metadata` is the stored DataJud metadata list or one source record. Unknown grade or
    system leaves the choice ambiguous; the CNJ number alone never implies a case system.
    """
    if connector_ids is None:
        from .providers.pje_trf1 import registered_connector_ids
        connector_ids = registered_connector_ids()
    number = normalize(number)
    code = "".join(filter(str.isdigit, number))
    segment, tribunal_code = code[13], code[14:16]
    tribunal = next((entry for entry in load_tribunals(matrix_path)
                     if entry["cnj_segment"] == segment and entry["cnj_tribunal_code"] == tribunal_code), None)
    if isinstance(metadata, dict):
        metadata = [metadata]
    metadata = metadata or []
    systems, grades, tribunal_names = set(), set(), set()
    for record in metadata:
        if not isinstance(record, dict):
            continue
        system = _system_name(record.get("sistema"))
        if system:
            systems.add(system)
        degree = _degree(record.get("grau"))
        if degree:
            grades.add(degree)
        court_name = str(record.get("tribunal") or "").casefold().replace("-", "").replace(" ", "")
        if court_name:
            tribunal_names.add(court_name)

    if not tribunal:
        return RoutingDecision(number, None, segment, None, "not_available",
                               sorted(systems), sorted(grades), status="unknown_tribunal",
                               limitations=["Tribunal CNJ não consta da matriz validada."]).as_dict()

    alias = tribunal.get("datajud_alias")
    datajud_status = "operational" if alias else "not_implemented"
    court_id = tribunal["id"]
    code_alias = (tribunal.get("datajud_alias") or court_id).casefold().replace("-", "")
    evidence = [{"source": "CNJ", "field": "numero CNJ", "value": f"segmento {segment}, tribunal {tribunal_code}"}]
    if alias:
        evidence.append({"source": "DataJud endpoint registry", "alias": alias,
                         "url": tribunal.get("datajud_endpoint")})
    candidates = [item for item in load_matrix(matrix_path)
                  if item["court"].casefold() == court_id.casefold()]
    for item in candidates:
        item["routing_status"] = _source_state(item, connector_available=item["id"] in connector_ids)
        item["matches_metadata"] = bool(
            systems and _system_name(item["system"]) in systems and
            grades and _degree(item["degree"]) in grades
        )
        item["reason"] = ("correspondência exata de tribunal, sistema e grau" if item["matches_metadata"]
                          else "sistema/grau ainda não confirmados em metadados oficiais")
        evidence.append({"source": item["id"], "url": item.get("official_url"),
                         "state": item["routing_status"], "match": item["matches_metadata"]})

    exact = [item for item in candidates if item["matches_metadata"]]
    runnable = [item for item in exact if item["routing_status"] == "operational"]
    compatible_unvalidated = []
    for item in exact:
        if item["id"] != "TRF1-PJE-1G" or not connector_ids or item["id"] not in connector_ids:
            continue
        validated_sibling = next((candidate for candidate in candidates
            if candidate.get("id") == "TRF1-PJE-JE" and candidate.get("real_validation")
            and candidate.get("integration_state") == "operational"
            and candidate.get("system") == item.get("system")
            and candidate.get("official_url") == item.get("official_url")
            and candidate.get("query_url") == item.get("query_url")), None)
        if validated_sibling:
            compatible_unvalidated.append(item)
    if len(runnable) == 1:
        status, selected = "routed", runnable[0]["id"]
    elif len(runnable) > 1:
        status, selected = "ambiguous", None
    elif len(compatible_unvalidated) == 1:
        status, selected = "routed_compatible_unvalidated", compatible_unvalidated[0]["id"]
        evidence.append({"source": compatible_unvalidated[0]["id"],
                         "basis": "same official URL and query form as validated TRF1 PJe sibling",
                         "validation": "not independently validated for this degree"})
    elif not candidates:
        status, selected = ("system_unconfirmed" if not systems else "no_court_connector"), None
    elif not systems or not grades:
        status, selected = "system_unconfirmed", None
    elif not exact:
        status, selected = "system_not_in_matrix", None
    elif len(exact) > 1:
        status, selected = "ambiguous", None
    else:
        status, selected = exact[0]["routing_status"], None

    limitations = []
    if tribunal_names and any(name != code_alias for name in tribunal_names):
        limitations.append("O identificador do tribunal retornado por fonte diverge do código de origem CNJ; não selecionar conector." )
        status, selected = "tribunal_mismatch", None
    if not alias:
        limitations.append("A documentação consultada do DataJud não publica alias para este tribunal.")
    if not selected and candidates:
        limitations.append("Nenhum conector judicial específico está operacional e validado nesta instalação.")
    return RoutingDecision(number, court_id, tribunal["justice_segment"], alias, datajud_status,
                           sorted(systems), sorted(grades), candidates, selected, status,
                           evidence, limitations).as_dict()


def public_query_url(routing):
    """Return one unique HTTPS query endpoint from the matched court registry.

    If metadata does not identify a supported system/degree, a generic query
    page remains safe when every registered candidate for this CNJ tribunal
    points to the same official URL.
    """
    candidates = routing.get("court_candidates", []) if isinstance(routing, dict) else []
    matched = [item for item in candidates if item.get("matches_metadata") and item.get("query_url")]
    pool = matched or [item for item in candidates if item.get("query_url")]
    urls = set()
    for item in pool:
        url = item.get("query_url")
        if not isinstance(url, str):
            continue
        parsed = urlsplit(url)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
            urls.add(parsed.geturl())
    return next(iter(urls)) if len(urls) == 1 else None
