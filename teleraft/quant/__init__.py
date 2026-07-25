"""Quant research capability for a TeleRaft agent team.

Modelled on the two headline features of HKUDS/Vibe-Trading:

  * **Self-Improving Trading Agent** → the ``HypothesisRegistry`` closes the research
    loop: hypothesis → signal spec → backtest → metrics → refine, with invalidated
    ideas remembered so the team never re-tests a dead end.
  * **Multi-Agent Trading Teams** → the existing TeleRaft team model (souls, goals,
    topics, claimable tasks) plays the swarm roles: factor researcher, backtester,
    risk officer, macro analyst.

**Scope, deliberately:** this is *research* tooling. There are no broker connectors and
no order placement anywhere in this package — agents produce research drafts that a
human approves in Telegram. See docs/QUANT_TEAM_TUTORIAL.md §1.
"""

from .backtest import (
    BacktestResult,
    SignalSpec,
    SPEC_TYPES,
    backtest,
    generate_weights,
)
from .data import Bars, CsvLoader, MarketDataLoader, SyntheticLoader, split_period
from .hypothesis import Hypothesis, HypothesisRegistry, HypothesisStatus

__all__ = [
    "Bars",
    "BacktestResult",
    "CsvLoader",
    "Hypothesis",
    "HypothesisRegistry",
    "HypothesisStatus",
    "MarketDataLoader",
    "SPEC_TYPES",
    "SignalSpec",
    "SyntheticLoader",
    "backtest",
    "generate_weights",
    "split_period",
]
