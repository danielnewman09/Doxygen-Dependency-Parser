"""Backend evaluation engine for the calculator.

The :class:`Evaluator` accumulates a running value across a sequence of
operator/operand steps, deferring the arithmetic to
:func:`samplepkg.operations.apply_operator`.
"""

from samplepkg.errors import DivisionByZeroError
from samplepkg.operations import Operator, apply_operator


class Evaluator:
    """Evaluates a sequence of ``(operator, operand)`` steps.

    The evaluator holds a running ``value`` that each call to
    :meth:`step` mutates in place.

    Attributes:
        value: The current accumulated value.
    """

    #: Default starting value for a fresh evaluator.
    DEFAULT_INITIAL: float = 0.0

    def __init__(self, initial: float = DEFAULT_INITIAL):
        """Construct an evaluator seeded with *initial*."""
        self.value = initial

    @classmethod
    def from_zero(cls) -> "Evaluator":
        """Create an evaluator starting from zero."""
        return cls(0.0)

    @property
    def current(self) -> float:
        """The current accumulated value (read-only view of ``value``)."""
        return self.value

    def step(self, op: Operator, operand: float) -> float:
        """Apply one ``(op, operand)`` step to the running value.

        Args:
            op: The operator to apply.
            operand: The right-hand operand.

        Returns:
            The new accumulated value.

        Raises:
            DivisionByZeroError: If *op* is division and *operand* is 0.
        """
        if op is Operator.DIVIDE and operand == 0:
            raise DivisionByZeroError(
                "division by zero", expression=f"{self.value} / 0",
            )
        self.value = apply_operator(op, self.value, operand)
        return self.value

    def reset(self) -> None:
        """Reset the accumulated value to zero."""
        self.value = 0.0