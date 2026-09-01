"""Minimal runtime remnants of the removed evaluation framework.

The full ``bujji.evals`` research framework was deleted. This package keeps
only the pieces the hybrid-agent runner (``bujji.agents.hybrid.runner``)
still uses at runtime: the core record/config dataclasses, the GAIA and
SWE-bench dataset loaders, their scorers, and the direct inference backend.
"""

from bujji.evals_compat.types import EvalRecord  # noqa: F401
