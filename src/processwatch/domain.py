"""Typed public data contracts shared by the API and timeline."""
from datetime import datetime
from pydantic import BaseModel, Field


class OrgaoJulgador(BaseModel):
    nome: str | None = None
    codigo: str | None = None
    endereco: str | None = None


class Processo(BaseModel):
    number: str
    court: str
    classe: str | None = None
    distribuicao: str | None = None
    ultima_movimentacao: str | None = None
    ultima_sincronizacao: str | None = None
    situacao: str | None = None


class MovimentoDetalhado(BaseModel):
    id: str
    number: str
    code: str
    name: str
    original_name: str
    when: str
    source: str = "DataJud"
    complements: list[dict] = Field(default_factory=list)


class Audiencia(BaseModel):
    number: str
    when: str
    status: str | None = None
    evidence: str
    source: str


class Advogado(BaseModel):
    nome: str
    oab: str | None = None
    uf: str | None = None


class Parte(BaseModel):
    nome: str
    polo: str | None = None
    papel: str | None = None
    documento_mascarado: str | None = None
    advogados: list[Advogado] = Field(default_factory=list)


class Participant(BaseModel):
    schema_version: int = 1
    participant_id: str
    number: str
    original_name: str
    normalized_name: str
    kind: str
    role: str | None = None
    role_original: str | None = None
    procedural_function: str | None = None
    function_mapping_status: str | None = None
    function_code_original: str | None = None
    role_source_field: str | None = None
    source_record_id: str | None = None
    source_identity_id: str | None = None
    position_scope: str | None = None
    function_scope: str | None = None
    procedural_pole: str | None = None
    identity_type: str = "unconfirmed"
    identity_status: str = "unresolved"
    relationships: list[dict] = Field(default_factory=list)
    evidence: dict = Field(default_factory=dict)
    polo: str | None = None
    polo_code_original: str | None = None
    polo_normalized: str | None = None
    polo_mapping_status: str | None = None
    polo_mapping_reference: str | None = None
    polo_source_field: str | None = None
    communication_role: str | None = None
    role_code_original: str | None = None
    oab: str | None = None
    uf: str | None = None
    document_type: str | None = None
    document_number: str | None = None
    cpf: str | None = None
    cnpj: str | None = None
    source: str
    evidence_id: str
    identity_confidence: str = "menção não conciliada"


class ComunicacaoJudicial(BaseModel):
    source_id: str
    number: str
    hash: str | None = None
    tribunal: str | None = None
    orgao: str | None = None
    tipo: str | None = None
    documento_tipo: str | None = None
    available_at: str | None = None
    published_at: str | None = None
    text_original: str | None = None
    text_plain: str | None = None
    recipients: list[str] = Field(default_factory=list)
    lawyers: list[Advogado] = Field(default_factory=list)
    parties: list[dict] = Field(default_factory=list)
    participants: list[Participant] = Field(default_factory=list)
    meio: str | None = None
    url: str | None = None
    status: str | None = None
    cancellation: str | None = None
    hearing_at: str | None = None
    hearing_status: str | None = None
    hearing_evidence: str | None = None
    collected_at: str | None = None
    original: dict | None = None


class EventoTimeline(BaseModel):
    id: str
    number: str
    tipo: str
    titulo: str
    descricao: str | None = None
    occurred_at: str
    temporal_context: str
    fonte: str
    original_id: str | None = None
    original_url: str | None = None
    collected_at: str | None = None
    related_to: list[str] = Field(default_factory=list)
    validation: str = "original"
    original_title: str | None = None
    code: str | None = None
    complements: list[dict] = Field(default_factory=list)
    complement_display: list[dict] = Field(default_factory=list)
    original: dict | None = None
    tpu: dict | None = None
    fontes: list[str] = Field(default_factory=list)
    preferred_source: str | None = None
    source_event_ids: list[str] = Field(default_factory=list)
    source_details: list[dict] = Field(default_factory=list)
    documents: list[dict] = Field(default_factory=list)
    equivalence: dict | None = None


class FonteDados(BaseModel):
    name: str
    status: str
    checked_at: str | None = None
    period_start: str | None = None
    period_end: str | None = None


class RegistroAcesso(BaseModel):
    at: str
    action: str
    number: str | None = None
    actor: str
    purpose: str


def mask_document(value: str | None) -> str | None:
    if value is None:
        return None
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) not in (11, 14):
        return "***"
    return "*" * (len(digits) - 4) + digits[-4:]
