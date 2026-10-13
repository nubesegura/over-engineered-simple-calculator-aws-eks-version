import json
import logging

import pytest

from calculator_core.application.errors import InfrastructureError
from calculator_core.config.observability import (
    QUIET_LIBRARY_LOGGERS,
    REDACTED,
    JsonFormatter,
    RedactingFilter,
    configure_logging,
    set_request_context,
)


def _format(record: logging.LogRecord) -> dict[str, object]:
    result: dict[str, object] = json.loads(JsonFormatter("calc-add", "dev").format(record))
    return result


def _record(message: str, exc_info: tuple[type[BaseException], BaseException, None] | None = None):  # type: ignore[no-untyped-def]
    return logging.LogRecord("test", logging.ERROR, __file__, 1, message, None, exc_info)


def test_entry_carries_correlation_id_and_sub_when_set() -> None:
    set_request_context("corr-1", "user-sub")

    entry = _format(_record("hello"))

    assert entry["correlation_id"] == "corr-1"
    assert entry["sub"] == "user-sub"
    assert entry["service"] == "calc-add"
    set_request_context(None)


def _raise_wrapped() -> BaseException:
    def inner() -> None:
        raise ConnectionError("password=hunter2")

    try:
        try:
            inner()
        except ConnectionError as cause:
            raise InfrastructureError("token=abc123", cause) from cause
    except InfrastructureError as error:
        return error
    raise AssertionError("unreachable")


def test_entry_has_cause_type_and_frames_but_no_message_or_value() -> None:
    error = _raise_wrapped()

    entry = _format(_record("failed", (type(error), error, error.__traceback__)))  # type: ignore[arg-type]

    assert entry["exception_type"] == "InfrastructureError"
    assert entry["cause_type"] == "ConnectionError"
    frames = entry["frames"]
    assert isinstance(frames, list)
    assert set(frames[-1]) == {"file", "line", "function"}
    assert frames[-1]["function"] == "_raise_wrapped"
    assert frames[-1]["file"].endswith("test_observability.py")
    dumped = json.dumps(entry)
    assert "hunter2" not in dumped
    assert "abc123" not in dumped
    assert "raise " not in dumped


def test_entry_omits_context_when_absent_and_logs_exception_type_only() -> None:
    set_request_context(None)
    error = ValueError("password=hunter2")

    entry = _format(_record("failed", (ValueError, error, None)))

    assert "sub" not in entry
    assert "correlation_id" not in entry
    assert entry["exception_type"] == "ValueError"
    assert "hunter2" not in json.dumps(entry)


def _configure_and_capture(
    level: str, capsys: pytest.CaptureFixture[str]
) -> list[dict[str, object]]:
    saved = (logging.getLogger().handlers[:], logging.getLogger().level)
    try:
        configure_logging("calc-add", "dev", level)
        for name in (
            "botocore.endpoint",
            "boto3.resources",
            "urllib3.connectionpool",
            "s3transfer.tasks",
            "psycopg",
        ):
            logging.getLogger(name).debug(
                "Request headers: {'x-amz-security-token': 'IQoJb3JpZ2luX2VjEXAMPLE'}"
            )
        logging.getLogger("calculator_core.app").debug("application debug record")
        return [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    finally:
        logging.getLogger().handlers[:] = saved[0]
        logging.getLogger().setLevel(saved[1])


def test_library_loggers_never_log_below_warning_even_with_debug(
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = _configure_and_capture("DEBUG", capsys)

    assert [e["logger"] for e in entries] == ["calculator_core.app"]
    for name in QUIET_LIBRARY_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
    assert set(QUIET_LIBRARY_LOGGERS) >= {"botocore", "boto3", "urllib3", "s3transfer", "psycopg"}


@pytest.mark.parametrize(
    "message",
    [
        "{'x-amz-security-token': 'IQoJb3JpZ2luX2VjEXAMPLE'}",
        "{'Authorization': b'AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/2026/s3, Signature=abc'}",
        "AWS4-HMAC-SHA256 Credential=ASIAEXAMPLE/20261013/us-east-2/sqs/aws4_request",
        "AWS_SESSION_TOKEN=IQoJb3JpZ2luX2VjEXAMPLE",
        "aws_secret_access_key = wJalrEXAMPLEKEY",
    ],
)
def test_credential_looking_values_are_redacted_from_any_record(message: str) -> None:
    record = _record("%s")
    record.args = (message,)

    assert RedactingFilter().filter(record)
    entry = _format(record)

    text = json.dumps(entry)
    for leaked in ("EXAMPLE", "Signature=abc", "wJalr"):
        assert leaked not in text
    assert REDACTED in text


def test_normal_messages_are_not_changed() -> None:
    record = _record("Calculation done for operation add")

    assert RedactingFilter().filter(record)
    assert _format(record)["message"] == "Calculation done for operation add"
