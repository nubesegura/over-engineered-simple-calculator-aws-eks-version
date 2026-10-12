import base64
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from calculator_core.application.errors import InvalidQueryError
from calculator_core.application.use_cases.get_history import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    GetHistory,
    GetHistoryQuery,
)
from calculator_core.domain.calculation import Calculation
from calculator_core.domain.errors import InvalidCursorError, InvalidInputError
from calculator_core.domain.history import CursorCodec, HistoryCursor
from calculator_core.domain.operation import Operation
from tests.fakes import CURSOR_KEY, FIXED_NOW, InMemoryCalculationRepository

CODEC = CursorCodec(CURSOR_KEY)


def _calculation(seconds: int, number: int) -> Calculation:
    return Calculation(
        id=UUID(int=number),
        operation=Operation.ADD,
        operand_a=Decimal(1),
        operand_b=Decimal(2),
        result=Decimal(3),
        occurred_at=FIXED_NOW + timedelta(seconds=seconds),
    )


def _repository(*rows: Calculation) -> InMemoryCalculationRepository:
    repo = InMemoryCalculationRepository()
    for row in rows:
        repo.save(row)
    return repo


def _use_case(repo: InMemoryCalculationRepository) -> GetHistory:
    return GetHistory(repo, CODEC)


def _numbers(items: list[Calculation]) -> list[int]:
    return [item.id.int for item in items]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _signed(raw: bytes, key: bytes = CURSOR_KEY) -> str:
    """A payload signed with the real key, to reach the payload checks."""
    return f"{_b64(raw)}.{_b64(hmac.new(key, raw, hashlib.sha256).digest())}"


def test_first_read_of_an_empty_history_is_empty_without_cursor() -> None:
    page = _use_case(_repository()).execute(GetHistoryQuery())

    assert page.items == []
    assert page.next_cursor is None


def test_default_limit_is_20_and_next_cursor_is_null_at_the_end() -> None:
    use_case = _use_case(_repository(*(_calculation(n, n) for n in range(25))))

    first = use_case.execute(GetHistoryQuery())
    second = use_case.execute(GetHistoryQuery(cursor=first.next_cursor))

    assert DEFAULT_LIMIT == 20
    assert len(first.items) == 20
    assert first.next_cursor is not None
    assert len(second.items) == 5
    assert second.next_cursor is None


def test_limit_above_the_cap_is_reduced_to_100() -> None:
    use_case = _use_case(_repository(*(_calculation(n, n) for n in range(MAX_LIMIT + 5))))

    page = use_case.execute(GetHistoryQuery(limit=10_000))

    assert len(page.items) == MAX_LIMIT
    assert page.next_cursor is not None


@pytest.mark.parametrize("limit", [0, -1])
def test_limit_below_one_is_an_input_error(limit: int) -> None:
    with pytest.raises(InvalidQueryError) as caught:
        _use_case(_repository()).execute(GetHistoryQuery(limit=limit))

    assert isinstance(caught.value, InvalidInputError)


def test_most_recent_first_with_stable_tie_break_on_calculation_id() -> None:
    repo = _repository(
        _calculation(10, 1), _calculation(20, 2), _calculation(20, 3), _calculation(5, 4)
    )

    page = _use_case(repo).execute(GetHistoryQuery())

    assert _numbers(page.items) == [3, 2, 1, 4]


def test_paging_never_repeats_or_skips_rows_even_with_equal_timestamps() -> None:
    use_case = _use_case(_repository(*(_calculation(n // 3, n) for n in range(10))))

    seen: list[int] = []
    cursor: str | None = None
    while True:
        page = use_case.execute(GetHistoryQuery(limit=3, cursor=cursor))
        seen.extend(_numbers(page.items))
        cursor = page.next_cursor
        if cursor is None:
            break

    assert sorted(seen) == list(range(10))
    assert seen == _numbers(use_case.execute(GetHistoryQuery(limit=100)).items)


def test_a_saved_calculation_shows_in_the_next_read() -> None:
    repo = _repository(_calculation(1, 1))
    use_case = _use_case(repo)
    assert _numbers(use_case.execute(GetHistoryQuery()).items) == [1]

    repo.save(_calculation(2, 2))

    assert _numbers(use_case.execute(GetHistoryQuery()).items) == [2, 1]


def test_saving_the_same_calculation_id_twice_keeps_one_row() -> None:
    repo = InMemoryCalculationRepository()

    assert repo.save(_calculation(1, 1)) is True
    assert repo.save(_calculation(9, 1)) is False

    page = _use_case(repo).execute(GetHistoryQuery())
    assert _numbers(page.items) == [1]
    assert page.items[0].occurred_at == FIXED_NOW + timedelta(seconds=1)


@pytest.mark.parametrize("cursor", ["", "not base64!", "abc", "a.b.c", "e30.e30", "..", "e30."])
def test_cursor_not_issued_by_the_system_is_an_input_error(cursor: str) -> None:
    with pytest.raises(InvalidCursorError) as caught:
        _use_case(_repository()).execute(GetHistoryQuery(cursor=cursor))

    assert isinstance(caught.value, InvalidInputError)


def test_cursor_issued_under_another_key_is_rejected() -> None:
    foreign = CursorCodec(b"another-key-another-key-0000")

    cursor = foreign.encode(HistoryCursor(FIXED_NOW, UUID(int=5)))

    with pytest.raises(InvalidCursorError):
        CODEC.decode(cursor)


def test_tampered_payload_is_rejected() -> None:
    _, tag = CODEC.encode(HistoryCursor(FIXED_NOW, UUID(int=5))).split(".")
    other = json.dumps({"t": FIXED_NOW.isoformat(), "i": str(UUID(int=6))}).encode()

    with pytest.raises(InvalidCursorError):
        CODEC.decode(f"{_b64(other)}.{tag}")


@pytest.mark.parametrize(
    "raw",
    [
        b'{"t":"2026-10-05T12:00:00+00:00","i":5}',
        b'{"t":5,"i":"00000000-0000-0000-0000-000000000001"}',
        b'{"t":null,"i":null}',
        b'{"t":"2026-10-05T12:00:00","i":"00000000-0000-0000-0000-000000000001"}',
        b"not-json",
        b"[" * 20_000 + b"]" * 20_000,
    ],
    ids=["id-not-text", "time-not-text", "nulls", "no-timezone", "not-json", "deeply-nested"],
)
def test_authentic_but_malformed_payload_is_an_input_error(raw: bytes) -> None:
    with pytest.raises(InvalidCursorError):
        CODEC.decode(_signed(raw))


def test_cursor_round_trips() -> None:
    cursor = HistoryCursor(occurred_at=FIXED_NOW, calculation_id=UUID(int=7))

    assert CODEC.decode(CODEC.encode(cursor)) == cursor


def test_short_cursor_key_is_refused() -> None:
    with pytest.raises(ValueError, match="at least"):
        CursorCodec(b"short")
