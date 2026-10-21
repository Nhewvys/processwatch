"""Operational tenant policy shared by API, CLI, engine and worker."""

RESERVED_LOCAL_OFFICE = "local"


def is_local_office_blocked(environment, office_id):
    return (str(environment or "").strip().lower() == "production" and
            str(office_id or "").strip().lower() == RESERVED_LOCAL_OFFICE)


def require_operational_office(environment, office_id):
    if not office_id:
        raise ValueError("Informe o escritório para esta operação.")
    if is_local_office_blocked(environment, office_id):
        raise ValueError("O escritório reservado 'local' não pode ser utilizado no ambiente de produção.")
