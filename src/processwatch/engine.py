import logging
from .cnj import normalize, tribunal
from .models import Provider, Repository, Result
from .errors import ProviderError
from .normalizer import normalize_response
from .environment_policy import require_operational_office

log = logging.getLogger("processwatch")


class Engine:
    def __init__(self, repository: Repository, provider: Provider):
        self.repository, self.provider = repository, provider

    def register(self, number: str, org="local", interval=86400, limit=100):
        require_operational_office(getattr(self.repository, "environment", "development"), org)
        number = normalize(number)
        self.repository.register(org, number, tribunal(number), interval, limit)
        return number

    def check(self, number: str, org="local") -> Result:
        require_operational_office(getattr(self.repository, "environment", "development"), org)
        number = normalize(number)
        watch = self.repository.get(org, number)
        run = self.repository.start_run(org, number)
        raw = None
        try:
            raw = self.provider.fetch(number, watch["court"])
            snapshot = normalize_response(raw, number, watch["court"])
            result = self.repository.apply(org, number, run, snapshot)
        except Exception as exc:
            # Mensagens de fornecedores não devem vazar credenciais, URLs assinadas ou corpo HTTP.
            safe = str(exc) if isinstance(exc, (ProviderError, ValueError)) else f"Falha interna: {type(exc).__name__}"
            self.repository.fail(org, number, run, safe, raw)
            log.error("check_failed", extra={"event": "check_failed", "org": org, "run_id": run, "error": safe})
            raise RuntimeError(safe) from None
        log.info("check_success", extra={"event": "check_success", "org": org, "run_id": run, "result": result.__dict__})
        return result

    def check_all(self, org="local"):
        """Check every registered process, isolating failures per process."""
        require_operational_office(getattr(self.repository, "environment", "development"), org)
        results = []
        for watch in self.repository.list_watches(org):
            try:
                results.append((watch["number"], self.check(watch["number"], org), None))
            except RuntimeError as exc:
                results.append((watch["number"], None, str(exc)))
        return results
