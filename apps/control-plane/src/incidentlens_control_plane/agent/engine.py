"""Compatibility re-exports.

The canonical implementations now live in:
  - ``baseline.DeterministicInvestigationEngine``
  - ``runtime.LLMInvestigationEngine``

This module re-exports them for backward compatibility until
the explicit factory is wired in (Task 9).
"""

from .baseline import DeterministicInvestigationEngine
from .runtime import LLMInvestigationEngine

# Temporary compatibility for main.py until Task 9 installs the explicit factory.
InvestigationEngine = DeterministicInvestigationEngine

__all__ = [
    "DeterministicInvestigationEngine",
    "InvestigationEngine",
    "LLMInvestigationEngine",
]
