import json
import random
import socket
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from ..config import Config
from ..errors import ProviderError
from .contracts import SourceCapabilities
from ..source_matrix import datajud_alias


class DataJud:
    source_id = "DataJud"
    capabilities = SourceCapabilities(process_lookup=True, metadata=True, movements=True, pagination=True,
                                     process_cover=True, incremental=False)

    def __init__(self, config: Config, opener=urlopen, sleep=time.sleep, clock=time.monotonic):
        self.config, self.opener, self.sleep, self.clock = config, opener, sleep, clock
        self.last_request = None

    def _wait(self):
        if self.last_request is not None:
            self.sleep(max(0, self.config.min_interval - (self.clock() - self.last_request)))
        self.last_request = self.clock()

    def _post(self, court: str, payload: dict) -> dict:
        if not self.config.api_key or self.config.api_key.startswith("APIKey "):
            raise ProviderError("Configure DATAJUD_API_KEY com a chave vigente, sem o prefixo APIKey.")
        alias = datajud_alias(court)
        if not alias:
            raise ProviderError("DataJud não publica endpoint oficial para o tribunal identificado.")
        endpoint = f"https://api-publica.datajud.cnj.jus.br/api_publica_{alias}/_search"
        request = Request(endpoint, data=json.dumps(payload).encode(), method="POST",
                          headers={"Authorization": "APIKey " + self.config.api_key,
                                   "Content-Type": "application/json", "User-Agent": "processwatch/0.1"})
        for attempt in range(self.config.attempts):
            delay = min(self.config.max_backoff, 2 ** attempt + random.random())
            self._wait()
            try:
                with self.opener(request, timeout=self.config.timeout) as response:
                    data = response.read(20 * 1024 * 1024 + 1)
                    if len(data) > 20 * 1024 * 1024:
                        raise ProviderError("Resposta excede limite local de 20 MiB.")
                    return json.loads(data)
            except HTTPError as exc:
                status = exc.code
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                if status in (401, 403):
                    raise ProviderError(f"HTTP {status}: autenticação/acesso recusado; confira a chave e as condições da fonte.") from None
                if status != 429 and not 500 <= status <= 599:
                    raise ProviderError(f"HTTP {status}: consulta recusada.") from None
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            date = parsedate_to_datetime(retry_after)
                            delay = max(delay, (date - datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError):
                            pass
                    # Não ignorar Retry-After longo nem manter CLI presa indefinidamente.
                    if delay > self.config.max_backoff:
                        raise ProviderError(f"HTTP {status}: Retry-After excede espera local; tente mais tarde.") from None
                reason = f"HTTP {status}"
            except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError):
                reason = "falha de rede/timeout"
            except (ValueError, UnicodeError):
                raise ProviderError("Resposta DataJud não contém JSON válido.") from None
            if attempt + 1 < self.config.attempts:
                self.sleep(delay)
        raise ProviderError(f"DataJud indisponível após {self.config.attempts} tentativas: {reason}.")

    def fetch(self, number: str, court: str) -> dict:
        # Paginação limitada e total exato: nunca importar silenciosamente resposta truncada.
        hits, last = [], {}
        for offset in range(0, 1000, 100):
            last = self._post(court, {"query": {"match": {"numeroProcesso": number}},
                                      "size": 100, "from": offset, "track_total_hits": True})
            if last.get("timed_out") or last.get("_shards", {}).get("failed", 0):
                raise ProviderError("DataJud retornou busca parcial.")
            try:
                batch = last["hits"]["hits"]
                total = last["hits"]["total"]
                if isinstance(total, dict):
                    if total.get("relation") != "eq":
                        raise ProviderError("Total de registros DataJud inexato.")
                    total = total["value"]
                if not isinstance(batch, list) or not isinstance(total, int):
                    raise TypeError
            except (KeyError, TypeError):
                raise ProviderError("Estrutura de resposta DataJud inválida.") from None
            hits.extend(batch)
            if len(hits) >= total:
                return dict(last, hits={"total": {"value": total, "relation": "eq"}, "hits": hits})
            if not batch:
                raise ProviderError("Paginação incompleta; nenhum dado importado.")
        raise ProviderError("Consulta excedeu 1000 documentos; nenhum dado importado.")
