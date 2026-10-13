import logging
from typing import Any

import boto3
import pytest

from calculator_core.adapters.inbound.sqs_worker import WAIT_SECONDS
from calculator_core.adapters.outbound.postgres_repository import PostgresCalculationRepository
from calculator_core.config import container as container_module
from calculator_core.config.container import build_container
from calculator_core.config.settings import (
    BACKEND_NAME,
    SettingsError,
    load_ingest_settings,
    load_settings,
)

FULL = {
    "AWS_REGION": "us-east-2",
    "ALERTS_TOPIC_ARN": "arn:aws:sns:us-east-2:000000000000:alerts",
    "APP_SECRET_ARN": "arn:aws:secretsmanager:us-east-2:000000000000:secret:app",
    "DB_HOST": "db.internal",
    "DB_PORT": "5432",
    "DB_NAME": "calculator",
    "ALLOWED_ORIGIN": "https://app.example.test",
    "LOG_LEVEL": "debug",
    "SSL_ROOT_CERT": "/app/db-trust-bundle.pem",
}
REQUIRED = [name for name in FULL if name not in ("DB_PORT", "LOG_LEVEL")]
INGEST_ENV = {
    **{k: v for k, v in FULL.items() if k != "ALLOWED_ORIGIN"},
    "INGEST_QUEUE_URL": "https://sqs.us-east-2.amazonaws.com/000000000000/ingest",
    "INGEST_BUCKET": "ingest-bucket",
}
CURSOR_ARN = "arn:aws:secretsmanager:us-east-2:000000000000:secret:cursor"


def test_complete_environment_is_loaded() -> None:
    settings = load_settings("add", FULL)

    assert settings.service_name == "calculator-add"
    assert settings.db_port == 5432
    assert settings.allowed_origin == "https://app.example.test"
    assert settings.log_level == logging.DEBUG
    assert settings.cursor_key_secret_arn is None


@pytest.mark.parametrize("missing", REQUIRED)
def test_missing_required_value_stops_the_start(missing: str) -> None:
    env = {name: value for name, value in FULL.items() if name != missing}

    with pytest.raises(SettingsError, match=missing):
        load_settings("add", env)


def test_error_lists_every_problem_and_never_echoes_values() -> None:
    env = {**FULL, "DB_PORT": "not-a-number", "ALLOWED_ORIGIN": "*", "LOG_LEVEL": "loud"}
    del env["DB_NAME"]

    with pytest.raises(SettingsError) as raised:
        load_settings("add", env)

    message = str(raised.value)
    for name in ("DB_NAME", "DB_PORT", "ALLOWED_ORIGIN", "LOG_LEVEL"):
        assert name in message
    assert "not-a-number" not in message


def test_unknown_workload_is_rejected() -> None:
    with pytest.raises(SettingsError, match="workload"):
        load_settings("pow", FULL)


def test_history_refuses_to_start_without_the_cursor_key_secret() -> None:
    with pytest.raises(SettingsError, match="CURSOR_KEY_SECRET_ARN"):
        load_settings("history", FULL)

    settings = load_settings("history", {**FULL, "CURSOR_KEY_SECRET_ARN": CURSOR_ARN})
    assert settings.cursor_key_secret_arn == CURSOR_ARN


def test_container_wires_postgres_and_only_the_routes_of_the_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr(container_module, "create_pool", lambda *args, **kwargs: object())

    container = build_container(load_settings("sub", FULL))

    assert isinstance(container.perform_calculation.repository, PostgresCalculationRepository)  # type: ignore[union-attr]
    assert container.get_history is None


def test_history_container_reads_the_cursor_key_from_its_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr(container_module, "create_pool", lambda *args, **kwargs: object())
    keys: list[str] = []

    class FakeKeyProvider:
        def __init__(self, secret_id: str, client: object) -> None:
            keys.append(secret_id)

        def get(self) -> bytes:
            return b"0123456789abcdef-test-key"

    monkeypatch.setattr(container_module, "SecretsManagerCursorKeyProvider", FakeKeyProvider)

    container = build_container(
        load_settings("history", {**FULL, "CURSOR_KEY_SECRET_ARN": CURSOR_ARN})
    )

    assert container.get_history is not None
    assert container.perform_calculation is None
    assert keys == [CURSOR_ARN]


def test_backend_name_is_a_lowercase_constant_of_this_repository() -> None:
    assert BACKEND_NAME == "eks"


def test_tuning_defaults_are_short_database_waits_and_five_minute_alert_interval() -> None:
    settings = load_settings("add", FULL)

    assert settings.database.connect_timeout_seconds == 3
    assert settings.database.pool_timeout_seconds == 3
    assert settings.database.pool_max_waiting == 20
    assert settings.alert_throttle_seconds == 300


def test_tuning_values_are_read_from_the_environment() -> None:
    env = {
        **FULL,
        "DB_CONNECT_TIMEOUT_SECONDS": "5",
        "DB_POOL_TIMEOUT_SECONDS": "7",
        "DB_POOL_MAX_WAITING": "50",
        "ALERT_THROTTLE_SECONDS": "0",
    }

    settings = load_settings("add", env)

    assert (settings.database.connect_timeout_seconds, settings.database.pool_timeout_seconds) == (
        5,
        7,
    )
    assert settings.database.pool_max_waiting == 50
    assert settings.alert_throttle_seconds == 0


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("DB_CONNECT_TIMEOUT_SECONDS", "0"),
        ("DB_CONNECT_TIMEOUT_SECONDS", "31"),
        ("DB_POOL_TIMEOUT_SECONDS", "-1"),
        ("DB_POOL_TIMEOUT_SECONDS", "soon"),
        ("DB_POOL_MAX_WAITING", "0"),
        ("DB_POOL_MAX_WAITING", "1001"),
        ("ALERT_THROTTLE_SECONDS", "86401"),
        ("ALERT_THROTTLE_SECONDS", "1.5"),
    ],
)
def test_out_of_range_tuning_stops_the_start_naming_the_variable_only(name: str, bad: str) -> None:
    with pytest.raises(SettingsError, match=f"{name} must be a whole number"):
        load_settings("add", {**FULL, name: bad})


def test_every_aws_client_has_bounded_timeouts_and_the_queue_client_outlasts_long_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[str, Any] = {}

    def fake_client(service: str, **kwargs: Any) -> object:
        created[service] = kwargs["config"]
        return object()

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setattr(container_module, "create_pool", lambda *args, **kwargs: object())
    monkeypatch.setattr(container_module, "SqsWorker", lambda *args: object())
    build_container(load_settings("sub", FULL))
    container_module.build_ingest_worker(load_ingest_settings(INGEST_ENV))

    assert set(created) == {"secretsmanager", "sns", "s3", "sqs"}
    for service in ("secretsmanager", "sns", "s3"):
        config = created[service]
        assert (config.connect_timeout, config.read_timeout) == (2, 3)
        assert config.retries["total_max_attempts"] == 2
    assert created["sqs"].read_timeout > WAIT_SECONDS


def test_the_pool_gets_the_configured_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    seen: dict[str, Any] = {}
    monkeypatch.setattr(
        container_module, "create_pool", lambda *args, **kwargs: seen.update(kwargs) or object()
    )

    build_container(load_settings("sub", {**FULL, "DB_POOL_MAX_WAITING": "11"}))

    assert seen == {"connect_timeout": 3, "pool_timeout": 3, "max_waiting": 11}
