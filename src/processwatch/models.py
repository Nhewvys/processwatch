from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class Movement:
    scope: str
    anchor: str
    fingerprint: str
    when: str
    code: str
    name: str
    complements: list[dict[str, Any]]
    raw: dict[str, Any]


@dataclass(frozen=True)
class Snapshot:
    metadata: list[dict[str, Any]]
    movements: list[Movement]
    scopes: list[str]
    raw: dict[str, Any]


@dataclass(frozen=True)
class Result:
    imported: int = 0
    new: int = 0
    corrected: int = 0
    review: int = 0


class Provider(Protocol):
    def fetch(self, number: str, court: str) -> dict[str, Any]: ...


class Repository(Protocol):
    def register(self, org: str, number: str, court: str, interval: int, limit: int) -> None: ...
    def get(self, org: str, number: str) -> dict[str, Any]: ...
    def list_watches(self, org: str, due_only: bool = False) -> list[dict[str, Any]]: ...
    def start_run(self, org: str, number: str) -> int: ...
    def apply(self, org: str, number: str, run: int, snapshot: Snapshot) -> Result: ...
    def fail(self, org: str, number: str, run: int, error: str, raw: dict | None = None) -> None: ...
    def timeline(self, org: str, number: str, revisions: bool = False) -> list[dict[str, Any]]: ...
    def runs(self, org: str, number: str) -> list[dict[str, Any]]: ...
    def events(self, org: str, number: str, after: int = 0) -> list[dict[str, Any]]: ...
