import json
import threading
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote_plus

import boto3
import pytest
from moto import mock_aws

from calculator_core.adapters.inbound.sqs_worker import SqsWorker


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Any, str]]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr("calculator_core.adapters.inbound.sqs_worker.WAIT_SECONDS", 0)
    with mock_aws():
        client = boto3.client("sqs", region_name="us-east-2")
        url = client.create_queue(QueueName="ingest", Attributes={"VisibilityTimeout": "0"})[
            "QueueUrl"
        ]
        yield client, url


def event(key: str) -> str:
    return json.dumps({"Records": [{"s3": {"object": {"key": quote_plus(key)}}}]})


def remaining(client: Any, url: str) -> int:
    attributes = client.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    return int(attributes["ApproximateNumberOfMessages"])


def test_handled_message_is_deleted_and_the_key_is_decoded(queue: tuple[Any, str]) -> None:
    client, url = queue
    keys: list[str] = []
    client.send_message(QueueUrl=url, MessageBody=event("incoming/my file.csv"))

    assert SqsWorker(client, url, keys.append).run_once() == 1

    assert keys == ["incoming/my file.csv"]
    assert remaining(client, url) == 0


def test_failed_handler_keeps_the_message_for_a_retry(
    queue: tuple[Any, str], caplog: pytest.LogCaptureFixture
) -> None:
    client, url = queue

    def fail(key: str) -> None:
        raise RuntimeError("database down")

    client.send_message(QueueUrl=url, MessageBody=event("incoming/a.csv"))

    assert SqsWorker(client, url, fail).run_once() == 0

    assert remaining(client, url) == 1
    assert "stays in the queue" in caplog.text


@pytest.mark.parametrize(
    "body", ["not json", "[]", '{"Records": [{"s3": {}}]}', '{"Event": "s3:TestEvent"}']
)
def test_messages_that_can_never_succeed_are_deleted(queue: tuple[Any, str], body: str) -> None:
    client, url = queue
    keys: list[str] = []
    client.send_message(QueueUrl=url, MessageBody=body)

    assert SqsWorker(client, url, keys.append).run_once() == 1

    assert keys == []
    assert remaining(client, url) == 0


def test_loop_survives_a_receive_failure_and_stops_on_request() -> None:
    stop = threading.Event()

    class Broken:
        def receive_message(self, **_: object) -> None:
            stop.set()
            raise RuntimeError("queue unreachable")

    SqsWorker(Broken(), "url", lambda key: None).run_forever(stop)

    assert stop.is_set()
