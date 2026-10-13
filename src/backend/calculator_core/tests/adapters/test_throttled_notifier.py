from calculator_core.adapters.outbound.throttled_notifier import ThrottledErrorNotifier
from calculator_core.application.ports.error_notifier import InternalErrorReport
from tests.fakes import RecordingNotifier


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def report(error_type: str = "InfrastructureError") -> InternalErrorReport:
    return InternalErrorReport("calc-add", "dev", error_type, "db down", "corr-1")


def test_an_outage_sends_one_alert_per_interval_and_the_next_one_carries_the_count() -> None:
    clock = FakeClock()
    inner = RecordingNotifier()
    notifier = ThrottledErrorNotifier(inner, 300, clock)

    for _ in range(5):
        notifier.notify(report())
    assert [r.error_message for r in inner.reports] == ["db down"]

    clock.now += 299
    notifier.notify(report())
    assert len(inner.reports) == 1

    clock.now += 1
    notifier.notify(report())
    assert len(inner.reports) == 2
    assert inner.reports[1].error_message.startswith("db down (5 similar alert(s) suppressed")
    assert inner.reports[1].correlation_id == "corr-1"

    clock.now += 300
    notifier.notify(report())
    assert inner.reports[2].error_message == "db down"


def test_each_error_type_has_its_own_budget() -> None:
    inner = RecordingNotifier()
    notifier = ThrottledErrorNotifier(inner, 300, FakeClock())

    notifier.notify(report("InfrastructureError"))
    notifier.notify(report("ValueError"))
    notifier.notify(report("InfrastructureError"))

    assert [r.error_type for r in inner.reports] == ["InfrastructureError", "ValueError"]


def test_a_zero_interval_sends_every_alert() -> None:
    inner = RecordingNotifier()
    notifier = ThrottledErrorNotifier(inner, 0, FakeClock())

    notifier.notify(report())
    notifier.notify(report())

    assert len(inner.reports) == 2
