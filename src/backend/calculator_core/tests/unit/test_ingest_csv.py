from datetime import UTC, datetime
from decimal import Decimal

import pytest

from calculator_core.application.errors import InfrastructureError
from calculator_core.application.ingest_csv import (
    MAX_FILE_BYTES,
    IngestCsv,
    IngestStatus,
    is_valid_key,
    row_id,
)
from calculator_core.domain.operation import Operation
from tests.fakes import FailingRepository, InMemoryCalculationRepository
from tests.fakes_ingest import InMemoryObjectStore, RecordingNotifier

KEY = "incoming/batch.csv"
HEADER = "operation,operand_a,operand_b,occurred_at\n"
GOOD = HEADER + "add,1,2,2026-10-01T10:00:00Z\nmul,3,4.5,2026-10-01T11:00:00+00:00\n"


def make(
    content: bytes, fail_on: str | None = None
) -> tuple[IngestCsv, InMemoryObjectStore, InMemoryCalculationRepository, RecordingNotifier]:
    store = InMemoryObjectStore({KEY: content}, fail_on)
    repository = InMemoryCalculationRepository()
    notifier = RecordingNotifier()
    use_case = IngestCsv(store, repository, notifier, "calculator-ingest", "dev")
    return use_case, store, repository, notifier


def test_valid_file_is_saved_and_moved_to_processed() -> None:
    use_case, store, repository, notifier = make(GOOD.encode())

    result = use_case.execute(KEY)

    assert (result.status, result.created, result.invalid) == (IngestStatus.PROCESSED, 2, 0)
    results = sorted(row.result for row in repository.rows.values())
    assert results == [Decimal("3"), Decimal("13.5")]
    assert set(store.objects) == {"processed/batch.csv"}
    assert notifier.reports == []


def test_processing_the_same_content_twice_creates_no_duplicates() -> None:
    use_case, store, repository, _ = make(GOOD.encode())
    use_case.execute(KEY)
    store.objects[KEY] = (GOOD + "add,1.0,2.00,2026-10-01T10:00:00Z\n").encode()

    result = use_case.execute(KEY)

    assert (result.created, result.duplicates) == (0, 2)
    assert len(repository.rows) == 2


def test_id_depends_on_content_only() -> None:
    moment = datetime(2026, 10, 1, tzinfo=UTC)

    same = row_id(Operation.ADD, Decimal("1.0"), Decimal("2"), moment)
    assert same == row_id(Operation.ADD, Decimal("1"), Decimal("2.000"), moment)
    assert same != row_id(Operation.SUB, Decimal("1"), Decimal("2"), moment)


@pytest.mark.parametrize(
    ("line", "reason"),
    [
        ("pow,1,2,2026-10-01T10:00:00Z", "unknown operation"),
        ("add,x,2,2026-10-01T10:00:00Z", "operand_a is not a number"),
        ("add,1,NaN,2026-10-01T10:00:00Z", "finite"),
        ("add,sNaN,2,2026-10-01T10:00:00Z", "finite"),
        ("add,1,-sNaN,2026-10-01T10:00:00Z", "finite"),
        ("mul,Infinity,2,2026-10-01T10:00:00Z", "finite"),
        ("mul,1,-Infinity,2026-10-01T10:00:00Z", "finite"),
        ("add,1e50000000,2,2026-10-01T10:00:00Z", "between"),
        ("add,1,-1e999999999999999,2026-10-01T10:00:00Z", "between"),
        ("div,1,1e-50000000,2026-10-01T10:00:00Z", "decimal places"),
        ("div,1,0,2026-10-01T10:00:00Z", "Division by zero"),
        ("add,1,2,yesterday", "ISO-8601"),
        ("add,1,2,2026-10-01T10:00:00", "UTC"),
        ("add,1,2,2026-10-01T10:00:00+02:00", "UTC"),
        ("add,1,2", "expected 4 columns"),
    ],
)
def test_invalid_rows_are_skipped_and_reported_with_line_and_reason(line: str, reason: str) -> None:
    use_case, store, _, notifier = make((GOOD + line + "\n").encode())

    result = use_case.execute(KEY)

    assert (result.status, result.created, result.invalid) == (IngestStatus.PROCESSED, 2, 1)
    report = store.objects["reports/batch.csv.txt"].decode()
    assert report.startswith("line 4: ")
    assert reason in report
    assert "processed/batch.csv" in store.objects
    assert notifier.reports == []


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"operation,a,b,c\nadd,1,2,2026-10-01T10:00:00Z\n", "header"),
        (b"", "header"),
        (b"\xff\xfe\x00binary", "UTF-8"),
        pytest.param(b"a" * (MAX_FILE_BYTES + 1), "5 MB", id="oversize"),
        ((HEADER + 'add,"1,2,2026-10-01T10:00:00Z\n').encode(), "not valid CSV"),
    ],
)
def test_unusable_file_is_rejected_with_report_and_alert(content: bytes, reason: str) -> None:
    use_case, store, repository, notifier = make(content)

    result = use_case.execute(KEY)

    assert result.status is IngestStatus.REJECTED
    assert set(store.objects) == {"rejected/batch.csv", "reports/batch.csv.txt"}
    assert reason in store.objects["reports/batch.csv.txt"].decode()
    assert repository.rows == {}
    assert [report.error_type for report in notifier.reports] == ["FileRejected"]
    assert reason in notifier.reports[0].error_message


