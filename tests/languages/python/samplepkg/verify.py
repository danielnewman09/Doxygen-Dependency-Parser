"""Verification of calculator results.

Provides a :class:`Verifier` interface plus a concrete
:class:`ToleranceVerifier` and the :func:`assert_close` helper for
checking that a computed result matches an expected value within a
tolerance governed by :class:`VerificationLevel`.
"""

from abc import ABC, abstractmethod
from enum import Enum


class VerificationLevel(Enum):
    """Strictness of result verification."""

    LENIENT = 0
    STRICT = 1


class Verifier(ABC):
    """Interface for verifying a computed result against an expectation."""

    @abstractmethod
    def verify(self, expected: float, actual: float) -> bool:
        """Return True if *actual* is an acceptable result for *expected*."""


class ToleranceVerifier(Verifier):
    """Verifies results within an absolute tolerance.

    Attributes:
        tolerance: The maximum permitted absolute difference between
            *expected* and *actual*.
    """

    def __init__(self, tolerance: float = 1e-9):
        """Construct a verifier with the given absolute *tolerance*."""
        self.tolerance = tolerance

    def verify(self, expected: float, actual: float) -> bool:
        """Return True if ``abs(expected - actual) <= tolerance``."""
        return abs(expected - actual) <= self.tolerance


def assert_close(
    expected: float,
    actual: float,
    level: VerificationLevel = VerificationLevel.STRICT,
) -> None:
    """Raise ``AssertionError`` if *actual* is not close to *expected*.

    Args:
        expected: The expected value.
        actual: The computed value.
        level: The verification strictness, selecting the tolerance.

    Raises:
        AssertionError: If the values differ by more than the tolerance.
    """
    tolerance = 1e-9 if level is VerificationLevel.STRICT else 1e-6
    verifier = ToleranceVerifier(tolerance=tolerance)
    if not verifier.verify(expected, actual):
        raise AssertionError(f"{actual} is not close to {expected}")