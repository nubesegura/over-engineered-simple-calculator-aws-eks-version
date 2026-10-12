import boto3
import pytest

from calculator_core.config import cli, migrate_job
from calculator_core.config.settings import SettingsError

FULL_MIGRATE_ENV = {
    "AWS_REGION": "us-east-2",
    "MASTER_SECRET_ARN": "arn:aws:secretsmanager:us-east-2:000000000000:secret:master",
    "APP_SECRET_ARN": "arn:aws:secretsmanager:us-east-2:000000000000:secret:app",
    "DB_INSTANCE_HOST": "db.instance.internal",
    "DB_NAME": "calculator",
    "SSL_ROOT_CERT": "/app/rds-global-bundle.pem",
}


@pytest.mark.parametrize("workload", ["add", "sub", "mul", "div", "history"])
def test_http_workloads_start_the_https_service(
    monkeypatch: pytest.MonkeyPatch, workload: str
) -> None:
    started: list[str] = []
    monkeypatch.setattr(cli, "run_http_service", started.append)

    assert cli.main([workload]) == 0
    assert started == [workload]


def test_migrate_runs_the_migration_and_returns_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "run_migrate", lambda: 7)

    assert cli.main(["migrate"]) == 7


def test_ingest_is_a_clear_error_for_now(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["ingest"]) == 2
    assert "not available" in capsys.readouterr().err


@pytest.mark.parametrize("args", [[], ["pow"], ["add", "extra"]])
def test_unknown_or_missing_command_prints_usage(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(args) == 2
    assert "Usage: calculator" in capsys.readouterr().err


def test_invalid_settings_stop_the_start_with_a_named_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def refuse(workload: str) -> None:
        raise SettingsError("Invalid configuration: CURSOR_KEY_SECRET_ARN is required.")

    monkeypatch.setattr(cli, "run_http_service", refuse)

    assert cli.main(["history"]) == 1
    assert "CURSOR_KEY_SECRET_ARN" in capsys.readouterr().err


def test_migrate_settings_name_every_missing_variable() -> None:
    env = {k: v for k, v in FULL_MIGRATE_ENV.items() if k not in ("DB_NAME", "SSL_ROOT_CERT")}

    with pytest.raises(SettingsError) as raised:
        migrate_job.load_migrate_settings(env)

    assert "DB_NAME" in str(raised.value)
    assert "SSL_ROOT_CERT" in str(raised.value)


def test_migrate_settings_read_the_instance_host() -> None:
    settings = migrate_job.load_migrate_settings({**FULL_MIGRATE_ENV, "DB_PORT": "5433"})

    assert settings.db_host == "db.instance.internal"
    assert settings.db_port == 5433


def test_migration_failure_is_logged_by_type_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(settings: object, client: object) -> list[str]:
        raise RuntimeError("password=hunter2")

    monkeypatch.setattr(migrate_job, "run_migrations", fail)
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: object())

    assert migrate_job.run_migrate(FULL_MIGRATE_ENV) == 1

    output = capsys.readouterr().out
    assert "RuntimeError" in output
    assert "hunter2" not in output


def test_migration_with_a_bad_environment_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert migrate_job.run_migrate({}) == 1

    assert "MASTER_SECRET_ARN" in capsys.readouterr().out
