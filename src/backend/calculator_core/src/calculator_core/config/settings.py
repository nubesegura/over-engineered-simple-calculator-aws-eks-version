"""Settings from environment variables, validated once at start.

A missing or invalid value stops the start with a message that names the variables (never
their values). The workload (`add`, `sub`, `mul`, `div`, `history`) comes from the command line.

Optional tuning variables (integers, with these defaults and inclusive ranges):
DB_CONNECT_TIMEOUT_SECONDS=3 (1-30), DB_POOL_TIMEOUT_SECONDS=3 (1-30), DB_POOL_MAX_WAITING=20
(1-1000), ALERT_THROTTLE_SECONDS=300 (0-86400; 0 sends every alert) and, for the ingestion
worker only, INGEST_BATCH_SIZE=500 (1-5000).
"""

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass

# Names the implementation that answers; sent in every calculation response (never configurable).
BACKEND_NAME = "eks"
CALCULATION_WORKLOADS = ("add", "sub", "mul", "div")
HISTORY_WORKLOAD = "history"
WORKLOADS = (*CALCULATION_WORKLOADS, HISTORY_WORKLOAD)
_REQUIRED = (
    "AWS_REGION",
    "ALERTS_TOPIC_ARN",
    "APP_SECRET_ARN",
    "DB_HOST",
    "DB_NAME",
    "ALLOWED_ORIGIN",
    "SSL_ROOT_CERT",
)
INGEST_WORKLOAD = "ingest"
_INGEST_REQUIRED = (
    "AWS_REGION",
    "ALERTS_TOPIC_ARN",
    "APP_SECRET_ARN",
    "DB_HOST",
    "DB_NAME",
    "SSL_ROOT_CERT",
    "INGEST_QUEUE_URL",
    "INGEST_BUCKET",
)
_DEFAULT_DB_PORT = 5432
_DEFAULT_LOG_LEVEL = "INFO"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
DEFAULT_ALERT_THROTTLE_SECONDS = 300
DEFAULT_INGEST_BATCH_SIZE = 500


@dataclass(frozen=True)
class DatabaseTuning:
    """How long a request may wait for the database, and how many may queue for a connection."""

    connect_timeout_seconds: int = 3
    pool_timeout_seconds: int = 3
    pool_max_waiting: int = 20


class SettingsError(Exception):
    """The environment does not describe a valid configuration."""


@dataclass(frozen=True)
class Settings:
    workload: str
    environment: str
    aws_region: str
    alerts_topic_arn: str
    app_secret_arn: str
    db_host: str
    db_port: int
    db_name: str
    ssl_root_cert: str
    allowed_origin: str
    log_level: int
    cursor_key_secret_arn: str | None
    database: DatabaseTuning = DatabaseTuning()
    alert_throttle_seconds: int = DEFAULT_ALERT_THROTTLE_SECONDS

    @property
    def service_name(self) -> str:
        return f"calculator-{self.workload}"


def load_settings(workload: str, env: Mapping[str, str]) -> Settings:
    if workload not in WORKLOADS:
        raise SettingsError(f"Unknown workload. Use one of: {', '.join(WORKLOADS)}.")

    def value(name: str, default: str = "") -> str:
        return env.get(name, "").strip() or default

    problems = [f"{name} is required" for name in _REQUIRED if not value(name)]
    port = _parse_port(value("DB_PORT"), problems)
    level = value("LOG_LEVEL", _DEFAULT_LOG_LEVEL).upper()
    if level not in _LOG_LEVELS:
        problems.append(f"LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}")
    if value("ALLOWED_ORIGIN") == "*":
        problems.append("ALLOWED_ORIGIN must be an explicit origin, not '*'")
    database = _load_database_tuning(value, problems)
    throttle = _load_int(
        value, "ALERT_THROTTLE_SECONDS", DEFAULT_ALERT_THROTTLE_SECONDS, 0, 86400, problems
    )
    cursor_secret = value("CURSOR_KEY_SECRET_ARN") or None
    if workload == HISTORY_WORKLOAD and cursor_secret is None:
        problems.append("CURSOR_KEY_SECRET_ARN is required by the history workload")
    if problems:
        raise SettingsError("Invalid configuration: " + "; ".join(problems) + ".")
    return Settings(
        workload=workload,
        environment=value("ENVIRONMENT", "unknown"),
        aws_region=value("AWS_REGION"),
        alerts_topic_arn=value("ALERTS_TOPIC_ARN"),
        app_secret_arn=value("APP_SECRET_ARN"),
        db_host=value("DB_HOST"),
        db_port=port,
        db_name=value("DB_NAME"),
        ssl_root_cert=value("SSL_ROOT_CERT"),
        allowed_origin=value("ALLOWED_ORIGIN"),
        log_level=logging.getLevelNamesMapping()[level],
        cursor_key_secret_arn=cursor_secret,
        database=database,
        alert_throttle_seconds=throttle,
    )


