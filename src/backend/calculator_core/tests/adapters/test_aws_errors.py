import json
import logging

import boto3
import pytest
from botocore.exceptions import ClientError

from calculator_core.adapters.outbound.aws_errors import describe_error
from calculator_core.adapters.outbound.sns_notifier import SnsErrorNotifier
from calculator_core.application.errors import InfrastructureError
from calculator_core.application.ports.error_notifier import InternalErrorReport
from calculator_core.config import migrate_job
from calculator_core.config.observability import JsonFormatter

LEAK = "hunter2-secret-value"


def _client_error(code: str = "AccessDeniedException") -> ClientError:
    response = {"Error": {"Code": code, "Message": f"password={LEAK}"}}
    return ClientError(response, "GetSecretValue")


def test_describe_error_has_type_code_and_operation_but_not_the_message() -> None:
    text = describe_error(_client_error())

    assert text == "ClientError code=AccessDeniedException operation=GetSecretValue"
    assert LEAK not in text


def test_describe_error_for_other_exceptions_is_the_type_only() -> None:
    assert describe_error(RuntimeError(LEAK)) == "RuntimeError"


def test_migration_log_names_the_aws_code_and_operation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(settings: object, client: object) -> list[str]:
        raise _client_error("ResourceNotFoundException")

    monkeypatch.setattr(migrate_job, "run_migrations", fail)
    monkeypatch.setattr(boto3, "client", lambda *a, **k: object())
    env = {
        "AWS_REGION": "us-east-2",
        "MASTER_SECRET_ARN": "m",
        "APP_SECRET_ARN": "a",
        "DB_INSTANCE_HOST": "h",
        "DB_NAME": "d",
        "SSL_ROOT_CERT": "/c.pem",
    }

    assert migrate_job.run_migrate(env) == 1

    output = capsys.readouterr().out
    assert "ClientError code=ResourceNotFoundException operation=GetSecretValue" in output
    assert LEAK not in output


def test_notifier_failure_log_names_the_aws_code_without_the_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Failing:
        def publish(self, **kwargs: object) -> None:
            raise _client_error("AuthorizationError")

    report = InternalErrorReport("svc", "dev", "X", "boom", "corr")

    with caplog.at_level(logging.ERROR):
        SnsErrorNotifier(Failing(), "arn").notify(report)

    assert "code=AuthorizationError" in caplog.text
    assert LEAK not in caplog.text


def test_json_log_of_an_infrastructure_error_carries_the_cause_code_only() -> None:
    try:
        raise InfrastructureError("The application secret could not be read.", _client_error())
    except InfrastructureError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "msg", None, sys.exc_info())

    entry = json.loads(JsonFormatter("svc", "dev").format(record))

    assert entry["aws_error"] == "ClientError code=AccessDeniedException operation=GetSecretValue"
    assert LEAK not in json.dumps(entry)
