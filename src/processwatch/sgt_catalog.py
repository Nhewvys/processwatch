"""Import versioned movement definitions from CNJ's live SGT SOAP service."""
from datetime import datetime
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path

from .providers.sgt import SgtClient, URL


def local(tag):
    return tag.rsplit("}", 1)[-1]


def first(root, name):
    return next((element for element in root.iter() if local(element.tag) == name), None)


def child_text(element, name):
    return next((child.text for child in element if local(child.tag) == name), None)


def parse_item(root):
    item = first(root, "Item")
    if item is None:
        return None
    code = child_text(item, "cod_item")
    name = child_text(item, "nome")
    if not code or not name:
        return None
    return {"code": str(code), "name": name,
            "parent_code": child_text(item, "cod_item_pai"),
            "glossary": child_text(item, "dscGlossario")}


def parse_details(root):
    result = {}
    for element in root.iter():
        if local(element.tag) != "item":
            continue
        key = child_text(element, "key")
        value = child_text(element, "value")
        if key:
            result[key] = value
    return result


def parse_complements(root):
    result = []
    for element in root.iter():
        if local(element.tag) != "ComplementoMovimento":
            continue
        code = child_text(element, "seqComplemento")
        name = child_text(element, "dscComplemento")
        if not code or not name:
            continue
        values = {}
        for group in element:
            if local(group.tag) != "arrayValoresTabelados":
                continue
            for item in group:
                value_code = child_text(item, "seqComplementoTabelado")
                value_name = child_text(item, "dscComplementoTabelado")
                if value_code and value_name:
                    values[str(value_code)] = value_name
        result.append({"code": str(code), "name": name,
                       "type_code": child_text(element, "seqTipoComplemento"),
                       "note": child_text(element, "dscObservacao"), "values": values})
    return result


class SgtCatalogService:
    def __init__(self, client=None):
        self.client = client or SgtClient()

    def import_codes(self, repository, codes):
        version_root, _ = self.client.call("getDataUltimaVersao")
        reference = first(version_root, "return")
        if reference is None or not reference.text:
            raise ValueError("SGT não informou a versão.")
        reference_date = datetime.strptime(reference.text.strip(), "%d/%m/%Y").date().isoformat()
        version = "SGT-" + reference_date
        items = {}
        pending = [str(code) for code in codes]
        while pending:
            code = pending.pop(0)
            if code in items:
                continue
            root, search_raw = self.client.call("pesquisarItemPublicoWS", tipoTabela="M",
                                                tipoPesquisa="C", valorPesquisa=code)
            item = parse_item(root)
            if item is None or item["code"] != code:
                # Unknown codes remain unknown; do not import a guessed label.
                continue
            item.update(version=version, source=URL + "?wsdl", parents=[],
                        description=None, complements=[], raw={"search": search_raw.decode("utf-8")})
            items[code] = item
            if item["parent_code"] and item["parent_code"] != code:
                pending.append(item["parent_code"])
            if code not in {str(value) for value in codes}:
                continue
            details, details_raw = self.client.call("getArrayDetalhesItemPublicoWS", seqItem=code, tipoItem="M")
            item["description"] = parse_details(details).get("movimento")
            item["raw"]["details"] = details_raw.decode("utf-8")
            parents, parents_raw = self.client.call("getStringPaisItemPublicoWS", seqItem=code, tipoItem="M")
            parent_result = first(parents, "return")
            item["parents"] = [part for part in (parent_result.text or "").split(",") if part] if parent_result is not None else []
            item["raw"]["parents"] = parents_raw.decode("utf-8")
            pending.extend(item["parents"])
            complements, complements_raw = self.client.call("getComplementoMovimentoWS", codMovimento=code)
            item["complements"] = parse_complements(complements)
            item["raw"]["complements"] = complements_raw.decode("utf-8")
        if not items:
            raise ValueError("Nenhum código TPU confirmado pelo SGT.")
        repository.save_tpu_catalog(version, reference_date, URL + "?wsdl", list(items.values()))
        return version, items


