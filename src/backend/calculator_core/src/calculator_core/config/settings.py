"""Settings from environment variables, validated once at start.

A missing or invalid value stops the start with a message that names the variables (never
their values). The workload (`add`, `sub`, `mul`, `div`, `history`) comes from the command line.
"""

import logging
from collections.abc import Mapping
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
_DEFAULT_DB_PORT = 5432
_DEFAULT_LOG_LEVEL = "INFO"
_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


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
    )


def _parse_port(raw: str, problems: list[str]) -> int:
    if not raw:
        return _DEFAULT_DB_PORT
    if raw.isdigit() and 0 < int(raw) < 65536:
        return int(raw)
    problems.append("DB_PORT must be a number between 1 and 65535")
    return _DEFAULT_DB_PORT
