"""Per-request timing for SQLite calls made by the local API.

The measurements stay in a context-local accumulator, so concurrent requests do
not mix their timings and no SQL or process data is logged.
"""
from contextvars import ContextVar
from time import perf_counter


current_sqlite_metrics = ContextVar("processwatch_sqlite_metrics", default=None)


def _record_sqlite_time(elapsed):
    metrics = current_sqlite_metrics.get()
    if metrics is not None:
        metrics["sqlite_seconds"] += elapsed
        metrics["sqlite_calls"] += 1


class _TimedCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def fetchone(self):
        started = perf_counter()
        try:
            return self._cursor.fetchone()
        finally:
            _record_sqlite_time(perf_counter() - started)

    def fetchmany(self, size=None):
        started = perf_counter()
        try:
            return self._cursor.fetchmany() if size is None else self._cursor.fetchmany(size)
        finally:
            _record_sqlite_time(perf_counter() - started)

    def fetchall(self):
        started = perf_counter()
        try:
            return self._cursor.fetchall()
        finally:
            _record_sqlite_time(perf_counter() - started)

    def __iter__(self):
        iterator = iter(self._cursor)
        while True:
            started = perf_counter()
            try:
                row = next(iterator)
            except StopIteration:
                _record_sqlite_time(perf_counter() - started)
                return
            except BaseException:
                _record_sqlite_time(perf_counter() - started)
                raise
            _record_sqlite_time(perf_counter() - started)
            yield row


class TimedSQLiteConnection:
    """Small proxy that times execute/fetch work without retaining SQL text."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def execute(self, *args, **kwargs):
        started = perf_counter()
        try:
            return _TimedCursor(self._connection.execute(*args, **kwargs))
        finally:
            _record_sqlite_time(perf_counter() - started)

    def executemany(self, *args, **kwargs):
        started = perf_counter()
        try:
            return _TimedCursor(self._connection.executemany(*args, **kwargs))
        finally:
            _record_sqlite_time(perf_counter() - started)

    def executescript(self, *args, **kwargs):
        started = perf_counter()
        try:
            return _TimedCursor(self._connection.executescript(*args, **kwargs))
        finally:
            _record_sqlite_time(perf_counter() - started)

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        return self._connection.__exit__(exc_type, exc, traceback)


def instrument_repository(repository):
    if not isinstance(repository.db, TimedSQLiteConnection):
        repository.db = TimedSQLiteConnection(repository.db)
