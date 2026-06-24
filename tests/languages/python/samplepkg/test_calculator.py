"""Pytest tests for the samplepkg calculator.

These tests exercise the calculator's frontend parser, backend evaluator,
and error handling.  They serve as fixture data for the doxygen-index
Python parser's test-node extraction: each ``test_*`` function becomes a
:class:`TestNode`, each ``assert`` becomes an :class:`AssertionNode`,
and each function call becomes a :class:`TestStepNode`.
"""

import samplepkg
from samplepkg import Operator, Evaluator, Parser


def test_evaluator_step():
    """Evaluator accumulates results across multiple steps."""
    evaluator = Evaluator(0.0)
    evaluator.step(Operator.ADD, 5)
    evaluator.step(Operator.MULTIPLY, 3)
    assert evaluator.current == 15.0


def test_evaluator_from_zero():
    """from_zero classmethod creates an evaluator starting at zero."""
    evaluator = Evaluator.from_zero()
    evaluator.step(Operator.ADD, 10)
    assert evaluator.current == 10.0


def test_parser_parse():
    """Parser converts text to operator/operand steps."""
    parser = Parser()
    steps = parser.parse("+ 5 * 3 - 2")
    assert len(steps) == 3
    assert steps[0][0] == Operator.ADD
    assert steps[0][1] == 5.0


def test_error_division_by_zero():
    """Division by zero raises DivisionByZeroError."""
    evaluator = Evaluator(1.0)
    try:
        evaluator.step(Operator.DIVIDE, 0)
        assert False, "Should have raised"
    except samplepkg.DivisionByZeroError:
        assert True


class TestCalculatorPipeline:
    """Integration tests for the full parse → evaluate pipeline."""

    def test_full_pipeline(self):
        """Parse and evaluate a multi-step expression end-to-end."""
        parser = Parser()
        steps = parser.parse("+ 5 * 3 - 2")
        evaluator = Evaluator.from_zero()
        for op, operand in steps:
            evaluator.step(op, operand)
        assert evaluator.current == 13.0