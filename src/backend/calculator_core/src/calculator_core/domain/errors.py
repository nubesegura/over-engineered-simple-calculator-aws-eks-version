"""Error taxonomy shared by every layer.

- `CalculatorError`: base class; every error carries a stable, machine-readable `code`.
- `InvalidInputError`: the caller's input was rejected. Translated to 400 and never
  notified as an incident. Adapters catch this class, not a list of subclasses.
- `DomainError`: a business rule was violated (an `InvalidInputError`).
- `AuthenticationError`: the caller's token was rejected (an `InvalidInputError`, translated
  to 401; the entrypoints decide when to raise it).

Unexpected failures (`InfrastructureError` in the application layer, or any other
exception) are translated to 500 and notified.
"""


class CalculatorError(Exception):
    """Base class of every error raised on purpose by `calculator_core`."""

    code = "INTERNAL_ERROR"


class InvalidInputError(CalculatorError):
    """The caller's input was rejected. Translated to 400 and never notified."""

    code = "VALIDATION_ERROR"


class AuthenticationError(InvalidInputError):
    """The caller's token is missing or not accepted."""

    code = "UNAUTHORIZED"


class DomainError(InvalidInputError):
    """A business rule was violated."""


class InvalidOperandError(DomainError):
    """An operand is not finite, out of range or too precise."""


class DivisionByZeroError(DomainError):
    code = "DIVISION_BY_ZERO"

    def __init__(self) -> None:
        super().__init__("Division by zero is not allowed.")


class InvalidCursorError(DomainError):
    """The cursor was not issued by this system (malformed or not authentic)."""

    def __init__(self) -> None:
        super().__init__("cursor is not valid.")
