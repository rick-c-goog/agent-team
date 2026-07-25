"""Runtime interface shared by every engine (mock, Claude, CLI adapters)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from ..models import Artifact, Passage, Plan, Verdict


@dataclass
class RoleRequest:
    """Everything a runtime needs to play one loop role for one task.

    The composed prompt a real runtime sees is: soul + goals + recalled memory +
    retrieved knowledge + task context + the role-specific instruction. We pass the
    pieces structured so a mock can be deterministic and a real adapter can format them
    however it likes.
    """

    role: str                       # "planner" | "builder" | "tester" | "learner"
    agent: str                      # which agent identity is playing the role
    soul: str = ""
    goals: dict = field(default_factory=dict)
    memory: list[str] = field(default_factory=list)
    knowledge: list[Passage] = field(default_factory=list)   # cited context (§4.1)
    task_title: str = ""
    task_body: str = ""
    # role-specific context
    plan: Optional[Plan] = None
    step: int = 0
    artifact: Optional[Artifact] = None
    prior_verdicts: list[Verdict] = field(default_factory=list)


class Runtime(Protocol):
    """A runtime plays loop roles. Token accounting lets the engine enforce budgets."""

    name: str

    def plan(self, req: RoleRequest) -> tuple[Plan, int]:
        """Return (plan, tokens_used)."""
        ...

    def build(self, req: RoleRequest) -> tuple[Artifact, int]:
        """Return (artifact, tokens_used)."""

    def test(self, req: RoleRequest) -> tuple[Verdict, int]:
        """Return (verdict, tokens_used). MUST be an agent other than the builder."""

    def learn(self, req: RoleRequest) -> tuple[list[str], int]:
        """Distill lessons from verdicts/human feedback. Return (lessons, tokens)."""
