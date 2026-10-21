"""Extract structured publication mentions without assuming cover-sheet identity."""
import hashlib
import json
import re
import unicodedata
from functools import lru_cache

from .domain import Participant
from .legal_catalog import explicit_role, load_roles


DJEN_MNI_COMPATIBILITY_REFERENCE = (
    "https://www.cnj.jus.br/wp-content/uploads/2011/05/intercomunicacao-2_0.pdf"
)
DATAJUD_XSD_REFERENCE = "https://www.cnj.jus.br/wp-content/uploads/2021/03/Datajud_XSD_10032021.pdf"


def normalize_pole(source: str | None, field_path: str | None, code):
    """Return a conservative interpretation while retaining source code and basis.

    We could not confirm an enum for ``destinatarios[].polo`` in the current public
    DJEN OpenAPI because its schema fetch was denied. A/P match the older MNI 2.0
    vocabulary; this is exposed only as a compatibility candidate, never as an
    authoritative DJEN classification. Unknown codes (including B) are preserved.
    """
    if code is None or not str(code).strip():
        return {"polo_code_original": None, "polo_normalized": None,
                "polo_mapping_status": "not_provided", "polo_mapping_reference": None,
                "polo_source_field": field_path}
    original = str(code).strip()
    normalized = " ".join(original.casefold().split())
    source_key = " ".join(str(source or "").casefold().split())
    field_key = (field_path or "").replace("$.", "").strip().casefold()
    if (source_key == "djen_publicacao" and field_key == "destinatarios[].polo"
            and original in {"A", "P"}):
        return {"polo_code_original": original,
                "polo_normalized": None,
                "polo_mapping_status": "mni_2_0_compatibility_unconfirmed_for_djen",
                "polo_mapping_reference": DJEN_MNI_COMPATIBILITY_REFERENCE,
                "polo_source_field": field_key}
    if (source_key in {"datajud", "datajud_publica"}
            and field_key in {"polo", "dadosbasicos.polo.polo", "dadosbasicos.polo",
                              "partes[].polo", "partesprocessuais[].polo"}
            and original in {"AT", "PA", "TC"}):
        side = {"AT": "ativo", "PA": "passivo", "TC": "terceiro"}[original]
        return {"polo_code_original": original, "polo_normalized": side,
                "polo_mapping_status": "official_datajud_xsd", "polo_mapping_reference": DATAJUD_XSD_REFERENCE,
                "polo_source_field": field_key}
    # Already-normalized descriptive values from a provider are retained as-is.
    descriptive = {"ativo", "ativa", "polo ativo", "passivo", "passiva", "polo passivo",
                   "terceiro", "terceira", "terceiro interessado"}
    if normalized in descriptive:
        side = "ativo" if normalized in {"ativo", "ativa", "polo ativo"} else (
            "passivo" if normalized in {"passivo", "passiva", "polo passivo"} else "terceiro")
        return {"polo_code_original": original, "polo_normalized": side,
                "polo_mapping_status": "provider_descriptive_value", "polo_mapping_reference": None,
                "polo_source_field": field_key or None}
    return {"polo_code_original": original, "polo_normalized": None,
            "polo_mapping_status": "unmapped_code", "polo_mapping_reference": None,
            "polo_source_field": field_key or None}


def normalized_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value)
    return " ".join(re.sub(r"[^\w ]", " ", "".join(c for c in folded if not unicodedata.combining(c)).casefold()).split())


_PRIVATE_ID = re.compile(r"\s*[-–]?\s*(?:CPF|CNPJ)\s*:\s*[\d./-]+", re.I)
_DOCUMENT = re.compile(r"\b(?P<type>CPF|CNPJ)\s*[:.]?\s*(?P<number>\d[\d./-]{8,20})\b", re.I)


def _document_fields(value):
    match = _DOCUMENT.search(str(value or ""))
    if not match:
        return {}
    kind = match.group("type").upper()
    digits = re.sub(r"\D", "", match.group("number"))
    expected = 11 if kind == "CPF" else 14
    if len(digits) != expected:
        return {}
    return {"document_type": kind.casefold(), "document_number": digits,
            "cpf": digits if kind == "CPF" else None,
            "cnpj": digits if kind == "CNPJ" else None}


def safe_participant_name(value: str, role: str | None = None) -> str:
    """Remove document numbers and an exact duplicated parenthetical qualification."""
    result = _PRIVATE_ID.sub("", str(value or ""))
    # Boundary of the next caption field, not part of the preceding person.
    result = re.split(r"\badvogad[oa]s?\s+do\s*\(?a\)?\b", result, maxsplit=1, flags=re.I)[0]
    if role:
        result = re.sub(r"\s*\(" + re.escape(str(role).strip()) + r"\)(?:\s+.*)?$", "", result, flags=re.I)
    result = re.sub(r"^(?:(?:desembargador(?:a)?|ju[ií]z(?:a)?|ministr[oa])\s+(?:federal\s+)?)", "", result, flags=re.I)
    if normalized_name(str(role or "")) in {"magistrado", "relator", "juiz", "juiza"}:
        result = re.sub(r"^Federal\s+", "", result, flags=re.I)
    return " ".join(result.strip(" -–\t\n").split())


def professional_kind(item, name=None):
    """Professional evidence takes precedence over a source table's party default."""
    kinds = [item.get("kind"), *(item.get("kinds") or []), item.get("procedural_function")]
    for kind in kinds:
        key = normalized_name(str(kind or "")).replace("_", " ")
        match = re.search(r"\b(advogad[oa]s?|lawyer|attorney|procurador(?:a)?|defensor(?:a)?)\b", key)
        if match:
            term = match.group(1)
            return "procurador" if term.startswith("procurador") else "defensor" if term.startswith("defensor") else "advogado"
    if item.get("oab") or re.search(r"\b(?:advogad[oa]s?|advocacia|OAB)\b", str(name or item.get("original_name") or item.get("name") or ""), re.I):
        return "advogado"
    return None