@dataclass(frozen=True)
class IngestSettings:
    environment: str
    aws_region: str
    alerts_topic_arn: str
    app_secret_arn: str
    db_host: str
    db_port: int
    db_name: str
    ssl_root_cert: str
    queue_url: str
    bucket: str
    log_level: int
    database: DatabaseTuning = DatabaseTuning()
    alert_throttle_seconds: int = DEFAULT_ALERT_THROTTLE_SECONDS
    batch_size: int = DEFAULT_INGEST_BATCH_SIZE

    @property
    def service_name(self) -> str:
        return f"calculator-{INGEST_WORKLOAD}"


def load_ingest_settings(env: Mapping[str, str]) -> IngestSettings:
    def value(name: str, default: str = "") -> str:
        return env.get(name, "").strip() or default

    problems = [f"{name} is required" for name in _INGEST_REQUIRED if not value(name)]
    port = _parse_port(value("DB_PORT"), problems)
    level = value("LOG_LEVEL", _DEFAULT_LOG_LEVEL).upper()
    if level not in _LOG_LEVELS:
        problems.append(f"LOG_LEVEL must be one of {', '.join(_LOG_LEVELS)}")
    database = _load_database_tuning(value, problems)
    throttle = _load_int(
        value, "ALERT_THROTTLE_SECONDS", DEFAULT_ALERT_THROTTLE_SECONDS, 0, 86400, problems
    )
    batch_size = _load_int(value, "INGEST_BATCH_SIZE", DEFAULT_INGEST_BATCH_SIZE, 1, 5000, problems)
    if problems:
        raise SettingsError("Invalid configuration: " + "; ".join(problems) + ".")
    return IngestSettings(
        environment=value("ENVIRONMENT", "unknown"),
        aws_region=value("AWS_REGION"),
        alerts_topic_arn=value("ALERTS_TOPIC_ARN"),
        app_secret_arn=value("APP_SECRET_ARN"),
        db_host=value("DB_HOST"),
        db_port=port,
        db_name=value("DB_NAME"),
        ssl_root_cert=value("SSL_ROOT_CERT"),
        queue_url=value("INGEST_QUEUE_URL"),
        bucket=value("INGEST_BUCKET"),
        log_level=logging.getLevelNamesMapping()[level],
        database=database,
        alert_throttle_seconds=throttle,
        batch_size=batch_size,
    )


def _load_int(
    value: Callable[[str], str],
    name: str,
    default: int,
    low: int,
    high: int,
    problems: list[str],
) -> int:
    raw = value(name)
    if not raw:
        return default
    if raw.isdigit() and low <= int(raw) <= high:
        return int(raw)
    problems.append(f"{name} must be a whole number between {low} and {high}")
    return default


def _load_database_tuning(value: Callable[[str], str], problems: list[str]) -> DatabaseTuning:
    defaults = DatabaseTuning()
    return DatabaseTuning(
        connect_timeout_seconds=_load_int(
            value, "DB_CONNECT_TIMEOUT_SECONDS", defaults.connect_timeout_seconds, 1, 30, problems
        ),
        pool_timeout_seconds=_load_int(
            value, "DB_POOL_TIMEOUT_SECONDS", defaults.pool_timeout_seconds, 1, 30, problems
        ),
        pool_max_waiting=_load_int(
            value, "DB_POOL_MAX_WAITING", defaults.pool_max_waiting, 1, 1000, problems
        ),
    )


def _parse_port(raw: str, problems: list[str]) -> int:
    if not raw:
        return _DEFAULT_DB_PORT
    if raw.isdigit() and 0 < int(raw) < 65536:
        return int(raw)
    problems.append("DB_PORT must be a number between 1 and 65535")
    return _DEFAULT_DB_PORT
