import signal
import threading
from typing import Any

import pytest

from calculator_core.config import ingest_worker
from calculator_core.config.settings import SettingsError, load_ingest_settings

ENV = {
    "AWS_REGION": "us-east-2",
    "ALERTS_TOPIC_ARN": "arn:aws:sns:us-east-2:000000000000:alerts",
    "APP_SECRET_ARN": "arn:aws:secretsmanager:us-east-2:000000000000:secret:app",
    "DB_HOST": "db.internal",
    "DB_NAME": "calculator",
    "SSL_ROOT_CERT": "/app/db-trust-bundle.pem",
    "INGEST_QUEUE_URL": "https://sqs.us-east-2.amazonaws.com/000000000000/ingest",
    "INGEST_BUCKET": "ingest-bucket",
}


def test_settings_are_loaded_without_the_http_variables() -> None:
    settings = load_ingest_settings({**ENV, "DB_PORT": "5433"})

    assert (settings.queue_url, settings.bucket, settings.db_port) == (
        ENV["INGEST_QUEUE_URL"],
        "ingest-bucket",
        5433,
    )
    assert settings.service_name == "calculator-ingest"


def test_settings_name_every_missing_variable() -> None:
    env = {k: v for k, v in ENV.items() if k not in ("INGEST_QUEUE_URL", "INGEST_BUCKET")}

    with pytest.raises(SettingsError) as raised:
        load_ingest_settings({**env, "LOG_LEVEL": "loud"})

    message = str(raised.value)
    assert "INGEST_QUEUE_URL" in message
    assert "INGEST_BUCKET" in message
    assert "LOG_LEVEL" in message


def test_invalid_settings_exit_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert ingest_worker.run_ingest_worker({}) == 1

    assert "INGEST_QUEUE_URL" in capsys.readouterr().out


def test_worker_runs_until_stopped_and_start_failure_exits_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers: dict[int, Any] = {}
    monkeypatch.setattr(
        signal, "signal", lambda number, handler: handlers.update({number: handler})
    )

    class Worker:
        def run_forever(self, stop: threading.Event) -> None:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            assert stop.is_set()

    monkeypatch.setattr(ingest_worker, "build_ingest_worker", lambda settings: Worker())
    assert ingest_worker.run_ingest_worker(ENV) == 0

    def refuse(settings: object) -> None:
        raise RuntimeError("password=hunter2")

    monkeypatch.setattr(ingest_worker, "build_ingest_worker", refuse)
    assert ingest_worker.run_ingest_worker(ENV) == 1


def test_ingest_tuning_defaults_and_overrides() -> None:
    default = load_ingest_settings(ENV)
    tuned = load_ingest_settings(
        {**ENV, "INGEST_BATCH_SIZE": "100", "ALERT_THROTTLE_SECONDS": "60"}
    )

    assert (default.batch_size, default.alert_throttle_seconds) == (500, 300)
    assert default.database.pool_timeout_seconds == 3
    assert (tuned.batch_size, tuned.alert_throttle_seconds) == (100, 60)


@pytest.mark.parametrize("bad", ["0", "5001", "many"])
def test_invalid_batch_size_stops_the_start(bad: str) -> None:
    with pytest.raises(SettingsError, match="INGEST_BATCH_SIZE"):
        load_ingest_settings({**ENV, "INGEST_BATCH_SIZE": bad})
