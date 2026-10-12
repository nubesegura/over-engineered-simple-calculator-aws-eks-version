"""In-memory fakes of the outbound ports, shared by the unit tests."""

from datetime import UTC, datetime
from uuid import UUID

from calculator_core.application.errors import InfrastructureError
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

    def save(self, calculation: Calculation) -> bool:
        if calculation.id in self.rows:
            return False
        self.rows[calculation.id] = calculation
        return True

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

    def list_page(self, limit: int, after: HistoryCursor | None) -> list[Calculation]:
        raise InfrastructureError("not used")
