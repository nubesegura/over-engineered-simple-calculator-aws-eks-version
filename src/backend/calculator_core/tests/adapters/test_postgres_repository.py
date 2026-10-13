"""Repository tests: statement shape without a database, behavior with a real PostgreSQL.

The tests marked `postgres` need `TEST_DATABASE_URL` (the CI has a postgres service; locally,
start a throwaway container and export the variable). Without it they are skipped.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from calculator_core.adapters.outbound.postgres_repository import PostgresCalculationRepository
from calculator_core.application.errors import InfrastructureError
from calculator_core.domain.calculation import Calculation
from calculator_core.domain.history import HistoryCursor
from calculator_core.domain.operation import Operation
from tests.fakes import FIXED_NOW

DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
needs_postgres = pytest.mark.skipif(not DATABASE_URL, reason="TEST_DATABASE_URL is not set")

# Same table as the migration of task 4.5 (kept inline so these tests do not depend on it).
SCHEMA = """
DROP TABLE IF EXISTS calculations;
CREATE TABLE calculations (
    calculation_id uuid PRIMARY KEY,
    operation text NOT NULL CHECK (operation IN ('add', 'sub', 'mul', 'div')),
    operand_a numeric NOT NULL,
    operand_b numeric NOT NULL,
    result numeric NOT NULL,
    occurred_at timestamptz NOT NULL,
    correlation_id text
);
CREATE INDEX calculations_history_idx ON calculations (occurred_at DESC, calculation_id DESC);
"""
HOSTILE = "x'); DROP TABLE calculations;--"


def _calculation(seconds: int, number: int, result: str = "3", **extra: Any) -> Calculation:
    values: dict[str, Any] = {
        "id": UUID(int=number),
        "operation": Operation.DIV,
        "operand_a": Decimal("1.5"),
        "operand_b": Decimal("0.5"),
        "result": Decimal(result),
        "occurred_at": FIXED_NOW + timedelta(seconds=seconds),
        "correlation_id": "corr",
    }
    return Calculation(**{**values, **extra})


class RecordingConnection:
    def __init__(self, fail: bool) -> None:
        self.fail = fail
        self.rowcount = 1
        self.statements: list[tuple[str, tuple[Any, ...]]] = []
        self.batches: list[tuple[str, list[tuple[Any, ...]]]] = []

    def cursor(self) -> RecordingConnection:
        return self

    def __enter__(self) -> RecordingConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def executemany(self, query: str, rows: list[tuple[Any, ...]]) -> None:
        if self.fail:
            raise psycopg.OperationalError("connection lost")
        self.batches.append((query, rows))
        self.rowcount = len(rows)

    def execute(self, query: str, params: tuple[Any, ...]) -> RecordingConnection:
        if self.fail:
            raise psycopg.OperationalError("connection lost")
        self.statements.append((query, params))
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return []


class RecordingPool:
    def __init__(self, fail: bool = False) -> None:
        self.connection_in_use = RecordingConnection(fail)

    @contextmanager
    def connection(self) -> Iterator[RecordingConnection]:
        yield self.connection_in_use


def _repository(pool: RecordingPool) -> PostgresCalculationRepository:
    return PostgresCalculationRepository(cast("ConnectionPool[Any]", pool))


def test_statements_are_constants_and_values_travel_only_as_parameters() -> None:
    pool = RecordingPool()
    repository = _repository(pool)

    repository.save(_calculation(0, 1, correlation_id=HOSTILE))
    repository.list_page(10, HistoryCursor(FIXED_NOW, UUID(int=7)))

    statements = pool.connection_in_use.statements
    for query, params in statements:
        assert query.count("%s") == len(params)
        assert "DROP TABLE" not in query
        assert str(UUID(int=1)) not in query
    assert "ON CONFLICT (calculation_id) DO NOTHING" in statements[0][0]
    assert "(occurred_at, calculation_id) < (%s, %s)" in statements[1][0]


def test_driver_failures_become_infrastructure_errors() -> None:
    repository = _repository(RecordingPool(fail=True))

    with pytest.raises(InfrastructureError):
        repository.save(_calculation(0, 1))
    with pytest.raises(InfrastructureError):
        repository.list_page(5, None)


def test_a_batch_is_one_statement_execution_with_bound_values() -> None:
    pool = RecordingPool()
    repository = _repository(pool)

    written = repository.save_many([_calculation(0, 1), _calculation(1, 2, correlation_id=HOSTILE)])

    assert written == 2
    ((query, rows),) = pool.connection_in_use.batches
    assert "ON CONFLICT (calculation_id) DO NOTHING" in query
    assert query.count("%s") == len(rows[0])
    assert [row[0] for row in rows] == [UUID(int=1), UUID(int=2)]
    assert HOSTILE not in query


def test_a_batch_driver_failure_becomes_an_infrastructure_error() -> None:
    with pytest.raises(InfrastructureError):
        _repository(RecordingPool(fail=True)).save_many([_calculation(0, 1)])


@pytest.fixture
def repository() -> Iterator[PostgresCalculationRepository]:
    assert DATABASE_URL
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(SCHEMA)
    with ConnectionPool(DATABASE_URL, min_size=1, max_size=2, open=True) as pool:
        yield PostgresCalculationRepository(pool)


@pytest.mark.postgres
@needs_postgres
def test_saved_calculation_is_read_back_unchanged(
    repository: PostgresCalculationRepository,
) -> None:
    saved = _calculation(0, 1, "3.000000000000000000000000000001")

    assert repository.save(saved) is True

    assert repository.list_page(10, None) == [saved]


@pytest.mark.postgres
@needs_postgres
def test_saving_the_same_id_twice_keeps_one_row_and_reports_it(
    repository: PostgresCalculationRepository,
) -> None:
    assert repository.save(_calculation(0, 1, "3")) is True
    assert repository.save(_calculation(5, 1, "99")) is False

    rows = repository.list_page(10, None)
    assert [row.result for row in rows] == [Decimal(3)]


@pytest.mark.postgres
@needs_postgres
def test_empty_history_is_an_empty_list(repository: PostgresCalculationRepository) -> None:
    assert repository.list_page(10, None) == []


@pytest.mark.postgres
@needs_postgres
def test_order_ties_and_keyset_paging_without_repeat_or_skip(
    repository: PostgresCalculationRepository,
) -> None:
    rows = [_calculation(0, 1), _calculation(10, 2), _calculation(10, 3), _calculation(20, 4)]
    for row in rows:
        repository.save(row)

    first = repository.list_page(2, None)
    second = repository.list_page(2, HistoryCursor(first[-1].occurred_at, first[-1].id))
    third = repository.list_page(2, HistoryCursor(second[-1].occurred_at, second[-1].id))

    assert [row.id.int for row in first + second] == [4, 3, 2, 1]
    assert third == []


@pytest.mark.postgres
@needs_postgres
def test_hostile_text_is_stored_as_data_and_the_table_survives(
    repository: PostgresCalculationRepository,
) -> None:
    repository.save(_calculation(0, 1, correlation_id=HOSTILE))
    repository.save(_calculation(1, 2))

    rows = repository.list_page(10, HistoryCursor(FIXED_NOW + timedelta(seconds=9), UUID(int=0)))

    assert {row.correlation_id for row in rows} == {HOSTILE, "corr"}


@pytest.mark.postgres
@needs_postgres
def test_a_batch_counts_only_new_rows_and_a_repeat_creates_no_duplicates(
    repository: PostgresCalculationRepository,
) -> None:
    rows = [_calculation(n, n + 1) for n in range(4)]

    assert repository.save_many(rows[:3]) == 3
    assert repository.save_many(rows) == 1
    assert repository.save_many(rows) == 0
    assert len(repository.list_page(10, None)) == 4
