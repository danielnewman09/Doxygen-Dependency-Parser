"""Error hierarchy for the calculator.

All calculator-specific exceptions derive from :class:`CalculatorError`,
which records the offending expression so callers can report context.
"""


class CalculatorError(Exception):
    """Base class for every error raised by the calculator.

    Attributes:
        expression: The expression (or token stream) that caused the
            error, if known.  Empty string when no context is available.
    """

    def __init__(self, message: str, *, expression: str = ""):
        """Construct a calculator error.

        Args:
            message: Human-readable description of the failure.
            expression: The expression context in which it occurred.
        """
        super().__init__(message)
        self.expression = expression

    def describe(self) -> str:
        """Return a single-line description including any expression context."""
        if self.expression:
            return f"{self.expression}: {self.args[0]}"
        return str(self.args[0])


class DivisionByZeroError(CalculatorError):
    """Raised when a division by zero is attempted."""


class UnknownOperatorError(CalculatorError):
    """Raised when a token does not map to a known operator."""


class MalformedExpressionError(CalculatorError):
    """Raised when an expression cannot be parsed into steps."""