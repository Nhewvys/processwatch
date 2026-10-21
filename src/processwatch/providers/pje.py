"""Compatibility contracts and default PJe provider selection."""
from typing import Protocol

from .contracts import SourceCapabilities


class PJeProvider(Protocol):
    source_id: str
    capabilities: SourceCapabilities

    async def fetch_public_case(self, number: str) -> dict: ...


class UnavailablePJeProvider:
    source_id = "PJe/TRF1"
    capabilities = SourceCapabilities()
    access_status = "not_implemented"

    async def fetch_public_case(self, number: str) -> dict:
        return {"status": "not_implemented", "reason": "Instale a dependência opcional de navegador para habilitar a consulta pública TRF1/PJe."}


def default_pje_provider():
    """Return the operational adapter; the Playwright extra remains optional."""
    try:
        from .pje_trf1 import PJeTRF1Connector
        return PJeTRF1Connector()
    except Exception:
        return UnavailablePJeProvider()

