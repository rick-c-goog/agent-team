"""Evaluation and operations (DESIGN.md §5.9).

A platform that cannot answer "did that change help?" accumulates plausible adjustments
forever. Two capabilities make improvement attributable rather than intuitive:

* **replay** — re-run a recorded run against a changed soul, prompt, or threshold and
  compare the outcomes.
* **fixtures** — known-answer cases per gate: a deliberately flawed artifact the checker
  must reject, a citation that does not support its claim, pure noise a statistical gate
  must kill. Regression tests for judgement.

Plus the process metrics worth watching in production — which are not model metrics.
"""

from .metrics import Metrics, collect
from .replay import ReplayComparison, replay_run
from .fixtures import Fixture, FixtureResult, GateFixtures, run_fixtures

__all__ = [
    "Fixture",
    "FixtureResult",
    "GateFixtures",
    "Metrics",
    "ReplayComparison",
    "collect",
    "replay_run",
    "run_fixtures",
]
