from typing import Protocol


class ObjectStore(Protocol):
    def read(self, key: str, max_bytes: int) -> bytes | None:
        """Return the object body, at most `max_bytes + 1` bytes, or None if the key is absent.

        One byte more than the limit tells the caller that the object is too large.
        Must raise `InfrastructureError` if the store failed.
        """

    def put(self, key: str, body: bytes) -> None:
        """Write an object, replacing any previous one. Raises `InfrastructureError` on failure."""

    def move(self, source: str, target: str) -> None:
        """Copy `source` to `target` and then delete `source`.

        Raises `InfrastructureError` on failure; a failure after the copy leaves both keys.
        """
