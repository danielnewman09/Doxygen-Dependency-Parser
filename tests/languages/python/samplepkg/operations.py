"""Operation table for the calculator backend.

Defines the supported binary :class:`Operator` values and a single
:func:`apply_operator` entry point used by the evaluator.
"""

from enum import Enum


class Operator(Enum):
    """The binary operators supported by the calculator."""

    ADD = "+"
    SUBTRACT = "-"
    MULTIPLY = "*"
    DIVIDE = "/"


def apply_operator(op: Operator, left: float, right: float) -> float:
    """Apply *op* to *left* and *right* and return the numeric result.

    Args:
        op: The operator to apply.
        left: The left-hand operand.
        right: The right-hand operand.

    Returns:
        The result of the binary operation.

    Raises:
        ValueError: If *op* is not a supported operator.
    """
    if op is Operator.ADD:
        return left + right
    if op is Operator.SUBTRACT:
        return left - right
    if op is Operator.MULTIPLY:
        return left * right
    if op is Operator.DIVIDE:
        return left / right
    raise ValueError(f"Unsupported operator: {op}")