"""Versioned, auditable lookup for explicit procedural designations.

The bundled role file is a curated recognition vocabulary. It does not claim
to reproduce the complete CNJ class taxonomy or infer a role from a class.
"""
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata
import xml.etree.ElementTree as ET


_PROJECT_ROLES = Path(__file__).resolve().parents[2] / "data" / "legal" / "roles-2026-10-14.json"
_BUNDLED_ROLES = Path(__file__).resolve().parent / "_data" / "legal" / "roles-2026-10-14.json"
DEFAULT_ROLES = _PROJECT_ROLES if _PROJECT_ROLES.is_file() else _BUNDLED_ROLES


def _key(text):
    folded = unicodedata.normalize("NFKD", str(text or ""))
    return " ".join(re.sub(r"[^\w ]", " ", "".join(
        char for char in folded if not unicodedata.combining(char)).casefold()).split())


@lru_cache(maxsize=4)
def load_roles(path=None):
    source = Path(path) if path else DEFAULT_ROLES
    document = json.loads(source.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1 or not document.get("version") or not document.get("reference"):
        raise ValueError("Catálogo de papéis sem versão ou origem.")
    index = {}
    for role in document.get("roles", []):
        if not role.get("id") or role.get("kind") not in {
            "party", "participant", "third_party", "professional", "representative", "court_actor"
        }:
            raise ValueError("Papel sem identificação ou categoria válida.")
        for alias in role.get("aliases", []):
            key = _key(alias)
            if not key or key in index:
                raise ValueError(f"Designação duplicada ou vazia: {alias!r}")
            index[key] = role
    if not index:
        raise ValueError("Catálogo de papéis vazio.")
    return {"version": document["version"], "reference": document["reference"],
            "roles": document["roles"], "index": index}


def explicit_role(text, path=None):
    """Recognize a full source qualification, never a substring or name guess."""
    role = load_roles(path)["index"].get(_key(text))
    if role is None:
        return None
    position = role.get("position")
    pole = role.get("canonical_pole") or ("ativo" if position in {"active", "recourse_active", "incident_active"} else
            "passivo" if position in {"passive", "recourse_passive", "incident_passive"} else
            "terceiro" if position == "third" else None)
    scope = role.get("scope") or (
        "recurso" if str(position or "").startswith("recourse") else
        "incidente" if str(position or "").startswith("incident") else "processo")
    return {**role, "canonical_function": role["id"], "canonical_pole": pole, "scope": scope}


class SgtClassCatalogService:
    """Acquire selected class records directly from the CNJ SGT SOAP contract.

    The resulting export preserves each XML response. A selected-code export
    must never be described as coverage of the complete national class table.
    """

    def __init__(self, client=None):
        from .providers.sgt import SgtClient
        self.client = client or SgtClient()

    def fetch_codes(self, codes):
        from .providers.sgt import URL
        from .sgt_catalog import first, parse_item, parse_details
        from datetime import datetime

        requested = list(dict.fromkeys(str(value).strip() for value in codes))
        version_root, version_raw = self.client.call("getDataUltimaVersao")
        reference = first(version_root, "return")
        if reference is None or not reference.text:
            raise ValueError("SGT não informou a versão das classes.")
        reference_date = datetime.strptime(reference.text.strip(), "%d/%m/%Y").date().isoformat()
        records = []
        for code in requested:
            if not re.fullmatch(r"\d+", code):
                raise ValueError("Código de classe inválido.")
            root, raw = self.client.call("pesquisarItemPublicoWS", tipoTabela="C",
                                         tipoPesquisa="C", valorPesquisa=code)
            item = parse_item(root)
            if not item or item["code"] != code:
                continue
            details, details_raw = self.client.call("getArrayDetalhesItemPublicoWS",
                                                     seqItem=code, tipoItem="C")
            parents, parents_raw = self.client.call("getStringPaisItemPublicoWS",
                                                     seqItem=code, tipoItem="C")
            parent_result = first(parents, "return")
            ancestry = [part.strip() for part in (parent_result.text or "").split(",") if part.strip()] if parent_result is not None else []
            records.append({**item, "details": parse_details(details), "parents": ancestry,
                            "raw": {"search": raw.decode("utf-8"),
                                    "details": details_raw.decode("utf-8"),
                                    "parents": parents_raw.decode("utf-8")}})
        return {"schema_version": 1, "catalog": "classes", "version": "SGT-" + reference_date,
                "reference_date": reference_date, "source": URL + "?wsdl",
                "coverage": {"requested_codes": len(requested),
                             "confirmed_codes": len(records), "national_complete": False},
                "raw_version": version_raw.decode("utf-8"), "items": records}


def validate_class_catalog(document):
    """Reject exports whose labels/codes do not match their original SGT XML."""
    from .providers.sgt import URL
    from .sgt_catalog import parse_item, parse_details, first

    if (document.get("schema_version") != 1 or document.get("catalog") != "classes"
            or document.get("source") != URL + "?wsdl"
            or document.get("version") != "SGT-" + str(document.get("reference_date"))):
        raise ValueError("Catálogo de classes sem contrato e versão reconhecidos.")
    seen = set()
    for item in document.get("items", []):
        try:
            raw = item["raw"]
            observed = parse_item(ET.fromstring(raw["search"]))
            details = parse_details(ET.fromstring(raw["details"]))
            parent = first(ET.fromstring(raw["parents"]), "return")
            ancestry = [part.strip() for part in (parent.text or "").split(",") if part.strip()] if parent is not None else []
        except (KeyError, TypeError, ET.ParseError):
            raise ValueError("Item de classe sem evidência XML verificável.") from None
        if (not observed or observed["code"] in seen
                or any(observed[field] != item.get(field) for field in ("code", "name", "parent_code", "glossary"))
                or details != item.get("details") or ancestry != item.get("parents")):
            raise ValueError("Item de classe diverge do XML oficial preservado.")
        seen.add(observed["code"])
    if len(seen) != document.get("coverage", {}).get("confirmed_codes"):
        raise ValueError("Cobertura declarada difere dos itens verificados.")
    return document


def find_class(document, query):
    validate_class_catalog(document)
    key = _key(query)
    return [item for item in document["items"]
            if item["code"] == str(query) or key and key in _key(item["name"])]
