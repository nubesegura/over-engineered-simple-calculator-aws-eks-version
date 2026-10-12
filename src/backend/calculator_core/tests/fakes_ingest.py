"""In-memory fakes of the ingestion ports."""

from calculator_core.application.errors import InfrastructureError
from calculator_core.application.ports.error_notifier import InternalErrorReport


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
