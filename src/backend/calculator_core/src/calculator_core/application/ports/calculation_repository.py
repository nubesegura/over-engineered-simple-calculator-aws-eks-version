from typing import Protocol

from calculator_core.domain.calculation import Calculation
from calculator_core.domain.history import HistoryCursor


class CalculationRepository(Protocol):
    def save(self, calculation: Calculation) -> bool:
        """Persist a calculation. Idempotent by `calculation.id`.

        Returns True when a new row was written and False when the id already existed
        (the stored row is kept and nothing fails). A row is visible to the next
        `list_page` call as soon as `save` returns.
        Must raise `InfrastructureError` if the row was not persisted.
        """

    def list_page(self, limit: int, after: HistoryCursor | None) -> list[Calculation]:
        """Return at most `limit` calculations in history order (keyset pagination).

        Order: `occurred_at` descending, then `id` descending. With `after` set, only
        rows strictly later than that key in this order are returned. An empty store
        yields an empty list.
        Must raise `InfrastructureError` if the read failed.
        """
