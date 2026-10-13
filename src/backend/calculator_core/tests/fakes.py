"""In-memory fakes of the outbound ports, shared by the unit tests."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from calculator_core.application.errors import InfrastructureError
from calculator_core.application.ports.error_notifier import InternalErrorReport
from calculator_core.domain.calculation import Calculation
from calculator_core.domain.history import HistoryCursor, is_after_cursor, sort_key

FIXED_ID = UUID("00000000-0000-0000-0000-000000000001")
FIXED_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
CURSOR_KEY = b"unit-test-cursor-key-0123456789"


class FixedClock:
    def now(self) -> datetime:
        return FIXED_NOW


class FixedIdGenerator:
    def new_id(self) -> UUID:
        return FIXED_ID


class InMemoryCalculationRepository:
    """Keeps one row per calculation id and serves the keyset order of the port."""

    def __init__(self) -> None:
        self.rows: dict[UUID, Calculation] = {}
        self.batch_sizes: list[int] = []
        self.fail_on_batch: int | None = None

    def save(self, calculation: Calculation) -> bool:
        if calculation.id in self.rows:
            return False
        self.rows[calculation.id] = calculation
        return True

    def save_many(self, calculations: Sequence[Calculation]) -> int:
        """All-or-nothing like a transaction; `fail_on_batch` (1-based) simulates an outage."""
        if self.fail_on_batch == len(self.batch_sizes) + 1:
            raise InfrastructureError("db down")
        self.batch_sizes.append(len(calculations))
        return sum(1 for calculation in calculations if self.save(calculation))

    def list_page(self, limit: int, after: HistoryCursor | None) -> list[Calculation]:
        ordered = sorted(self.rows.values(), key=sort_key, reverse=True)
        if after is not None:
            ordered = [row for row in ordered if is_after_cursor(row, after)]
        return ordered[:limit]


class FailingRepository:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def save(self, calculation: Calculation) -> bool:
        raise self.error

    def save_many(self, calculations: Sequence[Calculation]) -> int:
        raise self.error

    def list_page(self, limit: int, after: HistoryCursor | None) -> list[Calculation]:
        raise InfrastructureError("not used")


class RecordingNotifier:
    def __init__(self) -> None:
        self.reports: list[InternalErrorReport] = []

    def notify(self, report: InternalErrorReport) -> None:
        self.reports.append(report)


class InMemoryObjectStore:
    """Dict-backed object store; `fail_on` names the operation that raises InfrastructureError."""

    def __init__(self, objects: dict[str, bytes] | None = None, fail_on: str | None = None) -> None:
        self.objects = dict(objects or {})
        self.fail_on = fail_on

    def _check(self, operation: str) -> None:
        if self.fail_on == operation:
            raise InfrastructureError(f"{operation} failed")

    def read(self, key: str, max_bytes: int) -> bytes | None:
        self._check("read")
        body = self.objects.get(key)
        return None if body is None else body[: max_bytes + 1]

    def put(self, key: str, body: bytes) -> None:
        self._check("put")
        self.objects[key] = body

    def move(self, source: str, target: str) -> None:
        self._check("move")
        self.objects[target] = self.objects.pop(source)
