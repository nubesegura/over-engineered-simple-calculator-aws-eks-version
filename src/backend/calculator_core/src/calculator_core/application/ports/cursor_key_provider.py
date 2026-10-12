from typing import Protocol


class CursorKeyProvider(Protocol):
    def get(self) -> bytes:
        """Secret key that signs the history cursors, cached after the first read.

        Must raise `InfrastructureError` (never including the key) when it cannot be read.
        """
