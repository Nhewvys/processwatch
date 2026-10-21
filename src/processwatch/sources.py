"""Capabilities and access status; connectors remain outside domain services."""
from dataclasses import dataclass


@dataclass(frozen=True)
class SourceCapability:
    name: str
    process_lookup: bool = False
    communication_lookup: bool = False
    participant_mentions: bool = False
    discovery_oab: bool = False
    automated_access_verified: bool = False
    limitation: str | None = None


REGISTRY = {
    "DataJud": SourceCapability("DataJud", process_lookup=True,
                                limitation="Metadados e movimentos públicos; partes não garantidas."),
    "DJEN": SourceCapability("DJEN", communication_lookup=True, participant_mentions=True,
                             discovery_oab=True, automated_access_verified=True,
                             limitation="OAB/UF descobre somente comunicações dentro do período consultado."),
    "PJe/TRF1": SourceCapability("PJe/TRF1", limitation="API pública e autorização de automação não confirmadas."),
    "SGT/TPU": SourceCapability("SGT/TPU", limitation="479 códigos na versão 2026-10-06; planilha cobre Justiça Federal de 1º grau."),
}


def require_capability(source: str, capability: str):
    item = REGISTRY[source]
    if not getattr(item, capability, False):
        raise ValueError(f"{source} não possui capacidade confirmada: {capability}.")
