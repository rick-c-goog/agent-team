"""Pipeline vocabulary: nodes, items, outcomes (DESIGN.md §5.7.2).

The whole point of naming four node kinds is that each has different *scheduling*
semantics, and conflating them is what produces subtly wrong results rather than
obviously broken ones.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class NodeKind(str, enum.Enum):
    PRODUCER = "producer"              # builds artifacts; fans out; never kills
    GATE = "gate"                      # judges ONE item; may kill it
    JOIN = "join"                      # barrier: consumes ALL survivors → one aggregate
    AGGREGATE_GATE = "aggregate_gate"  # judges the aggregate, not its parts


class Verdict(str, enum.Enum):
    PASS = "pass"
    FAIL = "fail"
    # The gate could not be evaluated — missing data, unavailable benchmark. It
    # **blocks** rather than passing: synthesizing a gate's input defeats the gate
    # (DESIGN.md §5.7.7).
    CANNOT_EVALUATE = "cannot_evaluate"


@dataclass
class Item:
    """One thing flowing through the pipeline."""

    subject: str
    payload: dict = field(default_factory=dict)
    id: str = ""
    status: str = "running"          # running | passed | killed | blocked | graduated
    killed_at: str = ""
    kill_reason: str = ""

    @property
    def alive(self) -> bool:
        return self.status in ("running", "passed")


@dataclass
class NodeOutcome:
    """What a node execution produced."""

    verdict: Verdict = Verdict.PASS
    reasons: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)     # merged into the item
    # Artifacts survive their item being killed — a rejected factor's beta estimate is
    # still needed by a downstream join (§5.7.2 rule 4).
    artifacts: dict[str, Any] = field(default_factory=dict)
    # Optional significance record, fed to the trials-aware correction (§5.7.4).
    statistic: Optional[float] = None
    p_value: Optional[float] = None
    # Items a producer emitted (producers only).
    items: list[Item] = field(default_factory=list)


@dataclass
class Node:
    name: str
    kind: NodeKind
    run: Callable[..., NodeOutcome]
    owner: str = ""                   # agent that makes it
    checker: str = ""                 # agent that judges it; must differ from owner
    criteria: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.kind in (NodeKind.GATE, NodeKind.AGGREGATE_GATE) \
                and self.owner and self.checker and self.owner == self.checker:
            raise ValueError(
                f"node {self.name!r}: owner and checker are both {self.owner!r} — "
                "no agent grades its own work (DESIGN.md §5.1)"
            )


# -- constructors, so a pipeline reads as its shape -------------------------- #
def producer(name: str, run, owner: str = "", **kw) -> Node:
    return Node(name=name, kind=NodeKind.PRODUCER, run=run, owner=owner, **kw)


def gate(name: str, run, owner: str = "", checker: str = "", **kw) -> Node:
    return Node(name=name, kind=NodeKind.GATE, run=run, owner=owner, checker=checker, **kw)


def join(name: str, run, owner: str = "", **kw) -> Node:
    return Node(name=name, kind=NodeKind.JOIN, run=run, owner=owner, **kw)


def aggregate_gate(name: str, run, owner: str = "", checker: str = "", **kw) -> Node:
    return Node(name=name, kind=NodeKind.AGGREGATE_GATE, run=run,
                owner=owner, checker=checker, **kw)


@dataclass
class Pipeline:
    name: str
    nodes: list[Node]

    def validate(self) -> "Pipeline":
        if not self.nodes:
            raise ValueError(f"pipeline {self.name!r} has no nodes")
        kinds = [n.kind for n in self.nodes]
        if kinds[0] is not NodeKind.PRODUCER:
            raise ValueError(
                f"pipeline {self.name!r} must start with a producer — nothing else "
                "creates the items the gates judge"
            )
        seen: set[str] = set()
        for node in self.nodes:
            if node.name in seen:
                raise ValueError(f"duplicate node name {node.name!r}")
            seen.add(node.name)
        # An aggregate gate judges a join's output, so it must follow one.
        for i, node in enumerate(self.nodes):
            if node.kind is NodeKind.AGGREGATE_GATE:
                if not any(k is NodeKind.JOIN for k in kinds[:i]):
                    raise ValueError(
                        f"aggregate gate {node.name!r} has no preceding join — there is "
                        "no aggregate for it to judge"
                    )
        return self