@pytest.mark.parametrize(
    "key",
    [
        "incoming/../secrets.csv",
        "incoming/a/b.csv",
        "../incoming/a.csv",
        "processed/a.csv",
        "incoming/a.txt",
        "incoming/.csv",
        "incoming/a..csv",
        "incoming/a.csv/",
        "/incoming/a.csv",
        pytest.param("incoming/" + "a" * 200 + ".csv", id="long-name"),
    ],
)
def test_hostile_or_misplaced_keys_are_ignored_with_an_alert(key: str) -> None:
    use_case, store, _, notifier = make(GOOD.encode())

    result = use_case.execute(key)

    assert result.status is IngestStatus.HOSTILE_KEY
    assert set(store.objects) == {KEY}
    assert [report.error_type for report in notifier.reports] == ["HostileObjectKey"]
    assert key not in notifier.reports[0].error_message


def test_valid_key_shape() -> None:
    assert is_valid_key("incoming/data-2026_10.v1.csv")


def test_missing_object_is_already_handled() -> None:
    use_case, store, _, notifier = make(GOOD.encode())
    store.objects.clear()

    assert use_case.execute(KEY).status is IngestStatus.ALREADY_HANDLED
    assert notifier.reports == []


@pytest.mark.parametrize("failing", ["read", "move"])
def test_store_failure_raises_and_keeps_the_file_in_incoming(failing: str) -> None:
    use_case, store, _, _ = make(GOOD.encode(), fail_on=failing)

    with pytest.raises(InfrastructureError):
        use_case.execute(KEY)

    assert KEY in store.objects


def test_database_failure_raises_and_keeps_the_file_in_incoming() -> None:
    store = InMemoryObjectStore({KEY: GOOD.encode()})
    use_case = IngestCsv(
        store, FailingRepository(InfrastructureError("db down")), RecordingNotifier(), "s", "dev"
    )

    with pytest.raises(InfrastructureError):
        use_case.execute(KEY)

    assert set(store.objects) == {KEY}


def rows_text(count: int) -> str:
    return HEADER + "".join(f"add,{n},1,2026-10-01T10:00:00Z\n" for n in range(count))


def make_batched(
    count: int, batch_size: int
) -> tuple[IngestCsv, InMemoryObjectStore, InMemoryCalculationRepository]:
    store = InMemoryObjectStore({KEY: rows_text(count).encode()})
    repository = InMemoryCalculationRepository()
    use_case = IngestCsv(store, repository, RecordingNotifier(), "s", "dev", batch_size)
    return use_case, store, repository


@pytest.mark.parametrize(
    ("count", "batch_size", "expected"),
    [(0, 3, []), (3, 3, [3]), (4, 3, [3, 1]), (7, 3, [3, 3, 1]), (2, 500, [2])],
)
def test_valid_rows_are_saved_in_batches_of_the_configured_size(
    count: int, batch_size: int, expected: list[int]
) -> None:
    use_case, store, repository = make_batched(count, batch_size)

    result = use_case.execute(KEY)

    assert repository.batch_sizes == expected
    assert (result.created, result.duplicates) == (count, 0)
    assert set(store.objects) == {"processed/batch.csv"}


def test_an_outage_in_a_later_batch_keeps_the_file_and_the_retry_creates_no_duplicates() -> None:
    use_case, store, repository = make_batched(7, 3)
    repository.fail_on_batch = 2

    with pytest.raises(InfrastructureError):
        use_case.execute(KEY)

    assert set(store.objects) == {KEY}
    assert len(repository.rows) == 3
    repository.fail_on_batch = None

    result = use_case.execute(KEY)

    assert (result.created, result.duplicates) == (4, 3)
    assert len(repository.rows) == 7
    assert set(store.objects) == {"processed/batch.csv"}


def test_a_file_at_the_size_ceiling_is_ingested_in_bounded_batches() -> None:
    body = HEADER + "".join(f"add,{n},2,2026-10-01T10:00:00Z\n" for n in range(100000))
    assert len(body) < MAX_FILE_BYTES
    store = InMemoryObjectStore({KEY: body.encode()})
    repository = InMemoryCalculationRepository()
    use_case = IngestCsv(store, repository, RecordingNotifier(), "s", "dev", 500)

    use_case.execute(KEY)

    assert max(repository.batch_sizes) == 500
    assert len(repository.batch_sizes) == 200
