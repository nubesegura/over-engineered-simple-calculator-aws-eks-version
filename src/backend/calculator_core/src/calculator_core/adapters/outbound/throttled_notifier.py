"""Limits the alert rate: at most one alert per error type per interval, per process.

During an outage every request fails with the same error; without a limit each one would send
an email. Alerts of a type that arrive inside the interval are counted, and the count travels in
the next alert of that type. The first alert of each type is always sent.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import replace

from calculator_core.application.ports.error_notifier import ErrorNotifier, InternalErrorReport


class ThrottledErrorNotifier:
    """Wraps a notifier; like the port, `notify` never raises (the wrapped one must not)."""

    def __init__(
        self,
        inner: ErrorNotifier,
        interval_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._interval_seconds = interval_seconds
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._last_sent: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    def notify(self, report: InternalErrorReport) -> None:
        to_send = self._admit(report)
        if to_send is not None:
            self._inner.notify(to_send)

    def _admit(self, report: InternalErrorReport) -> InternalErrorReport | None:
        key = report.error_type
        with self._lock:
            now = self._monotonic()
            last = self._last_sent.get(key)
            if last is not None and now - last < self._interval_seconds:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return None
            self._last_sent[key] = now
            count = self._suppressed.pop(key, 0)
        if count == 0:
            return report
        note = f" ({count} similar alert(s) suppressed since the previous one.)"
        return replace(report, error_message=report.error_message + note)
