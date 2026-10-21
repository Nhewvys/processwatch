"""Discovery criteria are admitted only when a source declares support."""
from dataclasses import dataclass
from datetime import date
import re

from .cnj import normalize
from .sources import require_capability


@dataclass(frozen=True)
class DiscoveryResult:
    criterion: str
    numbers: tuple[str, ...]
    source: str
    coverage: str


@dataclass(frozen=True)
class OabCriterion:
    oab: str
    uf: str
    source: str


BRAZIL_UFS = frozenset({
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
    "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO",
})


def normalize_oab(value: str) -> str:
    """Normalize a professional registration for an exact OAB query."""
    raw = str(value or "").strip().upper()
    normalized = re.sub(r"[.\-/\s]", "", raw)
    if not normalized or len(normalized) > 12 or not normalized.isascii() or not normalized.isalnum():
        raise ValueError("Número OAB inválido.")
    return normalized


def normalize_document_digits(value: str, kind: str) -> str:
    """Strip presentation punctuation without validating or querying a document.

    This helper is intentionally not wired to a search endpoint: no source has
    been validated for automated CPF/CNPJ discovery in this application.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    expected = {"CPF": 11, "CNPJ": 14}.get(str(kind or "").upper())
    if expected is None or len(digits) != expected:
        raise ValueError("Documento inválido para normalização.")
    return digits


def by_number(value: str) -> DiscoveryResult:
    number = normalize(value)
    return DiscoveryResult("numero_cnj", (number,), "entrada_do_usuario", "identificação, sem consulta externa")


def by_oab(number: str, uf: str, source: str):
    normalized_number = normalize_oab(number)
    normalized_uf = str(uf or "").strip().upper()
    if normalized_uf not in BRAZIL_UFS:
        raise ValueError("OAB e UF inválidas.")
    require_capability(source, "discovery_oab")
    return OabCriterion(normalized_number, normalized_uf, source)


async def discover_oab(repository, client, org: str, number: str, uf: str,
                       start: date, end: date):
    """DJEN discovery with exact OAB evidence, independent of monitoring enrollment."""
    criterion = by_oab(number, uf, "DJEN")
    if end < start or (end - start).days > 30:
        raise ValueError("Informe período explícito de até 31 dias.")
    uf = criterion.uf
    try:
        result = await client.fetch_by_oab(criterion.oab, criterion.uf, start, end)
    except Exception as exc:
        repository.record_oab_discovery_failure(org, number, uf,
                                                start.isoformat(), end.isoformat(), exc)
        raise
    run_id = repository.save_oab_discovery(org, number, uf, result)
    return {"run_id": run_id, "source": "DJEN", "period_start": result.period_start,
            "period_end": result.period_end, "pages": result.pages,
            "reported": result.reported, "matched": len(result.items),
            "processes": repository.discovered_by_oab(org, number, uf)}
