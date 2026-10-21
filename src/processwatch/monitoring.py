"""Historical DJEN windows and conservative incremental overlap."""
from dataclasses import dataclass
from datetime import date, timedelta

from .cnj import normalize
from .providers.djen import DjenError


@dataclass(frozen=True)
class SyncReport:
    number: str
    requested_start: str
    requested_end: str
    windows_completed: int
    records_seen: int
    status: str
    error: str | None = None


def date_windows(start: date, end: date, days: int = 90):
    if days < 1 or end < start:
        raise ValueError("Período ou tamanho de janela inválido.")
    current = start
    while current <= end:
        finish = min(end, current + timedelta(days=days - 1))
        yield current, finish
        current = finish + timedelta(days=1)


def first_uncovered(initial: date, coverage: list[dict], overlap_days: int = 7) -> date:
    """Only complete, contiguous windows advance the incremental cursor."""
    cursor = initial
    intervals = sorted((date.fromisoformat(row["period_start"]), date.fromisoformat(row["period_end"]))
                       for row in coverage if row["status"] == "período consultado"
                       and row["period_start"] and row["period_end"])
    for start, end in intervals:
        if start > cursor:
            break
        if end >= cursor:
            cursor = end + timedelta(days=1)
    return max(initial, cursor - timedelta(days=overlap_days)) if cursor > initial else initial


async def sync_djen(repository, client, org: str, number: str, *, initial: date,
                    end: date | None = None, start: date | None = None,
                    window_days: int = 90, overlap_days: int = 7) -> SyncReport:
    number = normalize(number)
    repository.get(org, number)
    finish = end or date.today()
    coverage = repository.source_coverage(org, number, "DJEN")
    begin = start or first_uncovered(initial, coverage, overlap_days)
    if finish < begin:
        return SyncReport(number, begin.isoformat(), finish.isoformat(), 0, 0, "atualizado")
    completed = seen = 0
    for window_start, window_end in date_windows(begin, finish, window_days):
        try:
            result = await client.fetch(number, window_start, window_end)
            if not result.complete:
                raise DjenError("DJEN não confirmou a cobertura da janela.")
            items = [item.model_dump() for item in result.items]
            repository.save_communications(org, number, items, status="período consultado",
                                           period_start=window_start.isoformat(), period_end=window_end.isoformat())
            completed += 1
            seen += len(items)
        except DjenError as exc:
            repository.save_communications(org, number, [], status="erro",
                                           period_start=window_start.isoformat(), period_end=window_end.isoformat(),
                                           error=str(exc))
            return SyncReport(number, begin.isoformat(), finish.isoformat(), completed, seen, "parcial", str(exc))
    return SyncReport(number, begin.isoformat(), finish.isoformat(), completed, seen, "ok")


def import_numbers(engine, repository, org: str, values, *, portfolio: str,
                   interval: int, limit: int, fetch=False, config=None):
    from .config import Config
    from .orchestrator import schedule_sync

    config = config or Config(interval=interval, max_processes=limit)
    results = []
    seen = set()
    for value in values:
        try:
            number = normalize(value.strip())
            if number in seen:
                results.append({"input": value, "number": number, "status": "duplicado"})
                continue
            seen.add(number)
            engine.register(number, org, interval, limit)
            repository.add_to_portfolio(org, portfolio, number)
            sync = schedule_sync(repository, org, number, config, collection_type="batch",
                                 priority=10 if fetch else 50)
            results.append({"input": value, "number": number, "status": "cadastrado",
                            "sync_group_id": sync.get("group_id"),
                            "synchronization": sync["status"]})
        except (ValueError, RuntimeError) as exc:
            results.append({"input": value, "status": "erro", "error": str(exc)})
    return results
