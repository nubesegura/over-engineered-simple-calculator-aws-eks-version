"""Ingestion of one CSV file: validate the key, parse row by row, save and move the file.

Business outcomes (valid file, rejected file, invalid rows) are returned or reported; only an
infrastructure failure raises, so the caller leaves the queue message for a retry. Saving is
idempotent (the id derives from the row content), hence a retry never duplicates a row.
"""

import csv
import io
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from uuid import NAMESPACE_URL, UUID, uuid5

from calculator_core.application.ports.calculation_repository import CalculationRepository
from calculator_core.application.ports.error_notifier import ErrorNotifier, InternalErrorReport
from calculator_core.application.ports.object_store import ObjectStore
from calculator_core.domain.calculation import Calculation
from calculator_core.domain.errors import DomainError
from calculator_core.domain.operation import Operation

INCOMING_PREFIX = "incoming/"
PROCESSED_PREFIX = "processed/"
REJECTED_PREFIX = "rejected/"
REPORTS_PREFIX = "reports/"
MAX_FILE_BYTES = 5 * 1024 * 1024
HEADER = ["operation", "operand_a", "operand_b", "occurred_at"]
MAX_REPORTED_PROBLEMS = 1000
# One file name, no directory part; a ".." inside the name is rejected separately.
_KEY_PATTERN = re.compile(r"incoming/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.csv")
_ID_NAMESPACE = uuid5(NAMESPACE_URL, "urn:calculator:ingest-row")


class IngestStatus(Enum):
    PROCESSED = "processed"
    REJECTED = "rejected"
    HOSTILE_KEY = "hostile_key"
    ALREADY_HANDLED = "already_handled"


@dataclass(frozen=True)
class IngestResult:
    status: IngestStatus
    created: int = 0
    duplicates: int = 0
    invalid: int = 0


class _RejectedFileError(Exception):
    """The whole file is refused (not CSV, wrong header, too large)."""


def is_valid_key(key: str) -> bool:
    return _KEY_PATTERN.fullmatch(key) is not None and ".." not in key


def row_id(operation: Operation, a: Decimal, b: Decimal, occurred_at: datetime) -> UUID:
    """Stable id from the normalized content of a row."""
    content = f"{operation.value}|{a.normalize():f}|{b.normalize():f}|{occurred_at.isoformat()}"
    return uuid5(_ID_NAMESPACE, content)


class IngestCsv:
    def __init__(
        self,
        store: ObjectStore,
        repository: CalculationRepository,
        notifier: ErrorNotifier,
        service: str,
        environment: str,
    ) -> None:
        self._store = store
        self._repository = repository
        self._notifier = notifier
        self._service = service
        self._environment = environment

    def execute(self, key: str) -> IngestResult:
        if not is_valid_key(key):
            self._alert(
                "HostileObjectKey", "An object key outside incoming/<name>.csv was ignored."
            )
            return IngestResult(IngestStatus.HOSTILE_KEY)
        raw = self._store.read(key, MAX_FILE_BYTES)
        if raw is None:
            return IngestResult(IngestStatus.ALREADY_HANDLED)
        try:
            rows, problems = _parse(raw)
        except _RejectedFileError as error:
            return self._reject(key, str(error))
        created = sum(1 for row in rows if self._repository.save(row))
        if problems:
            self._store.put(_report_key(key), _report_text(problems))
        self._store.move(key, PROCESSED_PREFIX + key.removeprefix(INCOMING_PREFIX))
        return IngestResult(IngestStatus.PROCESSED, created, len(rows) - created, len(problems))

    def _reject(self, key: str, reason: str) -> IngestResult:
        self._store.put(_report_key(key), _report_text([reason]))
        self._store.move(key, REJECTED_PREFIX + key.removeprefix(INCOMING_PREFIX))
        self._alert("FileRejected", reason)
        return IngestResult(IngestStatus.REJECTED)

    def _alert(self, error_type: str, message: str) -> None:
        self._notifier.notify(
            InternalErrorReport(self._service, self._environment, error_type, message, None)
        )


def _report_key(key: str) -> str:
    return REPORTS_PREFIX + key.removeprefix(INCOMING_PREFIX) + ".txt"


def _report_text(lines: list[str]) -> bytes:
    shown = lines[:MAX_REPORTED_PROBLEMS]
    if len(lines) > len(shown):
        shown.append(f"... {len(lines) - len(shown)} more problem(s) not listed.")
    return ("\n".join(shown) + "\n").encode()


def _parse(raw: bytes) -> tuple[list[Calculation], list[str]]:
    if len(raw) > MAX_FILE_BYTES:
        raise _RejectedFileError("The file is larger than 5 MB.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _RejectedFileError("The file is not valid UTF-8 text.") from error
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    rows: dict[UUID, Calculation] = {}
    problems: list[str] = []
    try:
        if next(reader, None) != HEADER:
            raise _RejectedFileError(f"The header must be: {','.join(HEADER)}.")
        for fields in reader:
            if not fields:
                continue
            try:
                calculation = _to_calculation(fields)
            except (DomainError, ValueError) as error:
                problems.append(f"line {reader.line_num}: {error}")
            else:
                rows.setdefault(calculation.id, calculation)
    except csv.Error as error:
        raise _RejectedFileError("The file is not valid CSV.") from error
    return list(rows.values()), problems


def _to_calculation(fields: list[str]) -> Calculation:
    if len(fields) != len(HEADER):
        raise ValueError(f"expected {len(HEADER)} columns, found {len(fields)}")
    name, text_a, text_b, text_at = (field.strip() for field in fields)
    try:
        operation = Operation(name)
    except ValueError:
        raise ValueError("unknown operation") from None
    a, b = _decimal(text_a, "operand_a"), _decimal(text_b, "operand_b")
    occurred_at = _utc_timestamp(text_at)
    return Calculation(
        id=row_id(operation, a, b, occurred_at),
        operation=operation,
        operand_a=a,
        operand_b=b,
        result=operation.calculate(a, b),
        occurred_at=occurred_at,
    )


def _decimal(text: str, column: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{column} is not a number") from None


def _utc_timestamp(text: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError("occurred_at is not an ISO-8601 timestamp") from None
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("occurred_at must be in UTC (Z or +00:00)")
    return parsed.astimezone(UTC)
