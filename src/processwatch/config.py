import math
import os
from dataclasses import dataclass
from pathlib import Path
from datetime import date


@dataclass(frozen=True)
class Config:
    db: str = "processwatch.sqlite3"
    environment: str = "principal"
    primary_db: str = ""
    api_key: str = ""
    timeout: float = 90
    attempts: int = 3
    min_interval: float = 2
    max_backoff: float = 60
    interval: int = 86400
    max_processes: int = 100
    access_token: str = ""
    auth_mode: str = "token"
    session_days: int = 14
    session_idle_minutes: int = 60
    session_cookie_secure: bool = False
    production: bool = False
    public_origin: str = ""
    chrome_extension_id: str = "mnbildfdkooflihnapccjchegadodfih"
    djen_initial_date: date = date(2024, 10, 1)
    djen_window_days: int = 90
    djen_overlap_days: int = 7
    djen_sync_interval: int = 86400
    djen_min_interval: float = 1.0
    djen_auto_sync: bool = False
    court_sync_interval: int = 86400
    court_min_interval: float = 10.0
    collection_max_attempts: int = 5
    collection_lease_seconds: int = 1800
    collection_retry_base_seconds: int = 5
    collection_retry_max_seconds: int = 900
    court_retry_base_seconds: int = 30
    court_retry_max_seconds: int = 3600

    def __post_init__(self):
        if self.production or self.environment == "production":
            object.__setattr__(self, "production", True)
            object.__setattr__(self, "environment", "production")
        if self.environment not in ("principal", "production", "development", "test", "homologation"):
            raise ValueError("PROCESSWATCH_ENV deve ser principal, production, development, test ou homologation.")
        for name in ("timeout", "max_backoff", "interval", "attempts", "max_processes",
                     "collection_max_attempts", "collection_lease_seconds",
                     "collection_retry_base_seconds", "collection_retry_max_seconds", "court_sync_interval",
                     "court_retry_base_seconds", "court_retry_max_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Configuração {name} deve ser positiva e finita.")
        if not math.isfinite(self.min_interval) or self.min_interval < 0:
            raise ValueError("min_interval deve ser finito e não negativo.")
        if not math.isfinite(self.djen_min_interval) or self.djen_min_interval < 0:
            raise ValueError("djen_min_interval deve ser finito e não negativo.")
        if not math.isfinite(self.court_min_interval) or self.court_min_interval < 0:
            raise ValueError("court_min_interval deve ser finito e não negativo.")
        if self.djen_window_days < 1 or self.djen_overlap_days < 0 or self.djen_sync_interval < 1:
            raise ValueError("Janela ou sobreposição DJEN inválida.")
        if self.auth_mode not in ("token", "accounts"):
            raise ValueError("PROCESSWATCH_AUTH_MODE deve ser 'token' ou 'accounts'.")
        if self.production and self.auth_mode != "accounts":
            raise ValueError("Em produção, PROCESSWATCH_AUTH_MODE deve ser 'accounts'.")
        if not 1 <= self.session_days <= 30:
            raise ValueError("PROCESSWATCH_SESSION_DAYS deve estar entre 1 e 30.")
        if not 5 <= self.session_idle_minutes <= 1440:
            raise ValueError("PROCESSWATCH_SESSION_IDLE_MINUTES deve estar entre 5 e 1440.")
        if self.production and self.auth_mode == "accounts":
            if not self.session_cookie_secure:
                raise ValueError("Em produção com contas, PROCESSWATCH_SESSION_COOKIE_SECURE deve estar habilitado.")
            if not self.public_origin.startswith("https://"):
                raise ValueError("Em produção com contas, PROCESSWATCH_PUBLIC_ORIGIN deve ser HTTPS.")

    @classmethod
    def from_env(cls):
        # Read local settings without an extra dependency. Real environment
        # variables take precedence, which keeps scheduled tasks configurable.
        env_file = Path.cwd() / ".env"
        if env_file.is_file():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                name, value = name.strip(), value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                os.environ.setdefault(name, value)
        environment = os.getenv("PROCESSWATCH_ENV", "principal").strip().lower()
        return cls(db=os.getenv("PROCESSWATCH_DB", "processwatch.sqlite3"),
                   environment=environment,
                   primary_db=os.getenv("PROCESSWATCH_PRIMARY_DB", ""),
                   api_key=os.getenv("DATAJUD_API_KEY", "").strip(),
                   timeout=float(os.getenv("DATAJUD_TIMEOUT", "90")),
                   attempts=int(os.getenv("DATAJUD_ATTEMPTS", "3")),
                   min_interval=float(os.getenv("DATAJUD_MIN_INTERVAL", "2")),
                   max_backoff=float(os.getenv("DATAJUD_MAX_BACKOFF", "60")),
                   interval=int(os.getenv("PROCESSWATCH_INTERVAL", "86400")),
                   max_processes=int(os.getenv("PROCESSWATCH_MAX_PROCESSES", "100")),
                   access_token=os.getenv("PROCESSWATCH_ACCESS_TOKEN", "").strip(),
                   auth_mode=os.getenv("PROCESSWATCH_AUTH_MODE", "token").strip().lower(),
                   session_days=int(os.getenv("PROCESSWATCH_SESSION_DAYS", "14")),
                   session_idle_minutes=int(os.getenv("PROCESSWATCH_SESSION_IDLE_MINUTES", "60")),
                   session_cookie_secure=os.getenv("PROCESSWATCH_SESSION_COOKIE_SECURE", "0").strip().lower() in ("1", "true", "yes"),
                   production=environment == "production",
                   public_origin=os.getenv("PROCESSWATCH_PUBLIC_ORIGIN", "").strip().rstrip("/"),
                   chrome_extension_id=os.getenv("PROCESSWATCH_CHROME_EXTENSION_ID", "mnbildfdkooflihnapccjchegadodfih").strip(),
                   djen_initial_date=date.fromisoformat(os.getenv("DJEN_INITIAL_DATE", "2024-10-01")),
                   djen_window_days=int(os.getenv("DJEN_WINDOW_DAYS", "90")),
                   djen_overlap_days=int(os.getenv("DJEN_OVERLAP_DAYS", "7")),
                   djen_sync_interval=int(os.getenv("DJEN_SYNC_INTERVAL", "86400")),
                   djen_min_interval=float(os.getenv("DJEN_MIN_INTERVAL", "1")),
                   djen_auto_sync=os.getenv("DJEN_AUTO_SYNC", "0").strip() in ("1", "true", "True"),
                   court_sync_interval=int(os.getenv("PROCESSWATCH_COURT_SYNC_INTERVAL", "86400")),
                   court_min_interval=float(os.getenv("PROCESSWATCH_COURT_MIN_INTERVAL", "10")),
                   collection_max_attempts=int(os.getenv("PROCESSWATCH_JOB_MAX_ATTEMPTS", "5")),
                   collection_lease_seconds=int(os.getenv("PROCESSWATCH_JOB_LEASE_SECONDS", "1800")),
                   collection_retry_base_seconds=int(os.getenv("PROCESSWATCH_JOB_RETRY_BASE_SECONDS", "5")),
                   collection_retry_max_seconds=int(os.getenv("PROCESSWATCH_JOB_RETRY_MAX_SECONDS", "900")),
                   court_retry_base_seconds=int(os.getenv("PROCESSWATCH_COURT_RETRY_BASE_SECONDS", "30")),
                   court_retry_max_seconds=int(os.getenv("PROCESSWATCH_COURT_RETRY_MAX_SECONDS", "3600")))
