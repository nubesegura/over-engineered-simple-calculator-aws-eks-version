import logging

import pytest

from calculator_core.adapters.outbound.postgres_repository import PostgresCalculationRepository
from calculator_core.config import container as container_module
from calculator_core.config.container import build_container
from calculator_core.config.settings import BACKEND_NAME, SettingsError, load_settings

FULL = {
    "AWS_REGION": "us-east-2",
    "ALERTS_TOPIC_ARN": "arn:aws:sns:us-east-2:000000000000:alerts",
    "APP_SECRET_ARN": "arn:aws:secretsmanager:us-east-2:000000000000:secret:app",
    "DB_HOST": "db.internal",
    "DB_PORT": "5432",
    "DB_NAME": "calculator",
    "ALLOWED_ORIGIN": "https://app.example.test",
    "LOG_LEVEL": "debug",
    "SSL_ROOT_CERT": "/app/rds-global-bundle.pem",
}
REQUIRED = [name for name in FULL if name not in ("DB_PORT", "LOG_LEVEL")]
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
