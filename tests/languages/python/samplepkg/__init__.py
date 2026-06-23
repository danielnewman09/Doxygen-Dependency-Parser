"""samplepkg — a trivial calculator for doxygen-extraction tests.

The package is intentionally small but exercises every construct the
``doxygen-index`` Python parser and ``graph_json`` relationship builder
must handle: a package with re-exports, multiple sub-modules, a class
hierarchy (errors), an ``Enum`` (operations / verification levels), an
``abc.ABC`` interface (verifier), ``classmethod``/``property``/
``abstractmethod`` decorators, class-level attributes, free functions,
and a mix of external and cross-module imports.

Layout:

* :mod:`samplepkg.errors`      — error hierarchy (``CalculatorError`` + subclasses)
* :mod:`samplepkg.operations`  — ``Operator`` enum and ``apply_operator``
* :mod:`samplepkg.backend`     — ``Evaluator`` accumulation engine
* :mod:`samplepkg.frontend`    — ``Parser`` turning text into steps
* :mod:`samplepkg.verify`      — ``Verifier`` interface, ``ToleranceVerifier``, ``assert_close``
"""

from samplepkg.errors import (
    CalculatorError,
    DivisionByZeroError,
    MalformedExpressionError,
    UnknownOperatorError,
)
from samplepkg.operations import Operator, apply_operator
from samplepkg.backend import Evaluator
from samplepkg.frontend import Parser
from samplepkg.verify import (
    ToleranceVerifier,
    VerificationLevel,
    Verifier,
    assert_close,
)

__all__ = [
    "CalculatorError",
    "DivisionByZeroError",
    "MalformedExpressionError",
    "UnknownOperatorError",
    "Operator",
    "apply_operator",
    "Evaluator",
    "Parser",
    "Verifier",
    "ToleranceVerifier",
    "VerificationLevel",
    "assert_close",
]