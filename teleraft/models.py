"""Core domain models (DESIGN.md §5, §7).

These are plain dataclasses with JSON (de)serialization so the whole ``RunState`` can
be checkpointed to SQLite after every graph node and rehydrated on resume.
"""

from __future__ import annotations

import enum
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class TaskStatus(str, enum.Enum):
    """DESIGN.md §6 — Todo → In Progress → In Review → Done / Closed."""

    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"
    CLOSED = "closed"


class RunStatus(str, enum.Enum):
    PLANNING = "planning"
    BUILDING = "building"
    TESTING = "testing"
    AWAITING_HUMAN = "awaiting_human"
    DONE = "done"
    FAILED = "failed"


class Role(str, enum.Enum):
    """DESIGN.md §11 — only humans own; admin agents manage structure."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class Gate(str, enum.Enum):
    """Human-interrupt points in the graph (DESIGN.md §5.2)."""

    PLAN = "plan"
    REVIEW = "review"


# --------------------------------------------------------------------------- #
# Loop artifacts (DESIGN.md §5.1)
# --------------------------------------------------------------------------- #
@dataclass
class Plan:
    """Planner output: acceptance criteria + steps, produced before any building."""

    criteria: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    needs_human: bool = False

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Plan":
        return Plan(
            criteria=list(d.get("criteria", [])),
            steps=list(d.get("steps", [])),
            risks=list(d.get("risks", [])),
            needs_human=bool(d.get("needs_human", False)),
        )


@dataclass
class Passage:
    """A retrieved chunk of an agent's knowledge base (DESIGN.md §4.1.3)."""

    source_id: str
    doc: str
    locator: str          # "p.12" | "# Brand > ## Tone" | "row 4"
    text: str
    score: float = 0.0

    def cite(self) -> str:
        return f"{self.doc} {self.locator}".strip()

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Passage":
        return Passage(
            source_id=d.get("source_id", ""),
            doc=d.get("doc", ""),
            locator=d.get("locator", ""),
            text=d.get("text", ""),
            score=float(d.get("score", 0.0)),
        )


@dataclass
class Citation:
    """What a Builder actually leaned on — checkable by the Tester (DESIGN.md §4.1.3)."""

    source_id: str
    doc: str
    locator: str
    quote: str

    def render(self) -> str:
        return f"{self.doc} {self.locator}".strip()

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Citation":
        return Citation(
            source_id=d.get("source_id", ""),
            doc=d.get("doc", ""),
            locator=d.get("locator", ""),
            quote=d.get("quote", ""),
        )


@dataclass
class Artifact:
    """Builder output for one step."""

    step: int
    content: str = ""
    files: list[str] = field(default_factory=list)
    notes: str = ""
    citations: list[Citation] = field(default_factory=list)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Artifact":
        return Artifact(
            step=int(d["step"]),
            content=d.get("content", ""),
            files=list(d.get("files", [])),
            notes=d.get("notes", ""),
            citations=[Citation.from_dict(c) for c in d.get("citations", [])],
        )


@dataclass
class Verdict:
    """Tester output: adversarial review against the Planner's criteria."""

    step: int
    passed: bool
    reasons: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    tester: str = ""

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "Verdict":
        return Verdict(
            step=int(d["step"]),
            passed=bool(d["passed"]),
            reasons=list(d.get("reasons", [])),
            lessons=list(d.get("lessons", [])),
            tester=d.get("tester", ""),
        )


@dataclass
class Budget:
    """Per-run caps (DESIGN.md §5.2)."""

    token_cap: int = 200_000
    wall_clock_cap_s: int = 3600
    max_retries_per_step: int = 2
    max_replans: int = 2


# --------------------------------------------------------------------------- #
# RunState — the checkpointed heart of a graph run (DESIGN.md §5.2)
# --------------------------------------------------------------------------- #
@dataclass
class RunState:
    task_id: str
    agent: str                       # owning agent (claimed the task)
    tester_agent: str = ""           # MUST differ from `agent` — no self-grading
    plan: Optional[Plan] = None
    current_step: int = 0
    artifacts: list[Artifact] = field(default_factory=list)
    verdicts: list[Verdict] = field(default_factory=list)
    retries: dict[int, int] = field(default_factory=dict)   # per-step retry counts
    replans: int = 0
    tokens_used: int = 0
    wall_clock_s: float = 0.0
    budget: Budget = field(default_factory=Budget)
    status: RunStatus = RunStatus.PLANNING
    lessons: list[str] = field(default_factory=list)
    recalled_memory: list[str] = field(default_factory=list)
    knowledge: list[Passage] = field(default_factory=list)   # cited context (§4.1)
    # Where to resume after a human gate suspends the run:
    resume_node: Optional[str] = None
    pending_gate: Optional[Gate] = None

    # -- serialization ----------------------------------------------------- #
    def to_json(self) -> str:
        return json.dumps(self._plain())

    def _plain(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent": self.agent,
            "tester_agent": self.tester_agent,
            "plan": asdict(self.plan) if self.plan else None,
            "current_step": self.current_step,
            "artifacts": [asdict(a) for a in self.artifacts],
            "verdicts": [asdict(v) for v in self.verdicts],
            "retries": {str(k): v for k, v in self.retries.items()},
            "replans": self.replans,
            "tokens_used": self.tokens_used,
            "wall_clock_s": self.wall_clock_s,
            "budget": asdict(self.budget),
            "status": self.status.value,
            "lessons": list(self.lessons),
            "recalled_memory": list(self.recalled_memory),
            "knowledge": [asdict(p) for p in self.knowledge],
            "resume_node": self.resume_node,
            "pending_gate": self.pending_gate.value if self.pending_gate else None,
        }

    @staticmethod
    def from_json(s: str) -> "RunState":
        d = json.loads(s)
        return RunState(
            task_id=d["task_id"],
            agent=d["agent"],
            tester_agent=d.get("tester_agent", ""),
            plan=Plan.from_dict(d["plan"]) if d.get("plan") else None,
            current_step=int(d.get("current_step", 0)),
            artifacts=[Artifact.from_dict(a) for a in d.get("artifacts", [])],
            verdicts=[Verdict.from_dict(v) for v in d.get("verdicts", [])],
            retries={int(k): int(v) for k, v in d.get("retries", {}).items()},
            replans=int(d.get("replans", 0)),
            tokens_used=int(d.get("tokens_used", 0)),
            wall_clock_s=float(d.get("wall_clock_s", 0.0)),
            budget=Budget(**d["budget"]) if d.get("budget") else Budget(),
            status=RunStatus(d.get("status", "planning")),
            lessons=list(d.get("lessons", [])),
            recalled_memory=list(d.get("recalled_memory", [])),
            knowledge=[Passage.from_dict(p) for p in d.get("knowledge", [])],
            resume_node=d.get("resume_node"),
            pending_gate=Gate(d["pending_gate"]) if d.get("pending_gate") else None,
        )

    # -- convenience ------------------------------------------------------- #
    @property
    def latest_artifact(self) -> Optional[Artifact]:
        return self.artifacts[-1] if self.artifacts else None

    def artifact_for_step(self, step: int) -> Optional[Artifact]:
        for a in reversed(self.artifacts):
            if a.step == step:
                return a
        return None
