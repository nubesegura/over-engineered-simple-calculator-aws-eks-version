from starlette.testclient import TestClient

from calculator_core.application.use_cases.get_history import GetHistory
from calculator_core.application.use_cases.perform_calculation import PerformCalculation
from calculator_core.config.container import Container
from calculator_core.config.http_service import PORT, build_service_app
from calculator_core.config.settings import load_settings
from calculator_core.domain.history import CursorCodec
from tests.entrypoints.test_http_app import RecordingNotifier, SequentialIds
from tests.fakes import CURSOR_KEY, FixedClock, InMemoryCalculationRepository

ENV = {
    "AWS_REGION": "us-east-2",
    "ALERTS_TOPIC_ARN": "arn:aws:sns:us-east-2:000000000000:alerts",
    "APP_SECRET_ARN": "arn:aws:secretsmanager:us-east-2:000000000000:secret:app",
    "DB_HOST": "db.internal",
    "DB_NAME": "calculator",
    "ALLOWED_ORIGIN": "https://app.example.test",
    "SSL_ROOT_CERT": "/app/rds-global-bundle.pem",
    "CURSOR_KEY_SECRET_ARN": "arn:aws:secretsmanager:us-east-2:000000000000:secret:cursor",
}


def _client(workload: str) -> TestClient:
    repository = InMemoryCalculationRepository()
    container = Container(
        settings=load_settings(workload, ENV),
        notifier=RecordingNotifier(),
        perform_calculation=(
            None
            if workload == "history"
            else PerformCalculation(repository, FixedClock(), SequentialIds())
        ),
        get_history=GetHistory(repository, CursorCodec(CURSOR_KEY))
        if workload == "history"
        else None,
    )
    return TestClient(build_service_app(container), raise_server_exceptions=False)


def test_the_mul_workload_serves_only_mul_and_answers_as_eks() -> None:
    client = _client("mul")

    response = client.post("/api/eks/v1/mul", json={"a": 2, "b": 3})

    assert response.json()["result"] == "6"
    assert response.json()["backend"] == "eks"
    assert response.headers["access-control-allow-origin"] == "https://app.example.test"
    assert client.post("/api/v1/add", json={"a": 1, "b": 1}).status_code == 404


def test_the_history_workload_serves_history_and_health() -> None:
    client = _client("history")

    assert client.get("/api/eks/v1/history").json() == {"items": [], "next_cursor": None}
    assert client.get("/health").status_code == 200
    assert PORT == 8443