def export_catalog(path, version, items):
    document = {"schema_version": 1, "version": version,
                "reference_date": version.removeprefix("SGT-"), "source": URL + "?wsdl",
                "items": list(items.values())}
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def import_export(repository, path):
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if document.get("schema_version") != 1 or document.get("source") != URL + "?wsdl":
        raise ValueError("Exportação SGT sem origem oficial identificada.")
    version = document["version"]
    reference = document["reference_date"]
    if version != "SGT-" + reference or not document.get("items"):
        raise ValueError("Exportação SGT com versão inválida.")
    for item in document["items"]:
        raw = item.get("raw", {}).get("search")
        if not raw or item.get("version") != version or item.get("source") != URL + "?wsdl":
            raise ValueError("Item SGT sem evidência de origem.")
        import xml.etree.ElementTree as ET
        observed = parse_item(ET.fromstring(raw))
        if observed is None or any(observed[key] != item.get(key) for key in ("code", "name", "parent_code", "glossary")):
            raise ValueError("Item SGT não corresponde ao XML original.")
        details = item["raw"].get("details")
        if details and parse_details(ET.fromstring(details)).get("movimento") != item.get("description"):
            raise ValueError("Detalhamento SGT não corresponde ao XML original.")
        parents = item["raw"].get("parents")
        if parents:
            root = ET.fromstring(parents)
            returned = first(root, "return")
            codes = [part for part in (returned.text or "").split(",") if part] if returned is not None else []
            if codes != item.get("parents"):
                raise ValueError("Hierarquia SGT não corresponde ao XML original.")
        complements = item["raw"].get("complements")
        if complements and parse_complements(ET.fromstring(complements)) != item.get("complements"):
            raise ValueError("Complementos SGT não correspondem ao XML original.")
    repository.save_tpu_catalog(version, reference, document["source"], document["items"])
    return version, len(document["items"])


class _SgtTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def import_official_spreadsheet(repository, path, *, version="SGT-2026-10-06"):
    """Import CNJ's HTML-formatted .xls, retaining its hash and exact row evidence.

    The spreadsheet lists complement templates, not their enumerated values. Existing
    SOAP definitions therefore take precedence and no value is inferred here.
    """
    import re

    source = "https://www.cnj.jus.br/sgt/versoes.php?tipo_tabela=M"
    data = Path(path).read_bytes()
    if not data.startswith(b"<table") or len(data) < 1000:
        raise ValueError("Planilha SGT inválida ou vazia.")
    table = _SgtTable()
    table.feed(data.decode("latin-1"))
    if not table.rows or "Movimentos processuais" not in table.rows[0][0]:
        raise ValueError("Planilha não identificada como movimentos do CNJ.")
    if not version.startswith("SGT-"):
        raise ValueError("Versão da planilha SGT inválida.")
    version_date = version.removeprefix("SGT-")
    try:
        displayed_date = datetime.strptime(version_date, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        raise ValueError("Data da versão SGT inválida.") from None
    if displayed_date not in data.decode("latin-1"):
        raise ValueError("Versão da planilha SGT não corresponde à versão solicitada.")
    digest = hashlib.sha256(data).hexdigest()
    items = []
    seen = set()
    for row in table.rows:
        if len(row) < 9 or not re.fullmatch(r"\d+", row[5]):
            continue
        code = row[5]
        name = next((cell for cell in reversed(row[:5]) if cell), None)
        if not name or code in seen:
            raise ValueError("Código duplicado ou sem nome na planilha SGT.")
        seen.add(code)
        parent = row[6] or None
        if parent and not re.fullmatch(r"\d+", parent):
            raise ValueError("Código pai inválido na planilha SGT.")
        if repository.tpu_item(code, version):
            continue  # Preserve richer SOAP definitions, including verified complements.
        items.append({"version": version, "code": code, "name": name,
                      "parent_code": parent, "parents": [parent] if parent else [],
                      "description": row[13] if len(row) > 13 and row[13] else None,
                      "complements": [], "source": source,
                      "raw": {"spreadsheet_sha256": digest, "row": row,
                              "spreadsheet_name": Path(path).name}})
    if len(seen) < 100:
        raise ValueError("Planilha SGT incompleta: menos de 100 códigos.")
    if items:
        repository.save_tpu_catalog(version, version_date, source, items)
    return version, len(seen), len(items), digest
