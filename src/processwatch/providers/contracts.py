"""Extensible collection contracts; legacy provider call signatures remain valid."""
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


@dataclass(frozen=True)
class SourceCapabilities:
    # Legacy names remain supported by existing DataJud/DJEN clients.
    process_lookup: bool = False
    metadata: bool = False
    movements: bool = False
    communications: bool = False
    participant_mentions: bool = False
    discovery_oab: bool = False
    hearings: bool = False
    pagination: bool = False
    browser_required: bool = False
    catalog: bool = False
    hierarchy: bool = False
    complements: bool = False
    process_cover: bool = False
    participants: bool = False
    document_metadata: bool = False
    incremental: bool = False
    authentication_required: bool = False


@dataclass(frozen=True)
class CollectionRequest:
    number: str
    source: str
    collection_type: str
    parameters: dict[str, Any] = field(default_factory=dict)
    tribunal: str | None = None
    system: str | None = None
    degree: str | None = None


@dataclass(frozen=True)
class CollectionOutcome:
    source: str
    status: str
    records_seen: int = 0
    coverage: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CollectionFailure:
    source: str
    code: str
    message: str
    retryable: bool
    awaiting_access: bool = False


class ConnectorState(StrEnum):
    OPERATIONAL = "operational"
    PARTIAL = "partially_operational"
    NOT_IMPLEMENTED = "not_implemented"
    NOT_VERIFIED = "not_verified"
    CAPTCHA = "captcha_identified"
    HUMAN_VERIFICATION = "aguardando_verificacao_humana"
    EMPTY_RESPONSE = "err_empty_response"
    RATE_LIMITED = "rate_limited"
    AUTHENTICATION_REQUIRED = "authentication_required"
    UNAUTHORIZED = "unauthorized"
    COURT_UNAVAILABLE = "court_unavailable"
    LAYOUT_INCOMPATIBLE = "layout_incompatible"
    PROCESS_NOT_FOUND = "process_not_found"
    TIMEOUT = "timeout"
    SOURCE_UNAVAILABLE = "source_unavailable"
    CREDITS_INSUFFICIENT = "credits_insufficient"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True)
class ConnectorEvidence:
    source: str
    url: str | None = None
    retrieved_at: str | None = None
    method: str | None = None
    fields: tuple[str, ...] = ()
    raw_reference: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class ConnectorError:
    source: str
    code: str
    message: str
    state: ConnectorState
    retryable: bool
    stage: str | None = None


@dataclass(frozen=True)
class ConnectorResult:
    source: str
    state: ConnectorState
    found: bool | None = None
    data: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[ConnectorEvidence, ...] = ()
    error: ConnectorError | None = None
    limitations: tuple[str, ...] = ()


class CollectionJobRunner(Protocol):
    """Execution boundary for future HTTP or authorized browser connectors."""

    def run(self, job: dict[str, Any]) -> CollectionOutcome: ...


class CollectionConnector(Protocol):
    source_id: str
    capabilities: SourceCapabilities

    async def collect(self, request: CollectionRequest) -> ConnectorResult: ...
