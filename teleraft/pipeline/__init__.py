"""Pipelines: DAGs of gated runs (DESIGN.md §5.7).

An item enters, passes or dies at each gate, and what survives every gate is what
reaches a human. The four node kinds — producer, gate, join, aggregate_gate — are what
keep the three classic mistakes out: serialising gates that could run in parallel,
starting a join before the survivor set is known, and judging the parts when the
question is about the whole.

Domain-neutral by construction; the quant funnel in Appendix A is one instance.
"""

from .spec import (
    Item,
    Node,
    NodeKind,
    NodeOutcome,
    Pipeline,
    Verdict,
    aggregate_gate,
    gate,
    join,
    producer,
)
from .engine import PipelineEngine, PipelineResult
from .selection import SelectionReport, assess, benjamini_hochberg, deflate

__all__ = [
    "Item",
    "Node",
    "NodeKind",
    "NodeOutcome",
    "Pipeline",
    "PipelineEngine",
    "PipelineResult",
    "SelectionReport",
    "Verdict",
    "assess",
    "benjamini_hochberg",
    "deflate",
    "aggregate_gate",
    "gate",
    "join",
    "producer",
]