def _redact_evidence(value):
    if isinstance(value, str):
        return _PRIVATE_ID.sub(" [identificador pessoal omitido]", value)
    if isinstance(value, list):
        return [_redact_evidence(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_evidence(item) for key, item in value.items()}
    return value


def normalize_procedural_function(source: str | None, original: str | None):
    """Classify only explicit source qualifications; retain the original wording."""
    if not isinstance(original, str) or not original.strip():
        return {"procedural_function": None, "function_mapping_status": "not_provided",
                "function_code_original": None}
    raw = " ".join(original.strip().split())
    role = explicit_role(raw)
    if role:
        return {"procedural_function": role["canonical_function"],
                "procedural_pole": role["canonical_pole"], "function_scope": role["scope"],
                "function_mapping_status": "explicit_source_text",
                "function_code_original": raw, "role_catalog_version": load_roles()["version"],
                "role_catalog_reference": load_roles()["reference"],
                "position_context": role.get("position")}
    return {"procedural_function": None, "function_mapping_status": "unmapped_source_text",
            "function_code_original": raw}


_ROLE_LINE = re.compile(r"^\s*([\wÀ-ÿ ]{2,60})\s*:\s*([^:;|]{3,180})\s*$", re.U)


@lru_cache(maxsize=1)
def _role_aliases():
    roles = load_roles()["roles"]
    aliases = {}
    for role in roles:
        for alias in role.get("aliases", []):
            aliases[alias.casefold()] = explicit_role(alias)
            ascii_alias = "".join(c for c in unicodedata.normalize("NFKD", alias)
                                  if not unicodedata.combining(c))
            aliases[ascii_alias.casefold()] = explicit_role(alias)
    return sorted(aliases.items(), key=lambda pair: len(pair[0]), reverse=True)


@lru_cache(maxsize=2048)
def _role_pattern(alias, pattern_kind):
    """Compile catalog patterns once; portfolio reads reuse them per publication."""
    escaped = re.escape(alias).replace(r"\ ", r"\s+")
    patterns = {
        "qualified_prefix_colon": rf"^{escaped}\s*:\s*(?P<name>.+)$",
        "qualified_prefix": rf"^{escaped}\s+(?P<name>.+)$",
        "qualified_suffix": rf"^(?P<name>.+?)\s*[-–]\s*{escaped}$",
        "header_label": rf"(?<![\w]){escaped}\s*:\s*",
        "line_prefix": rf"^\s*{escaped}\s+(?::|[-–])?\s*(.+?)\s*$",
        "line_suffix": rf"^\s*(.+?)\s*(?:\(|[-–])\s*{escaped}\s*\)?\s*$",
        "common_notice": (rf"\b(?:intimad[oa]s?|citad[oa]s?|notificad[oa]s?|"
                          rf"ouvid[oa]s?|denunciad[oa]s?|executad[oa]s?)\s+"
                          rf"(?:o|a|os|as)?\s*{escaped}\s+(?P<name>[A-ZÀ-Þ][^\n,;|.{{}}]{{2,150}})"),
        "reverse_lawyer": (rf"(?P<name>[A-ZÀ-Þ][^\n,;|.]{{2,150}}?)\s*,\s*"
                           rf"advogad[oa]\s+(?:do|da|dos|das)\s+{escaped}\b"),
        "named_lawyer": (rf"\badvogad[oa]\s+(?:do|da|dos|das)(?:\(a\))?\s+{escaped}\s*:\s*"
                         r"(?P<name>[^\n,;|.]{3,150})"),
    }
    return re.compile(patterns[pattern_kind], re.I)


@lru_cache(maxsize=1)
def _combined_text_role_patterns():
    """Compile one bounded recognizer per prose pattern, instead of one per role."""
    alternatives = []
    for alias, _ in _role_aliases():
        escaped = re.escape(alias).replace(r"\ ", r"\s+")
        if escaped not in alternatives:
            alternatives.append(escaped)
    role = "(?P<role>" + "|".join(alternatives) + ")"
    return {
        "common_notice": re.compile(
            rf"\b(?:intimad[oa]s?|citad[oa]s?|notificad[oa]s?|ouvid[oa]s?|"
            rf"denunciad[oa]s?|executad[oa]s?)\s+(?:o|a|os|as)?\s*{role}\s+"
            r"(?P<name>[A-ZÀ-Þ][^\n,;|.{}]{2,150})", re.I),
        "reverse_lawyer": re.compile(
            rf"(?P<name>[A-ZÀ-Þ][^\n,;|.]{{2,150}}?),\s*advogad[oa]\s+"
            rf"(?:do|da|dos|das)\s+{role}\b", re.I),
        "named_lawyer": re.compile(
            rf"\badvogad[oa]\s+(?:do|da|dos|das)(?:\(a\))?\s+{role}\s*:\s*"
            r"(?P<name>[^\n,;|.]{3,150})", re.I),
    }


def extract_qualified_name(value):
    """Split a participant label only when a full, catalogued role is explicit."""
    text = " ".join(str(value or "").split())
    for match in re.finditer(r"\(([^()]*)\)", text):
        role = explicit_role(match.group(1))
        if role:
            return safe_participant_name(text, match.group(1)), match.group(1).strip()
    for alias, role in _role_aliases():
        prefix = _role_pattern(alias, "qualified_prefix_colon").match(text)
        if not prefix:
            prefix = _role_pattern(alias, "qualified_prefix").match(text)
        suffix = _role_pattern(alias, "qualified_suffix").match(text)
        found = prefix or suffix
        if found and _person_text(found.group("name")):
            return _person_text(found.group("name")), role["id"]
    return text, None


def _role_kind(role):
    return {"party": "parte", "third_party": "terceiro", "professional": role["id"],
            "representative": "representante", "court_actor": "ator_judicial",
            "participant": "participante"}.get(role.get("kind"), "participante")


def _person_text(value):
    """Keep a bounded person fragment; never treat the rest of a paragraph as a name."""
    value = re.split(r"[\n\r;,|]", str(value or ""), maxsplit=1)[0]
    value = re.split(r"\s+(?=(?:AUTOR(?:A)?|R[ÉE]U|REU|AGRAVANTE|AGRAVADO|APELANTE|APELADO|EMBARGANTE|EMBARGADO)\s*:)",
                     value, maxsplit=1, flags=re.I)[0]
    value = re.split(r"\b(?:ATO\s+ORDIN|INTIMA|DESPACHO|DECIS|SENTEN|EDITAL)",
                     value, maxsplit=1, flags=re.I)[0]
    value = re.sub(r"\s*[-–]\s*(?:OAB\s*)?(?:[A-Z]{2}\s*)?\d{4,}.*$", "", value, flags=re.I)
    value = re.sub(r"\s+\(OAB\s*[/:-]?.*?$", "", value, flags=re.I)
    value = re.sub(r"\s*[-–]\s*(?:[A-Z]{2}\s*)?\d{4,}(?:\s*/\s*[A-Z]{2})?\s*$", "", value)
    value = _PRIVATE_ID.sub("", value)
    return " ".join(value.strip(" \t:–-.").split())[:180]


def _make_text_role(name, role, source_id, excerpt, *, relation=None):
    name = safe_participant_name(_person_text(name))
    if (not role or not name or not re.search(r"[A-Za-zÀ-ÿ]{2}", name)
            or re.search(r"\b(?:não informado|nao informado|desconhecido|ignorado|nenhum)\b", name, re.I)
            or "@" in name or "http" in name.casefold()
            or re.search(r"[?!]|\b(?:colheu dados|procurou determinar|dever[aá]|examinou documenta|"
                         r"responda aos quesitos|concorda com o resultado|qual [ée]|o\s*\(?a\)?\s*periciando)\b",
                         name, re.I)):
        return None
    kind = _role_kind(role)
    if relation:
        kind = "advogado"
    function = role["canonical_function"] if not relation else "advogado"
    evidence = {"source_id": source_id, "source_field": "text_plain",
                "source_excerpt": str(excerpt).strip()[:500], "method": "explicit_role_text_v2"}
    relationships = ([{"type": "represents_role", "role": relation["canonical_function"],
                        "scope": relation["scope"], "status": "explicit_text_candidate",
                        "source_id": source_id, "evidence": str(excerpt).strip()[:500]}]
                     if relation else [])
    registration = re.search(r"\b(?:OAB\s*/?\s*)?(?P<uf>[A-Z]{2})\s*(?P<number>\d{4,7})\b",
                             str(excerpt), re.I)
    professional_id = ({"oab": registration.group("number"), "uf": registration.group("uf").upper()}
                       if relation and registration else {})
    return {"name": name, "original_name": name, "kind": kind,
            "role": "ADVOGADO" if relation else role.get("id"),
            "role_original": "ADVOGADO" if relation else str(role.get("id")),
            "procedural_function": function,
            "procedural_pole": None if relation else role.get("canonical_pole"),
            "function_scope": "processo" if relation else role.get("scope"),
            "function_mapping_status": "explicit_communication_line",
            "role_source_field": "text_plain:explicit_role_pattern",
            "source_text_original": str(excerpt).strip()[:500], "source": "DJEN_PUBLICACAO",
            "position_scope": "recurso" if (not relation and role.get("scope") == "recurso") else
                              "incidente" if (not relation and role.get("scope") == "incidente") else "processo consultado",
            "evidence_id": source_id, "evidence_ids": [source_id],
            "evidence": evidence, "relationships": relationships,
            **_document_fields(excerpt), **professional_id}


def explicit_text_roles(text, source_id):
    """Extract only explicit legal designations from labels, parentheses and phrases."""
    found = []
    text = str(text or "")

    # DJEN bodies often embed the court caption in one dense line instead of
    # separate JSON fields: "PROCESSO: ... AUTOR: X Advogado do(a) AUTOR: Y
    # REU: Z ATO ORDINATÓRIO". Treat only labels in that caption as parties;
    # the role repeated inside "Advogado do(a) AUTOR" labels the lawyer's
    # relationship and must not swallow the lawyer's name as the author's.
    case_match = re.search(r"\bPROCESSO\s*:\s*\d{7}-\d{2}\.\d{4}\.\d\.\d{2}\.\d{4}", text, re.I)
    if case_match:
        header = text[case_match.end():case_match.end() + 5000]
        act = re.search(r"\b(?:ATO\s+ORDIN|INTIMA|DESPACHO|DECIS|SENTEN|EDITAL)",
                        header, re.I)
        if act:
            header = header[:act.start()]
        labels = {}
        for match in re.finditer(r"(?=(?<![\w])(?P<label>[^:;|\n]{1,80})\s*:\s*)", header):
            role = explicit_role(match.group("label").strip())
            if not role or role.get("kind") not in {"party", "third_party"}:
                continue
            prefix = header[max(0, match.start() - 40):match.start()]
            if re.search(r"advogad[oa]\s+do\s*\(?a\)?\s*$", prefix, re.I):
                continue
            colon = header.find(":", match.start() + len(match.group("label")))
            value_start = colon + 1
            while value_start < len(header) and header[value_start].isspace():
                value_start += 1
            labels[(match.start(), role["id"])] = (match.start(), value_start, role)
        ordered_labels = sorted(labels.values(), key=lambda item: item[0])
        for index, (start, value_start, role) in enumerate(ordered_labels):
            end = ordered_labels[index + 1][0] if index + 1 < len(ordered_labels) else len(header)
            next_lawyer = re.search(r"\bADVOGAD[OA]S?\b", header[value_start:end], re.I)
            if next_lawyer:
                end = value_start + next_lawyer.start()
            raw_name = header[value_start:end].strip(" \t\r\n:;,-–")
            raw_name = re.sub(r"\s*\(\s*(?:AUTOR|AUTORA|R[ÉE]U)\s*\)\s*$", "", raw_name, flags=re.I)
            raw_name = raw_name[:250]
            row = _make_text_role(raw_name, role, source_id, header[start:end])
            if row:
                found.append(row)

    for line in re.split(r"[\n\r]+|(?<=[.;])\s+(?=[A-ZÀ-Þ])", text):
        match = _ROLE_LINE.fullmatch(line)
        if match:
            role = explicit_role(match.group(1))
            if role:
                row = _make_text_role(match.group(2), role, source_id, line)
                if row:
                    found.append(row)

    # Header/suffix designations are parsed by checking only short, structured
    # lines. Arbitrary publication bodies are never scanned once per catalog role.
    for line in re.split(r"[\n\r]+|(?<=[.;])\s+(?=[A-ZÀ-Þ])", text):
        if not line or len(line) > 500:
            continue
        parenthetical = list(re.finditer(r"\(([^()]*)\)", line))
        if parenthetical:
            match = parenthetical[-1]
            role = explicit_role(match.group(1).strip())
            if role:
                row = _make_text_role(line[:match.start()], role, source_id, line)
                if row:
                    found.append(row)
        # Recognize "PAPEL Nome" without colon using at most eight leading
        # tokens; explicit_role performs exact catalog lookup, never fuzzy match.
        # Colon-delimited labels use _ROLE_LINE above, whose delimiters reject
        # multi-party values such as "REU: Empresa A; Empresa B". Keep the
        # short-token recognizer for unambiguous "PAPEL Nome" forms only.
        if ":" not in line:
            token_matches = list(re.finditer(r"[^\s]+", line))
            for count in range(1, min(8, len(token_matches)) + 1):
                end = token_matches[count - 1].end()
                role = explicit_role(line[:end].strip(" :–-").strip())
                if role:
                    raw_name = line[end:].strip(" :–-")
                    if role.get("kind") == "professional" and re.match(r"^(?:do|da|dos|das|de|para)\b", raw_name, re.I):
                        break
                    row = _make_text_role(raw_name, role, source_id, line)
                    if row:
                        found.append(row)
                    break
        if "-" in line or "–" in line:
            left, separator, right = max(
                ((line.rpartition(char)[0], char, line.rpartition(char)[2]) for char in "-–"),
                key=lambda item: len(item[0]))
            role = explicit_role(right.strip()) if separator else None
            if role:
                row = _make_text_role(left, role, source_id, line)
                if row:
                    found.append(row)

    patterns = _combined_text_role_patterns()
    for match in patterns["common_notice"].finditer(text):
        role = explicit_role(match.group("role"))
        row = _make_text_role(match.group("name"), role, source_id, match.group(0))
        if row:
            found.append(row)
    for match in patterns["reverse_lawyer"].finditer(text):
        role = explicit_role(match.group("role"))
        row = _make_text_role(match.group("name"), role, source_id, match.group(0), relation=role)
        if row:
            found.append(row)
    for match in patterns["named_lawyer"].finditer(text):
        role = explicit_role(match.group("role"))
        row = _make_text_role(match.group("name"), role, source_id, match.group(0), relation=role)
        if row:
            found.append(row)

    unique = {}
    for row in found:
        key = (normalized_name(row["original_name"]), row.get("kind"),
               row.get("procedural_function"), json.dumps(row.get("relationships", []), sort_keys=True))
        if key not in unique:
            unique[key] = row
    return list(unique.values())


def mentions(number: str, source_id: str, parties: list[dict], lawyers: list[dict], recipients: list[dict]):
    result = []
    records = [("parte", item) for item in parties] + [("advogado", item) for item in lawyers]
    records += [("destinatário", item) for item in recipients]
    for kind, item in records:
        name = item.get("nome")
        if not isinstance(name, str) or not name.strip():
            continue
        role = item.get("papel") or item.get("tipo")
        oab = item.get("oab")
        uf = item.get("uf")
        kind = professional_kind({**item, "kind": kind}, name) or kind
        source = item.get("source") or "DJEN_PUBLICACAO"
        field_path = item.get("polo_source_field") or (
            "destinatarios[].polo" if kind == "destinatário" and "polo" in item else None)
        pole = normalize_pole(source, field_path, item.get("polo"))
        function = normalize_procedural_function(source, role)
        source_kind = kind
        normalized_kind = ("parte" if kind == "destinatário" and
            function.get("procedural_function") and explicit_role(role or "") and
            explicit_role(role or "").get("kind") == "party" else kind)
        key = "|".join([number, source_id, kind, name, str(role or ""), str(oab or ""), str(uf or "")])
        documents = _document_fields(name)
        documents.update({key: item.get(key) for key in
                          ("document_type", "document_number", "cpf", "cnpj") if item.get(key)})
        result.append(Participant(participant_id=hashlib.sha256(key.encode()).hexdigest(), number=number,
                                  original_name=name, normalized_name=normalized_name(name), kind=normalized_kind,
                                  role=role, role_original=role,
                                  procedural_function=function["procedural_function"],
                                  procedural_pole=function.get("procedural_pole"),
                                  function_scope=function.get("function_scope"),
                                  function_mapping_status=function["function_mapping_status"],
                                  function_code_original=function["function_code_original"],
                                  role_source_field=item.get("role_source_field"),
                                  source_record_id=str(item.get("source_record_id")) if item.get("source_record_id") else None,
                                  source_identity_id=str(item.get("source_identity_id")) if item.get("source_identity_id") else None,
                                  identity_type=item.get("identity_type") or "unconfirmed",
                                  identity_status="confirmed_oab_uf" if oab and uf else "unresolved",
                                  relationships=list(item.get("relationships") or []),
                                  evidence={"source_id": source_id, "source_field": field_path,
                                            "source_record_id": item.get("source_record_id")},
                                  role_code_original=item.get("papel") or item.get("tipo"),
                                  polo=item.get("polo"), **pole,
                                  communication_role=("destinatário da comunicação" if source_kind == "destinatário"
                                                      else "advogado destinatário" if source_kind == "advogado" else None),
                                  oab=oab, uf=uf, source=source, evidence_id=source_id, **documents))
    return result


def canonical_participants(repository, org: str, number: str, *, court_snapshots=(), metadata_records=()):
    """Build the shared, provenance-preserving people view used by list and details.

    Publication recipients remain recipients unless a separate explicit role or
    a PJe cover-sheet record establishes that they are also parties. Repeated
    exact-name mentions are consolidated within the same process and category;
    conflicting stable identifiers remain separate and are exposed as candidates.
    """
    existing = list(repository.participants(org, number))
    records = []

    def add(item, source_default, *, forced_kind=None, source_id=None):
        if not isinstance(item, dict):
            return
        raw_name = (item.get("name_original") or item.get("original_name") or item.get("name")
                    or item.get("nome"))
        if not isinstance(raw_name, str) or not raw_name.strip():
            return
        role = item.get("role_original") or item.get("role") or item.get("papel")
        if not role:
            for qualified in re.findall(r"\(([^()]*)\)", raw_name):
                if explicit_role(qualified):
                    role = qualified
                    break
        name = safe_participant_name(raw_name, role)
        if not name:
            return
        documents = {**_document_fields(raw_name), **{key: item.get(key) for key in
            ("document_type", "document_number", "cpf", "cnpj") if item.get(key)}}
        identity_type = item.get("identity_type") or (
            "pessoa_juridica_documento_presente" if documents.get("cnpj") else
            "pessoa_fisica_documento_presente" if documents.get("cpf") else "unconfirmed")
        kind = item.get("kind") or item.get("tipo") or forced_kind or "participante"
        kind_key = normalized_name(str(kind)).replace(" ", "_")
        kind = {"destinatario": "destinatário", "recipient": "destinatário",
                "lawyer": "advogado", "attorney": "advogado", "party": "parte"}.get(kind_key, kind)
        source = item.get("source") or source_default
        role_mapping = normalize_procedural_function(source, role)
        function = item.get("procedural_function") or role_mapping["procedural_function"]
        procedural_pole = role_mapping.get("procedural_pole") or item.get("procedural_pole")
        function_scope = item.get("function_scope") or role_mapping.get("function_scope")
        special_kind = {"advogado": "advogado", "procurador": "procurador", "defensor": "defensor",
                        "perito": "perito", "assistente técnico": "assistente_tecnico",
                        "relator": "ator_judicial", "magistrado": "ator_judicial",
                        "representante legal": "representante", "assistente": "assistente",
                        "terceiro interessado": "terceiro", "terceiro": "terceiro",
                        "investigado": "participante", "vítima": "participante",
                        "paciente": "participante", "autoridade coatora": "participante"}.get(function)
        if special_kind and (not item.get("kind") or item.get("kind") == "parte"):
            kind = special_kind
        raw_polo = item.get("polo_code_original")
        if raw_polo is None:
            raw_polo = item.get("polo") or item.get("side")
        pole = normalize_pole(source, item.get("polo_source_field"), raw_polo)
        # Existing DJEN participant payloads predate the explicit field metadata.
        if (pole["polo_mapping_status"] == "unmapped_code" and source == "DJEN_PUBLICACAO"
                and kind == "destinatário" and str(raw_polo or "").upper() in {"A", "P"}):
            pole = normalize_pole(source, "destinatarios[].polo", raw_polo)
        communication_role = item.get("communication_role") or (
            "destinatário da comunicação" if kind == "destinatário" and source == "DJEN_PUBLICACAO"
            else "advogado destinatário" if kind == "advogado" and source == "DJEN_PUBLICACAO" else None)
        oab = item.get("oab")
        uf = item.get("uf")
        professional = professional_kind({**item, "kind": kind, "procedural_function": function}, name)
        if professional:
            kind, function, procedural_pole = professional, professional, None
        evidence = list(item.get("evidence_ids") or [])
        if item.get("evidence_id") and item["evidence_id"] not in evidence:
            evidence.append(item["evidence_id"])
        if source_id and source_id not in evidence:
            evidence.append(source_id)
        original_evidence = _redact_evidence(item.get("evidence") or {})
        if not isinstance(original_evidence, dict):
            # Historical snapshots may store evidence as text/list. Keep its
            # shape and contents under a named field instead of raising during
            # API consolidation or silently dropping it.
            original_evidence = {"source_value": original_evidence}
        participant_id = item.get("participant_id")
        if not participant_id:
            identity = "|".join(str(value or "") for value in (
                number, source, kind, normalized_name(name), role, raw_polo, oab, uf,
                ",".join(sorted(map(str, evidence)))))
            participant_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        kinds = set(item.get("kinds") or [kind])
        if professional:
            kinds = {professional}
        if communication_role and "destinatário" in communication_role.casefold():
            kinds.add("destinatário")
        if item.get("kind") == "parte" and kind in {"terceiro", "assistente", "representante"}:
            kinds.add("parte")
        canonical_pole = pole.get("polo_normalized") or procedural_pole
        if professional:
            canonical_pole = None
            pole["polo_normalized"] = None
        records.append({
            **item, "participant_id": str(participant_id),
            "original_name": name.strip(), "normalized_name": normalized_name(name),
            "source_text_original": _redact_evidence(item.get("source_text_original")),
            "evidence": _redact_evidence(item.get("evidence") or {}),
            "kind": kind, "kinds": sorted(kinds | {kind}), "role": role,
            "role_original": item.get("role_original") or role,
            "procedural_function": function,
            "procedural_pole": procedural_pole,
            "function_scope": function_scope,
            "function_mapping_status": item.get("function_mapping_status") or
                role_mapping["function_mapping_status"],
            "role_catalog_version": item.get("role_catalog_version") or role_mapping.get("role_catalog_version"),
            "role_catalog_reference": item.get("role_catalog_reference") or role_mapping.get("role_catalog_reference"),
            "position_scope": item.get("position_scope"),
            "identity_type": identity_type,
            **documents,
            "identity_status": ("confirmed_source_id" if item.get("source_identity_id") else
                item.get("identity_status") or ("confirmed_oab_uf" if oab and uf else "unresolved")),
            "relationships": list(item.get("relationships") or []),
            "evidence": original_evidence,
            "roles": list(item.get("roles") or ([role] if role else [])), "polo": canonical_pole or raw_polo,
            "polo_provider_original": raw_polo,
            **pole,
            "polo_normalized": pole.get("polo_normalized") or procedural_pole,
            "polo_mapping_status": (pole.get("polo_mapping_status") if pole.get("polo_normalized")
                                     else "explicit_role_catalog" if procedural_pole else pole.get("polo_mapping_status")),
            "communication_role": communication_role,
            "communication_roles": list(item.get("communication_roles") or ([communication_role] if communication_role else [])),
            "role_code_original": item.get("role_code_original") or role,
            "source_identity_id": item.get("source_identity_id"),
            "oab": oab, "uf": uf, "source": source,
            "sources": list(item.get("sources") or [source]), "evidence_ids": evidence,
            "identity_confirmed": bool(item.get("identity_confirmed") or (oab and uf)),
        })

    # Older DJEN payloads already contain their full publication text. Interpret
    # only explicit standalone qualifications and keep the exact line as evidence.
    # Within one publication, a unique structured recipient with the identical
    # normalized name supplies an additional identity link; names across separate
    # publications never become an identity key.
    text_mentions = []
    if hasattr(repository, "communications"):
        for communication in repository.communications(org, number):
            source_id = str(communication.get("source_id") or "")
            for mention in explicit_text_roles(communication.get("text_plain"), source_id):
                same_publication = [person for person in existing
                    if person.get("normalized_name") == normalized_name(mention["name"])
                    and source_id in (person.get("evidence_ids") or [person.get("evidence_id")])]
                if len(same_publication) == 1:
                    recipient = same_publication[0]
                    mention["participant_id"] = recipient.get("participant_id")
                    mention["source_identity_id"] = recipient.get("source_identity_id")
                    mention["oab"], mention["uf"] = recipient.get("oab"), recipient.get("uf")
                    # The publication itself links this exact recipient record
                    # to an explicit legal role; preserve the recipient function
                    # as well as the newly evidenced party role.
                    mention["same_publication_identity_category"] = "parte"
                    recipient["same_publication_identity_category"] = "parte"
                    for document_field in ("document_type", "document_number", "cpf", "cnpj"):
                        if recipient.get(document_field):
                            mention[document_field] = recipient[document_field]
                text_mentions.append((mention, source_id))
    for person in existing:
        add(person, person.get("source") or "DJEN_PUBLICACAO")
    for mention, source_id in text_mentions:
        add(mention, "DJEN_PUBLICACAO", source_id=source_id)

    for snapshot in court_snapshots or ():
        data = snapshot.get("data", snapshot) if isinstance(snapshot, dict) else {}
        snapshot_source = snapshot.get("source") if isinstance(snapshot, dict) else None
        for person in data.get("participants", []) or []:
            add({**person, "source_snapshot_id": snapshot.get("source_id") if isinstance(snapshot, dict) else None,
                 "source_snapshot_collected_at": snapshot.get("collected_at") if isinstance(snapshot, dict) else None},
                snapshot_source or "PJe/TRF1")
    for metadata in metadata_records or ():
        if not isinstance(metadata, dict):
            continue
        parties = metadata.get("partes") or metadata.get("partesProcessuais") or []
        if isinstance(parties, dict):
            parties = list(parties.values())
        for person in parties if isinstance(parties, list) else ():
            add(person, "DataJud", forced_kind="parte")
        lawyers = metadata.get("advogados") or []
        if isinstance(lawyers, dict):
            lawyers = list(lawyers.values())
        for person in lawyers if isinstance(lawyers, list) else ():
            add(person, "DataJud", forced_kind="advogado")

    # PJe cover-sheet evidence is the display authority when a strong identity
    # key permits a merge. A name match alone remains a candidate and is never
    # used to fuse a PJe identity with a DJEN/DataJud identity.
    def source_rank(item):
        source = str(item.get("source") or "").casefold()
        if source == "pje/trf1": return 0
        if source == "djen_publicacao" and "parte" in (item.get("kinds") or [item.get("kind")]): return 1
        if item.get("function_mapping_status") == "explicit_communication_line": return 2
        if source == "datajud": return 4
        if "destinat" in source or "destinatário" in (item.get("kinds") or [item.get("kind")]): return 5
        return 3
    records.sort(key=lambda item: (source_rank(item), item.get("normalized_name") or "",
                                   item.get("participant_id") or ""))

    # Strong identifiers are decisive. Within one process and source/category,
    # exact normalized names consolidate repeated mentions as a probable
    # identity. Cross-source name matches remain candidates, never automatic
    # identity merges.
    groups, result = {}, []
    for item in records:
        source_list = sorted(set(item.get("sources") or [item.get("source") or "fonte não identificada"]))
        oab = re.sub(r"\W", "", str(item.get("oab") or "")).upper()
        uf = str(item.get("uf") or "").strip().upper()
        document_type = str(item.get("document_type") or
                            ("cpf" if item.get("cpf") else "cnpj" if item.get("cnpj") else "")).casefold()
        document_number = re.sub(r"\D", "", str(item.get("document_number") or
            item.get("cpf") or item.get("cnpj") or ""))
        evidence_key = tuple(sorted(map(str, item.get("evidence_ids") or [])))
        source_identity_id = str(item.get("source_identity_id") or "").strip()
        if document_number and document_type in {"cpf", "cnpj"}:
            key = ("document", document_type, document_number)
        elif oab and uf:
            key = ("oab-name", oab, uf, item.get("normalized_name"))
        elif source_identity_id:
            # A reused source ID with a different name is a source-data conflict,
            # not permission to merge two differently named identities.
            key = ("source-id", str(item.get("source") or "").casefold(),
                   source_identity_id, item.get("normalized_name"))
        elif item.get("participant_id"):
            # Repository identity groups are stable across the publications that
            # contributed evidence. This also lets an explicit role line in one
            # publication enrich that same person without creating a second card.
            if item.get("source") == "DJEN_PUBLICACAO" and item.get("normalized_name"):
                category = "advogado" if "advogado" in item.get("kinds", []) else (
                    "parte" if "parte" in item.get("kinds", []) else "destinatário")
                if item.get("same_publication_identity_category") in {"parte", "advogado", "destinatário"}:
                    category = item["same_publication_identity_category"]
                unmapped_code = (str(item.get("polo_code_original") or item.get("polo") or "").strip().upper()
                                 if item.get("polo_mapping_status") == "unmapped_code" else "")
                key = ("case-name-kind", category, unmapped_code, item.get("normalized_name"))
            else:
                key = ("participant-id", str(item.get("participant_id")))
        else:
            key = ("source-evidence", str(item.get("source") or "").casefold(), evidence_key,
                   str(item.get("participant_id") or ""))
        current = groups.get(key)
        if current is None:
            current = {**item, "sources": source_list,
                       "kinds": sorted(set(item.get("kinds") or [item.get("kind") or "participante"])),
                       "roles": sorted(set(item.get("roles") or ([item["role"]] if item.get("role") else [])), key=str.casefold),
                       "procedural_functions": sorted(set(item.get("procedural_functions") or
                           ([item["procedural_function"]] if item.get("procedural_function") else [])), key=str.casefold),
                       "function_evidence": ([{"source": item.get("source"),
                           "field": item.get("role_source_field"), "original": item.get("role_original"),
                           "normalized": item.get("procedural_function"),
                           "status": item.get("function_mapping_status"),
                           "evidence_ids": list(item.get("evidence_ids") or [])}]
                           if item.get("role_original") else []),
                       "evidence_ids": sorted(set(item.get("evidence_ids") or [])),
                       "polo_codes": [item["polo_code_original"]] if item.get("polo_code_original") else [],
                       "polo_evidence": ([{"source": item.get("source"),
                           "field": item.get("polo_source_field"),
                           "code": item.get("polo_code_original"),
                           "normalized_candidate": item.get("polo_normalized"),
                           "mapping_status": item.get("polo_mapping_status"),
                           "reference": item.get("polo_mapping_reference"),
                           "evidence_ids": list(item.get("evidence_ids") or [])}]
                           if item.get("polo_code_original") else []),
                       "communication_roles": sorted(set(item.get("communication_roles") or []), key=str.casefold),
                       "mention_count": int(item.get("mention_count") or 1),
                       "identity_confirmed": bool(item.get("identity_confirmed") or source_identity_id or (oab and uf)
                                                   or document_number),
                       "identity_status": ("confirmed_oab_uf" if oab and uf else
                           "confirmed_source_id" if source_identity_id else
                           "confirmed_document" if document_number else
                           "probable_case_name_kind" if str(key[0]).startswith("case-name")
                           else item.get("identity_status") or "unresolved"),
                       "role_conflict": False, "function_conflict": False, "polo_conflict": False,
                       "relationships": list(item.get("relationships") or [])}
            current["source_snapshots"] = ([{"source": item.get("source"), "source_id": item.get("source_snapshot_id"),
                "collected_at": item.get("source_snapshot_collected_at")}]
                if item.get("source_snapshot_id") else [])
            groups[key] = current
            result.append(current)
        else:
            current["sources"] = sorted(set(current["sources"] + source_list))
            current["kinds"] = sorted(set(current["kinds"] + (item.get("kinds") or [item.get("kind") or "participante"])))
            current["roles"] = sorted(set(current["roles"] + (item.get("roles") or ([item["role"]] if item.get("role") else []))), key=str.casefold)
            current["procedural_functions"] = sorted(set(current["procedural_functions"] +
                (item.get("procedural_functions") or ([item["procedural_function"]]
                 if item.get("procedural_function") else []))), key=str.casefold)
            if item.get("role_original"):
                function_item = {"source": item.get("source"), "field": item.get("role_source_field"),
                    "original": item.get("role_original"), "normalized": item.get("procedural_function"),
                    "status": item.get("function_mapping_status"), "evidence_ids": list(item.get("evidence_ids") or [])}
                if function_item not in current["function_evidence"]:
                    current["function_evidence"].append(function_item)
            current["relationships"] = current["relationships"] + [rel for rel in item.get("relationships", [])
                if rel not in current["relationships"]]
            snapshot_evidence = {"source": item.get("source"), "source_id": item.get("source_snapshot_id"),
                                 "collected_at": item.get("source_snapshot_collected_at")}
            if snapshot_evidence["source_id"] and snapshot_evidence not in current["source_snapshots"]:
                current["source_snapshots"].append(snapshot_evidence)
            current["evidence_ids"] = sorted(set(current["evidence_ids"] + (item.get("evidence_ids") or [])))
            for field in ("cpf", "cnpj", "document_number", "document_type", "oab", "uf"):
                if not current.get(field) and item.get(field):
                    current[field] = item[field]
            if not current.get("procedural_pole") and item.get("procedural_pole"):
                current["procedural_pole"] = item["procedural_pole"]
            if not current.get("function_scope") and item.get("function_scope"):
                current["function_scope"] = item["function_scope"]
            if item.get("polo_code_original"):
                current["polo_codes"] = sorted(set(current["polo_codes"] + [item["polo_code_original"]]))
                candidate = {"source": item.get("source"), "field": item.get("polo_source_field"),
                             "code": item.get("polo_code_original"),
                             "normalized_candidate": item.get("polo_normalized"),
                             "mapping_status": item.get("polo_mapping_status"),
                             "reference": item.get("polo_mapping_reference"),
                             "evidence_ids": list(item.get("evidence_ids") or [])}
                if candidate not in current["polo_evidence"]:
                    current["polo_evidence"].append(candidate)
            current["communication_roles"] = sorted(set(current["communication_roles"] +
                (item.get("communication_roles") or [])), key=str.casefold)
            current["communication_role"] = (current["communication_roles"][0]
                if len(current["communication_roles"]) == 1 else None)
            current["mention_count"] += int(item.get("mention_count") or 1)
            known_roles = set(filter(None, current.get("roles", [])))
            current["role_conflict"] = len(known_roles) > 1
            current["role"] = next(iter(known_roles)) if len(known_roles) == 1 else None
            current["function_conflict"] = len(current["procedural_functions"]) > 1
            current["procedural_function"] = (current["procedural_functions"][0]
                if len(current["procedural_functions"]) == 1 else None)
            if current["procedural_function"]:
                function_evidence = [entry for entry in current["function_evidence"]
                                     if entry.get("normalized") == current["procedural_function"]]
                if any(entry.get("status") == "explicit_communication_line" for entry in function_evidence):
                    current["function_mapping_status"] = "explicit_communication_line"
                elif any(entry.get("status") == "explicit_source_text" for entry in function_evidence):
                    current["function_mapping_status"] = "explicit_source_text"
                if not current.get("role_original") and len(current["roles"]) == 1:
                    current["role_original"] = current["roles"][0]
            current["role_catalog_version"] = current.get("role_catalog_version") or item.get("role_catalog_version")
            current["role_catalog_reference"] = current.get("role_catalog_reference") or item.get("role_catalog_reference")
            candidates = {entry.get("normalized_candidate") for entry in current["polo_evidence"]
                          if entry.get("normalized_candidate")}
            # An uninterpreted code cannot contradict a confirmed position.
            # Keep it in the evidence list without manufacturing a conflict.
            current["polo_conflict"] = len(candidates) > 1
            current["polo_normalized"] = (next(iter(candidates)) if len(candidates) == 1
                                          and not current["polo_conflict"] else None)
            raw_codes = set(current["polo_codes"])
            current["polo"] = next(iter(raw_codes)) if len(raw_codes) == 1 else None
            if not current["polo_conflict"] and current["polo_normalized"]:
                statuses = {entry.get("mapping_status") for entry in current["polo_evidence"]
                            if entry.get("normalized_candidate") == current["polo_normalized"]}
                current["polo_mapping_status"] = next(iter(statuses)) if len(statuses) == 1 else "mixed_evidence"
                evidence = next((entry for entry in current["polo_evidence"]
                                 if entry.get("normalized_candidate") == current["polo_normalized"]), {})
                current["polo_mapping_reference"] = evidence.get("reference")
                current["polo_source_field"] = evidence.get("field")
            elif current["polo_conflict"]:
                current["polo_mapping_status"] = "conflicting_source_evidence"
                current["polo_mapping_reference"] = None
                current["polo_source_field"] = None
    # Exact-name and OAB/UF collisions are exposed as candidates, never fused.
    name_groups = {}
    registration_groups = {}
    source_identity_groups = {}
    for item in result:
        name_groups.setdefault(item.get("normalized_name"), []).append(item)
        oab = re.sub(r"\W", "", str(item.get("oab") or "")).upper()
        uf = str(item.get("uf") or "").strip().upper()
        if oab and uf:
            registration_groups.setdefault((oab, uf), []).append(item)
        source_identity_id = str(item.get("source_identity_id") or "").strip()
        source_name = str(item.get("source") or "").casefold()
        if source_identity_id:
            source_identity_groups.setdefault((source_name, source_identity_id), []).append(item)
    for same_name in name_groups.values():
        if len(same_name) < 2:
            continue
        for item in same_name:
            # Matching names are unresolved candidates even within one provider:
            # repeated mentions may be the same person or homonyms. Preserve both
            # identities and their evidence instead of silently choosing.
            candidates = [other["participant_id"] for other in same_name if other is not item]
            if candidates:
                item["identity_status"] = "possible_match_requires_confirmation"
                item["identity_candidates"] = sorted(set(candidates))
    for collision in registration_groups.values():
        if len(collision) < 2:
            continue
        for item in collision:
            if item.get("identity_status") != "possible_match_requires_confirmation":
                item["identity_status"] = "identifier_name_conflict"
            item["identity_conflict_candidates"] = sorted(other["participant_id"] for other in collision
                                                             if other is not item)
    for collision in source_identity_groups.values():
        distinct_names = {item.get("normalized_name") for item in collision}
        if len(distinct_names) > 1:
            for item in collision:
                item["identity_status"] = "source_identifier_name_conflict"
                item["identity_conflict_candidates"] = sorted(other["participant_id"] for other in collision
                                                                 if other is not item)
    return sorted(result, key=lambda item: (
        str(item.get("kind") or "").casefold(),
        str(item.get("normalized_name") or "").casefold(),
        str(item.get("participant_id") or "")))


def presentation_participants(people):
    """One case-scoped presentation card; retain constituent identities/evidence.

    Name/initial matches are presentation associations, not irreversible identity
    merges. Contradictory document/professional IDs remain distinct.
    """
    def rank(item):
        sources = item.get("sources") or [item.get("source")]
        return (0 if "PJe/TRF1" in sources else
                2 if item.get("function_mapping_status") == "explicit_communication_line" else
                1 if item.get("procedural_function") or item.get("procedural_pole") else 3)

    def category(item):
        return "professional" if professional_kind(item) else "judicial" if item.get("kind") == "ator_judicial" else "person"

    def document(item):
        return re.sub(r"\D", "", str(item.get("cpf") or item.get("cnpj") or item.get("document_number") or ""))

    def abbreviated(name):
        words = normalized_name(name).split()
        if words and all(len(word) == 1 for word in words):
            return "".join(words)
        if len(words) == 1 and 2 <= len(words[0]) <= 10 and name.strip().isupper():
            return words[0]
        return None

    def match(left, right):
        if category(left) != category(right): return None
        ld, rd = document(left), document(right)
        if ld and rd: return "document" if ld == rd else None
        lo, ro = left.get("oab"), right.get("oab")
        if lo and ro and (str(lo), left.get("uf")) != (str(ro), right.get("uf")): return None
        a, b = left["normalized_name"], right["normalized_name"]
        if a == b: return "case_exact_name"
        ai, bi = abbreviated(left["original_name"]), abbreviated(right["original_name"])
        if ai and ai == "".join(word[0] for word in b.split()): return "case_initials"
        if bi and bi == "".join(word[0] for word in a.split()): return "case_initials"
        aw, bw = a.split(), b.split()
        if min(len(aw), len(bw)) >= 3 and (aw == bw[:len(aw)] or bw == aw[:len(bw)]):
            return "possible_name_extension"
        return None

    result = []
    for original in sorted(people, key=rank):
        item = {**original, "kinds": list(original.get("kinds") or [original.get("kind")]),
                "sources": list(original.get("sources") or [original.get("source")]),
                "presentation_members": list(original.get("presentation_members") or [{"participant_id": original.get("participant_id"),
                    "name": original.get("original_name"), "sources": original.get("sources") or [original.get("source")]}])}
        item["original_name"] = safe_participant_name(item.get("original_name"), item.get("role_original"))
        item["normalized_name"] = normalized_name(item["original_name"])
        matches = [(other, match(other, item)) for other in result]
        matches = [(other, reason) for other, reason in matches if reason]
        # Multiple plausible full identities sharing initials remain unresolved.
        if len(matches) != 1:
            result.append(item)
            continue
        target, reason = matches[0]
        target["presentation_members"].extend(item["presentation_members"])
        target["presentation_match"] = reason
        target["presentation_identity_status"] = ("possible_correspondence" if reason in
            {"case_initials", "possible_name_extension"} else "associated_case_identity")
        target["sources"] = sorted(set(target["sources"] + item["sources"]))
        target["kinds"] = sorted(set(target["kinds"] + item["kinds"]))
        for field in ("evidence_ids", "communication_roles", "function_evidence", "relationships", "source_snapshots"):
            values = list(target.get(field) or [])
            for value in item.get(field) or []:
                if value not in values: values.append(value)
            target[field] = values
        # More complete name for display; cover-sheet role remains authoritative.
        if len(item["original_name"]) > len(target["original_name"]) and not abbreviated(item["original_name"]):
            target["original_name"], target["normalized_name"] = item["original_name"], item["normalized_name"]
        for field in ("cpf", "cnpj", "document_number", "document_type", "oab", "uf",
                      "procedural_function", "procedural_pole", "polo_normalized", "polo", "role_original"):
            if not target.get(field) and item.get(field): target[field] = item[field]
        target["mention_count"] = int(target.get("mention_count") or 1) + int(item.get("mention_count") or 1)
    for item in result:
        professional = professional_kind(item)
        if professional:
            item.update(kind=professional, kinds=[professional], procedural_function=professional,
                        procedural_pole=None, polo_normalized=None, polo=None)
        elif "parte" in item["kinds"]:
            item["kinds"] = [kind for kind in item["kinds"] if kind != "destinatário"]
            item["kind"] = "parte"
    return result


def participant_summary(people):
    """Select a compact participant name and count for the portfolio table."""
    people = presentation_participants(people)
    parties = [item for item in people if "parte" in (item.get("kinds") or [item.get("kind")])
               and item.get("original_name")]
    participants_by_identity = {}
    for item in parties:
        identity = item.get("participant_id") or (
            item.get("source"), item.get("normalized_name") or normalized_name(item["original_name"]),
            item.get("kind"), item.get("role"), item.get("polo"),
            tuple(sorted(item.get("evidence_ids") or [])))
        participants_by_identity[identity] = item
    participants = list(participants_by_identity.values())
    active = [item for item in parties if "parte" in (item.get("kinds") or [item.get("kind")])
              and _confirmed_active(item) and not _confirmed_passive(item)]
    # Prefer an explicitly named claimant over a generic active-pole label.
    active.sort(key=lambda item: (not bool(re.search(
        r"\b(autor|autora|requerente|exequente|impetrante|recorrente|reclamante|demandante)\b",
        _role_text(item))), normalized_name(item["original_name"])))
    if active:
        return {"name": active[0]["original_name"], "label": "Polo ativo",
                "additional_count": max(0, len(participants) - 1),
                "participant_id": active[0].get("participant_id")}
    if parties:
        return {"name": parties[0]["original_name"], "label": "Parte identificada",
                "additional_count": max(0, len(participants) - 1),
                "participant_id": parties[0].get("participant_id")}
    # A recipient is useful in the compact table when no procedural party was
    # captured. Keep the source qualification; a professional must never fill
    # the party column merely because the party list is empty.
    recipients = [item for item in people if "destinatário" in
                  (item.get("kinds") or [item.get("kind")]) and item.get("original_name")]
    if recipients:
        return {"name": recipients[0]["original_name"], "label": "Destinatário DJEN",
                "additional_count": max(0, len(recipients) - 1),
                "participant_id": recipients[0].get("participant_id")}
    return {"name": None, "label": None, "additional_count": 0, "participant_id": None}


def _role_text(item):
    labels = [item.get("role"), item.get("role_original"), item.get("papel"), item.get("side"),
              item.get("procedural_function")]
    if item.get("polo_mapping_status") == "provider_descriptive_value":
        labels.append(item.get("polo_normalized"))
    elif item.get("polo_mapping_status") is None:
        labels.append(item.get("polo"))
    labels.extend(item.get("roles") or [])
    labels.extend(item.get("procedural_functions") or [])
    return normalized_name(" ".join(str(value) for value in labels if value))


def _confirmed_active(item):
    if item.get("procedural_pole") == "ativo" or item.get("polo_normalized") == "ativo":
        return True
    return bool(set(re.findall(r"[a-z]+", _role_text(item))) & {
        "ativo", "ativa", "autor", "autora", "requerente", "exequente",
        "impetrante", "recorrente", "reclamante", "demandante", "agravante", "apelante",
        "embargante", "opoente", "excipiente", "impugnante", "suscitante", "querelante"})


def _confirmed_passive(item):
    if item.get("procedural_pole") == "passivo" or item.get("polo_normalized") == "passivo":
        return True
    return bool(set(re.findall(r"[a-z]+", _role_text(item))) & {
        "passivo", "passiva", "reu", "re", "requerido", "requerida", "executado",
        "executada", "impetrado", "impetrada", "recorrido", "recorrida",
        "reclamado", "reclamada", "demandado", "demandada", "agravado", "agravada",
        "apelado", "apelada", "embargado", "embargada", "oposto", "oposta", "excepto",
        "excepta", "impugnado", "impugnada", "suscitado", "suscitada", "querelado", "querelada"})
