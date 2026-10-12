"""Long-polling SQS consumer for S3 event notifications.

A message is deleted only after its file was handled (including a business rejection) or when
it can never succeed (malformed body, S3 test event, hostile key). When the handler raises, the
message stays in the queue: it becomes visible again and, after the configured number of
receives, SQS moves it to the dead-letter queue.
"""

import json
import logging
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import unquote_plus

logger = logging.getLogger(__name__)

WAIT_SECONDS = 20
ERROR_PAUSE_SECONDS = 5.0


class SqsWorker:
    def __init__(self, client: Any, queue_url: str, handler: Callable[[str], object]) -> None:
        self._client = client
        self._queue_url = queue_url
        self._handler = handler

    def run_once(self) -> int:
        """Receive up to one batch and handle it; returns the number of messages deleted."""
        response = self._client.receive_message(
            QueueUrl=self._queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=WAIT_SECONDS
        )
        deleted = 0
        for message in response.get("Messages", []):
            if self._handle(message):
                self._client.delete_message(
                    QueueUrl=self._queue_url, ReceiptHandle=message["ReceiptHandle"]
                )
                deleted += 1
        return deleted

    def run_forever(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.run_once()
            except Exception:  # the loop must survive a failed receive or delete
                logger.exception("The queue could not be read; retrying shortly.")
                stop.wait(ERROR_PAUSE_SECONDS)

    def _handle(self, message: dict[str, Any]) -> bool:
        try:
            keys = _object_keys(message.get("Body", ""))
        except ValueError:
            logger.error("A queue message was not an S3 event notification and was dropped.")
            return True
        try:
            for key in keys:
                self._handler(key)
        except Exception:  # infrastructure failure: keep the message for a retry
            logger.exception("A file could not be processed; the message stays in the queue.")
            return False
        return True


def _object_keys(body: str) -> list[str]:
    """Object keys of an S3 event notification; an S3 test event has none."""
    try:
        document = json.loads(body)
        records = document.get("Records", [])
        return [unquote_plus(record["s3"]["object"]["key"]) for record in records]
    except (json.JSONDecodeError, AttributeError, KeyError, TypeError) as error:
        raise ValueError("not an S3 event notification") from error
