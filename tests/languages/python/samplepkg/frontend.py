"""Frontend parser for the calculator.

Turns a textual expression such as ``"+ 5 * 3 - 2"`` into a list of
``(Operator, float)`` steps that the :class:`samplepkg.backend.Evaluator`
can consume.
"""

from samplepkg.errors import MalformedExpressionError, UnknownOperatorError
from samplepkg.operations import Operator


class Parser:
    """Parses operator/operand token pairs into evaluation steps.

    The expected input format is a whitespace-separated sequence of
    ``<operator> <number>`` pairs, e.g. ``"+ 5 * 3 - 2"``.
    """

    #: Maps an operator symbol to its :class:`Operator` value.
    OPERATOR_MAP: dict[str, Operator] = {op.value: op for op in Operator}

    def parse(self, text: str) -> list[tuple[Operator, float]]:
        """Parse *text* into a list of ``(operator, operand)`` steps.

        Args:
            text: The expression to parse.

        Returns:
            A list of ``(Operator, float)`` steps in left-to-right order.

        Raises:
            MalformedExpressionError: If the token count is not even or a
                token is not a number.
            UnknownOperatorError: If an operator symbol is not recognised.
        """
        tokens = text.split()
        if len(tokens) % 2 != 0:
            raise MalformedExpressionError(
                "expected operator/operand pairs", expression=text,
            )

        steps: list[tuple[Operator, float]] = []
        for i in range(0, len(tokens), 2):
            symbol, number = tokens[i], tokens[i + 1]
            op = self.OPERATOR_MAP.get(symbol)
            if op is None:
                raise UnknownOperatorError(
                    f"unknown operator: {symbol!r}", expression=text,
                )
            try:
                operand = float(number)
            except ValueError as exc:
                raise MalformedExpressionError(
                    f"not a number: {number!r}", expression=text,
                ) from exc
            steps.append((op, operand))
        return steps