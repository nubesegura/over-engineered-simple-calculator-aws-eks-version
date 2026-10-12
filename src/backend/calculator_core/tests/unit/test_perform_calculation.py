from decimal import Decimal
from uuid import UUID

import pytest

from calculator_core.application.errors import InfrastructureError
from calculator_core.application.use_cases.perform_calculation import (
    PerformCalculation,
    PerformCalculationCommand,
)
from calculator_core.domain.errors import (
    AuthenticationError,
    DivisionByZeroError,
    InvalidInputError,
    InvalidOperandError,
)
from calculator_core.domain.operation import Operation
from tests.fakes import (
    FIXED_ID,
    FIXED_NOW,
    FailingRepository,
    FixedClock,
    FixedIdGenerator,
    InMemoryCalculationRepository,
)


def _use_case(
    repository: InMemoryCalculationRepository | FailingRepository | None = None,
) -> tuple[PerformCalculation, InMemoryCalculationRepository]:
    memory = InMemoryCalculationRepository()
    use_case = PerformCalculation(repository or memory, FixedClock(), FixedIdGenerator())
    return use_case, memory


def _run(operation: Operation, a: str, b: str) -> Decimal:
    use_case, _ = _use_case()
    return use_case.execute(PerformCalculationCommand(operation, Decimal(a), Decimal(b))).result


@pytest.mark.parametrize(
    ("operation", "a", "b", "expected"),
    [
        (Operation.ADD, "5", "3", "8"),
        (Operation.SUB, "5", "3", "2"),
        (Operation.MUL, "5", "3", "15"),
        (Operation.DIV, "6", "3", "2"),
        (Operation.ADD, "0.1", "0.2", "0.3"),
        (Operation.DIV, "1", "3", "0.3333333333333333333333333333"),
        (Operation.ADD, "1e15", "-1e15", "0"),
    ],
)
def test_each_operation_uses_exact_decimal_arithmetic(
    operation: Operation, a: str, b: str, expected: str
) -> None:
    assert _run(operation, a, b) == Decimal(expected)


def test_result_has_no_floating_point_artifacts() -> None:
    assert str(_run(Operation.ADD, "0.1", "0.2")) == "0.3"
    assert str(_run(Operation.MUL, "1.1", "1.1")) == "1.21"


def test_success_builds_the_calculation_and_saves_it_before_returning() -> None:
    use_case, repo = _use_case()

    calculation = use_case.execute(
        PerformCalculationCommand(Operation.MUL, Decimal(2), Decimal(4), correlation_id="req-1")
    )

    assert calculation.id == FIXED_ID
    assert calculation.occurred_at == FIXED_NOW
    assert calculation.correlation_id == "req-1"
    assert repo.rows[FIXED_ID] == calculation


def test_a_supplied_calculation_id_is_used_and_repeating_it_keeps_one_row() -> None:
    use_case, repo = _use_case()
    chosen = UUID(int=42)
    command = PerformCalculationCommand(
        Operation.ADD, Decimal(1), Decimal(2), calculation_id=chosen
    )

    first = use_case.execute(command)
    use_case.execute(command)

    assert first.id == chosen
    assert list(repo.rows) == [chosen]


@pytest.mark.parametrize(
    "operand",
    ["NaN", "Infinity", "1000000000000000.1", "-1e16", "0.1234567890123456"],
)
def test_invalid_operands_are_input_errors_and_nothing_is_saved(operand: str) -> None:
    use_case, repo = _use_case()

    with pytest.raises(InvalidOperandError) as caught:
        use_case.execute(PerformCalculationCommand(Operation.ADD, Decimal(operand), Decimal(1)))

    assert caught.value.code == "VALIDATION_ERROR"
    assert repo.rows == {}


def test_second_operand_is_validated_too() -> None:
    use_case, _ = _use_case()

    with pytest.raises(InvalidOperandError):
        use_case.execute(PerformCalculationCommand(Operation.ADD, Decimal(1), Decimal("1e16")))


def test_limits_are_inclusive() -> None:
    assert _run(Operation.ADD, "-1e15", "0.5") == Decimal("-999999999999999.5")
    assert _run(Operation.ADD, "0.000000000000001", "0") == Decimal("1e-15")


def test_division_by_zero_has_its_own_code() -> None:
    use_case, repo = _use_case()

    with pytest.raises(DivisionByZeroError) as caught:
        use_case.execute(PerformCalculationCommand(Operation.DIV, Decimal(1), Decimal(0)))

    assert caught.value.code == "DIVISION_BY_ZERO"
    assert isinstance(caught.value, InvalidInputError)
    assert repo.rows == {}


def test_taxonomy_codes() -> None:
    assert AuthenticationError.code == "UNAUTHORIZED"
    assert issubclass(AuthenticationError, InvalidInputError)
    assert InfrastructureError.code == "INTERNAL_ERROR"
    assert not issubclass(InfrastructureError, InvalidInputError)


def test_save_failure_becomes_infrastructure_error_and_returns_nothing() -> None:
    cause = RuntimeError("connection lost")
    use_case, _ = _use_case(FailingRepository(cause))

    with pytest.raises(InfrastructureError) as caught:
        use_case.execute(PerformCalculationCommand(Operation.ADD, Decimal(1), Decimal(2)))

    assert caught.value.original_error is cause
    assert "connection lost" not in str(caught.value)


def test_an_error_already_in_the_taxonomy_is_not_wrapped_again() -> None:
    error = InfrastructureError("pool exhausted")
    use_case, _ = _use_case(FailingRepository(error))

    with pytest.raises(InfrastructureError) as caught:
        use_case.execute(PerformCalculationCommand(Operation.ADD, Decimal(1), Decimal(2)))

    assert caught.value is error
