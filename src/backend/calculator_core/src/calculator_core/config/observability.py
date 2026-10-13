"""Structured JSON logging with a correlation id and the caller's `sub` claim.

Exceptions are logged by type, cause type and traceback frames (file, line, function) only:
messages of driver or SDK errors are not guaranteed to be free of sensitive values, and
frames hold no values.
"""

import json
import logging
import re
import sys
import traceback
from contextvars import ContextVar
from datetime import UTC, datetime
from types import TracebackType

from botocore.exceptions import ClientError

from calculator_core.adapters.outbound.aws_errors import describe_error

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_subject: ContextVar[str | None] = ContextVar("subject", default=None)


# Libraries whose DEBUG records carry request headers (temporary AWS credentials, signatures) or SQL
# parameters: they never log below WARNING, whatever LOG_LEVEL says.
QUIET_LIBRARY_LOGGERS = ("botocore", "boto3", "urllib3", "s3transfer", "psycopg")
REDACTED = "[REDACTED]"
_SENSITIVE = re.compile(
    r"(?i)(x-amz-security-token|authorization|aws_session_token|aws_secret_access_key|"
    r"secret_access_key|security_token|sessiontoken|secretaccesskey)([\"']?\s*[:=]\s*b?[\"']?)[^\"'\n]+"
    r"|AWS4-HMAC-SHA256\s+Credential=[^\"'\n]+"
)


def redact(text: str) -> str:
    """Replace credential-looking values (token or authorization headers) in a message."""
    return _SENSITIVE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}" if m.group(1) else REDACTED, text
    )


class RedactingFilter(logging.Filter):
    """Last line of defence: scrub credential-looking values from every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def set_request_context(correlation_id: str | None, subject: str | None = None) -> None:
    _correlation_id.set(correlation_id)
    _subject.set(subject)


def current_correlation_id() -> str | None:
    return _correlation_id.get()


def _exception_fields(
    error: BaseException | None, trace: TracebackType | None
) -> dict[str, object]:
    fields: dict[str, object] = {"exception_type": type(error).__name__}
    cause = getattr(error, "original_error", None) or getattr(error, "__cause__", None)
    if cause is not None:
        fields["cause_type"] = type(cause).__name__
        if isinstance(cause, ClientError):
            fields["aws_error"] = describe_error(cause)
    fields["frames"] = [
        {"file": frame.filename, "line": frame.lineno, "function": frame.name}
        for frame in traceback.extract_tb(trace)
    ]
    return fields


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, environment: str) -> None:
        super().__init__()
        self._service = service
        self._environment = environment

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self._service,
            "environment": self._environment,
        }
        correlation_id, subject = _correlation_id.get(), _subject.get()
        if correlation_id:
            entry["correlation_id"] = correlation_id
        if subject:
            entry["sub"] = subject
        if record.exc_info and record.exc_info[0] is not None:
            entry.update(_exception_fields(record.exc_info[1], record.exc_info[2]))
        return json.dumps(entry)


def configure_logging(service: str, environment: str, level: int | str = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service, environment))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in QUIET_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
