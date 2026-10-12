import logging
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from calculator_core.adapters.outbound.aws_errors import describe_error
from calculator_core.application.errors import InfrastructureError

logger = logging.getLogger(__name__)

_MISSING_CODES = {"NoSuchKey", "404", "NotFound"}


class S3ObjectStore:
    """Objects of one bucket. Every failure other than a missing key is an InfrastructureError."""

    def __init__(self, client: Any, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def read(self, key: str, max_bytes: int) -> bytes | None:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body: bytes = response["Body"].read(max_bytes + 1)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in _MISSING_CODES:
                return None
            raise self._failure("read", error) from error
        except BotoCoreError as error:
            raise self._failure("read", error) from error
        return body

    def put(self, key: str, body: bytes) -> None:
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=body)
        except (ClientError, BotoCoreError) as error:
            raise self._failure("write", error) from error

    def move(self, source: str, target: str) -> None:
        try:
            self._client.copy_object(
                Bucket=self._bucket,
                Key=target,
                CopySource={"Bucket": self._bucket, "Key": source},
            )
            self._client.delete_object(Bucket=self._bucket, Key=source)
        except (ClientError, BotoCoreError) as error:
            raise self._failure("move", error) from error

    @staticmethod
    def _failure(action: str, error: Exception) -> InfrastructureError:
        logger.error("Could not %s an object (%s).", action, describe_error(error))
        return InfrastructureError(f"The object store could not {action} the file.", error)
