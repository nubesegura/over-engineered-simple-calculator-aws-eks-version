from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError
from moto import mock_aws

from calculator_core.adapters.outbound.s3_object_store import S3ObjectStore
from calculator_core.application.errors import InfrastructureError

BUCKET = "ingest-test"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-2")
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-east-2"}
        )
        yield client


def test_read_is_bounded_to_one_byte_over_the_limit(s3: Any) -> None:
    s3.put_object(Bucket=BUCKET, Key="incoming/a.csv", Body=b"0123456789")
    store = S3ObjectStore(s3, BUCKET)

    assert store.read("incoming/a.csv", 4) == b"01234"
    assert store.read("incoming/a.csv", 100) == b"0123456789"


def test_read_of_a_missing_key_is_none(s3: Any) -> None:
    assert S3ObjectStore(s3, BUCKET).read("incoming/none.csv", 10) is None


def test_put_and_move(s3: Any) -> None:
    store = S3ObjectStore(s3, BUCKET)
    store.put("incoming/a.csv", b"x")

    store.move("incoming/a.csv", "processed/a.csv")

    keys = [item["Key"] for item in s3.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert keys == ["processed/a.csv"]


def test_missing_bucket_is_an_infrastructure_error(s3: Any) -> None:
    store = S3ObjectStore(s3, "no-such-bucket")

    with pytest.raises(InfrastructureError):
        store.read("incoming/a.csv", 1)
    with pytest.raises(InfrastructureError):
        store.put("reports/a.txt", b"x")
    with pytest.raises(InfrastructureError):
        store.move("incoming/a.csv", "processed/a.csv")


def test_network_failure_is_an_infrastructure_error() -> None:
    class Broken:
        def get_object(self, **_: object) -> None:
            raise EndpointConnectionError(endpoint_url="https://s3.invalid")

    with pytest.raises(InfrastructureError):
        S3ObjectStore(Broken(), BUCKET).read("incoming/a.csv", 1)
