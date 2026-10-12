from dataclasses import dataclass

from calculator_core.application.errors import InvalidQueryError
from calculator_core.application.ports.calculation_repository import CalculationRepository
from calculator_core.domain.calculation import Calculation
from calculator_core.domain.history import CursorCodec, cursor_for

DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@dataclass(frozen=True)
class GetHistoryQuery:
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None


@dataclass(frozen=True)
class HistoryPage:
    items: list[Calculation]
    next_cursor: str | None


class GetHistory:
    def __init__(self, repository: CalculationRepository, cursor_codec: CursorCodec) -> None:
        self.repository = repository
        self.cursor_codec = cursor_codec

    def execute(self, query: GetHistoryQuery) -> HistoryPage:
        if query.limit < 1:
            raise InvalidQueryError("limit must be greater than 0.")
        limit = min(query.limit, MAX_LIMIT)
        after = self.cursor_codec.decode(query.cursor) if query.cursor is not None else None
        # One extra row tells whether another page exists without a second query.
        rows = self.repository.list_page(limit + 1, after)
        items = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = self.cursor_codec.encode(cursor_for(items[-1])) if has_more else None
        return HistoryPage(items=items, next_cursor=next_cursor)
