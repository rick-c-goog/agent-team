"""The state-graph engine that runs the Anthropic loop (DESIGN.md §5.2).

Nodes:  intake → plan → [gate:plan] → orchestrate ⇄ build ⇄ test → [gate:review] →
        learn → END, with replan/escalation edges.

Guarantees: a checkpoint is written after every node, so a crash resumes at the last
node; human gates are *interrupts* (the run suspends and is resumed by a Telegram
callback, minutes or days later); token/step/replan budgets bound every run; and each
node emits an audit event.

The engine is runtime-agnostic and Telegram-agnostic: it talks to a ``Registry`` for
agent souls/goals/tester-selection, a ``MemoryService`` for recall/writeback, a
``runtime_for(agent)`` factory, and a ``notify(event, **data)`` hook that the gateway
wires to Telegram (tests pass a capturing stub).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from ..models import (
    Gate,
    RunState,
    RunStatus,
    TaskStatus,
    Verdict,
)
from ..runtime.base import RoleRequest, Runtime

GRAPH_VERSION = "pobt-v1"

END = "__end__"


class Interrupt(Exception):
    """Raised internally to suspend a run at a human gate.

    Carries enough context for the gateway to render the right inline keyboard.
    """

    def __init__(self, run_id: str, gate: Gate, task_id: str, payload: dict):
        super().__init__(f"interrupt at gate {gate.value} for run {run_id}")
        self.run_id = run_id
        self.gate = gate
        self.task_id = task_id
        self.payload = payload


class Registry(Protocol):
    def soul(self, agent: str) -> str: ...
    def goals(self, agent: str) -> dict: ...
    def pick_tester(self, exclude: str) -> str: ...


class MemoryService(Protocol):
    def recall(self, agent: str, query: str, k: int = 5) -> list[str]: ...
    def write(self, agent: str, note: str, source: str, task_id: Optional[str]) -> None: ...
    def propose_soul_amendment(self, agent: str, lesson: str, task_id: str) -> Optional[str]: ...


class KnowledgeService(Protocol):
    """Optional RAG layer (§4.1); when absent the loop runs ungrounded as before."""

    def retrieve(self, agent: str, query: str, k: int = 5): ...


@dataclass
class EngineResult:
    """Returned by run()/resume(): either finished, or suspended at a gate."""

    run_id: str
    status: RunStatus
    interrupt: Optional[Interrupt] = None
    error: str = ""


class GraphEngine:
    def __init__(
        self,
        storage,
        registry: Registry,
        memory: MemoryService,
        runtime_for: Callable[[str], Runtime],
        notify: Callable[..., None] | None = None,
        knowledge: Optional[KnowledgeService] = None,
        knowledge_k: int = 5,
    ):
        self.storage = storage
        self.registry = registry
        self.memory = memory
        self.runtime_for = runtime_for
        self.notify = notify or (lambda event, **data: None)
        self.knowledge = knowledge
        self.knowledge_k = knowledge_k

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def start(self, task_id: str, owner_agent: str) -> EngineResult:
        """Begin a new run for a claimed task and drive it to the first gate/END."""
        tester = self.registry.pick_tester(exclude=owner_agent)
        state = RunState(task_id=task_id, agent=owner_agent, tester_agent=tester)
        run_id = "run_" + uuid.uuid4().hex[:12]
        self.storage.create_run(run_id, task_id, GRAPH_VERSION, state)
        self.storage.update_task(task_id, status=TaskStatus.IN_PROGRESS.value, owner=owner_agent)
        return self._drive(run_id, state, "intake")

    def resume(self, run_id: str, decision: str, *, reason: str = "", user_id: str = "") -> EngineResult:
        """Resume a suspended run when a human answers a gate in Telegram."""
        loaded = self.storage.load_run(run_id)
        if loaded is None:
            raise KeyError(f"unknown run {run_id}")
        _task_id, state = loaded
        if state.status is not RunStatus.AWAITING_HUMAN or state.pending_gate is None:
            raise ValueError(f"run {run_id} is not awaiting a human decision")

        gate = state.pending_gate
        self.storage.add_approval(run_id, gate.value, user_id, decision, reason)
        resume_node = state.resume_node or END
        state.pending_gate = None
        state.resume_node = None

        if gate is Gate.PLAN:
            next_node = "orchestrate" if decision == "approve" else "plan"
        elif gate is Gate.REVIEW:
            if decision == "approve":
                next_node = "learn"
            else:
                # Human rejection with a reason feeds the loop like a Tester verdict.
                self._record_human_rejection(state, reason)
                next_node = "replan"
        else:  # pragma: no cover - future gates
            next_node = resume_node

        return self._drive(run_id, state, next_node)

    # ------------------------------------------------------------------ #
    # Driver
    # ------------------------------------------------------------------ #
    def _drive(self, run_id: str, state: RunState, node: str) -> EngineResult:
        """Execute nodes until END or a human interrupt, checkpointing after each."""
        seq = len(self.storage.run_events(run_id))
        while node != END:
            executed = node
            try:
                node = self._exec_node(executed, run_id, state)
            except Interrupt as intr:  # noqa: PERF203 - control flow, not an error
                state.status = RunStatus.AWAITING_HUMAN
                state.pending_gate = intr.gate
                self.storage.checkpoint(run_id, state)
                seq += 1
                self.storage.add_run_event(run_id, seq, f"{executed}→gate:{intr.gate.value}",
                                           "suspended for human")
                return EngineResult(run_id, RunStatus.AWAITING_HUMAN, interrupt=intr)
            except Exception as e:
                # A node blew up (bad credential, provider down, a bug). Record it,
                # surface it in Telegram, and leave the task visibly failed — a run
                # that dies only in the server log looks identical to a hung one.
                state.status = RunStatus.FAILED
                self.storage.checkpoint(run_id, state)
                seq += 1
                self.storage.add_run_event(run_id, seq, f"{executed}→failed", str(e)[:500])
                self.storage.finish_run(run_id, state)
                self.storage.update_task(state.task_id, status=TaskStatus.TODO.value,
                                         owner=None)
                self.notify("failed", run_id=run_id, task_id=state.task_id,
                            node=executed, agent=state.agent, error=str(e))
                return EngineResult(run_id, RunStatus.FAILED, error=str(e))

            seq += 1
            self.storage.add_run_event(run_id, seq, executed, self._event_detail(state))
            self.storage.checkpoint(run_id, state)

        state.status = RunStatus.DONE
        self.storage.finish_run(run_id, state)
        return EngineResult(run_id, RunStatus.DONE)

    def _exec_node(self, node: str, run_id: str, state: RunState) -> str:
        handler = getattr(self, f"_node_{node}", None)
        if handler is None:  # pragma: no cover - guards typos
            raise ValueError(f"no graph node named {node!r}")
        return handler(run_id, state)

    # ------------------------------------------------------------------ #
    # Nodes
    # ------------------------------------------------------------------ #
    def _node_intake(self, run_id: str, state: RunState) -> str:
        task = self.storage.get_task(state.task_id)
        query = f"{task['title']} {task['body'] or ''}"
        state.recalled_memory = self.memory.recall(state.agent, query)
        # Ground the run in the agent's knowledge base before any planning (§4.1.3).
        state.knowledge = self._retrieve(state.agent, query)
        if state.knowledge:
            self.notify("knowledge", task=task, run_id=run_id, agent=state.agent,
                        passages=state.knowledge)
        state.status = RunStatus.PLANNING
        return "plan"

    def _node_plan(self, run_id: str, state: RunState) -> str:
        task = self.storage.get_task(state.task_id)
        req = self._req("planner", state.agent, state, task)
        plan, tokens = self.runtime_for(state.agent).plan(req)
        state.plan = plan
        state.tokens_used += tokens
        state.retries = {}
        self.notify("plan", task=task, plan=plan, run_id=run_id, agent=state.agent)
        if plan.needs_human:
            self.notify("gate_plan", task=task, plan=plan, run_id=run_id, agent=state.agent)
            raise Interrupt(run_id, Gate.PLAN, state.task_id,
                            {"plan": plan, "agent": state.agent})
        return "orchestrate"

    def _node_orchestrate(self, run_id: str, state: RunState) -> str:
        """The Orchestrator: pick the next step, retry, replan, or send to review."""
        if state.tokens_used > state.budget.token_cap:
            self.notify("escalate", run_id=run_id, task_id=state.task_id, reason="token budget exceeded")
            raise Interrupt(run_id, Gate.REVIEW, state.task_id,
                            {"reason": "token budget exceeded", "artifact": state.latest_artifact})

        assert state.plan is not None
        next_step = self._first_unpassed_step(state)
        if next_step is None:
            # All steps have a passing verdict → human review gate.
            state.status = RunStatus.TESTING
            task = self.storage.get_task(state.task_id)
            self.storage.update_task(state.task_id, status=TaskStatus.IN_REVIEW.value)
            self.notify("gate_review", task=task, run_id=run_id,
                        artifact=state.latest_artifact, agent=state.agent, tester=state.tester_agent)
            raise Interrupt(run_id, Gate.REVIEW, state.task_id,
                            {"artifact": state.latest_artifact, "tester": state.tester_agent})
        state.current_step = next_step
        state.status = RunStatus.BUILDING
        return "build"

    def _node_build(self, run_id: str, state: RunState) -> str:
        task = self.storage.get_task(state.task_id)
        # Step-scoped retrieval: the Builder gets passages for *this* step, not just the
        # task-level ones gathered at Intake (§4.1.3).
        step_query = self._step_query(state, task)
        step_knowledge = self._retrieve(state.agent, step_query) or state.knowledge
        req = self._req("builder", state.agent, state, task, step=state.current_step,
                        knowledge=step_knowledge)
        artifact, tokens = self.runtime_for(state.agent).build(req)
        artifact.step = state.current_step
        state.artifacts.append(artifact)
        state.tokens_used += tokens
        if artifact.citations:
            self.storage.add_citations(run_id, state.current_step, artifact.citations)
        self.notify("progress", task=task, run_id=run_id, agent=state.agent,
                    # First line only: a hard character cut lands mid-word ("In-sample: po")
                    # and reads like the message itself is broken.
                    text=f"built step {state.current_step + 1}: "
                         f"{artifact.content.splitlines()[0] if artifact.content else '(empty)'}")
        return "test"

    def _node_test(self, run_id: str, state: RunState) -> str:
        # Adversarial review by a DIFFERENT agent — DESIGN.md: no agent grades its own work.
        assert state.tester_agent and state.tester_agent != state.agent
        task = self.storage.get_task(state.task_id)
        # The Tester checks grounding, so it must see the same passages the Builder saw.
        # Retrieval is deterministic, so re-running the step query reproduces them
        # against the *builder's* knowledge scope (§4.1.3).
        builder_knowledge = self._retrieve(state.agent, self._step_query(state, task)) or state.knowledge
        req = self._req("tester", state.tester_agent, state, task,
                        step=state.current_step, artifact=state.latest_artifact,
                        knowledge=builder_knowledge)
        verdict, tokens = self.runtime_for(state.tester_agent).test(req)
        verdict.step = state.current_step
        verdict.tester = state.tester_agent
        state.verdicts.append(verdict)
        state.tokens_used += tokens
        self.notify("verdict", task=task, run_id=run_id, verdict=verdict, tester=state.tester_agent)

        if verdict.passed:
            return "orchestrate"

        if verdict.terminal:
            # The Tester says no retry can succeed — escalate rather than burn budget.
            reason = "; ".join(verdict.reasons) or "tester marked the result terminal"
            self.notify("escalate", run_id=run_id, task_id=state.task_id, reason=reason)
            raise Interrupt(run_id, Gate.REVIEW, state.task_id,
                            {"reason": reason, "artifact": state.latest_artifact})

        used = state.retries.get(state.current_step, 0) + 1
        state.retries[state.current_step] = used
        if used <= state.budget.max_retries_per_step:
            return "build"     # retry the same step, addressing the reasons
        return "replan"

    def _node_replan(self, run_id: str, state: RunState) -> str:
        state.replans += 1
        if state.replans > state.budget.max_replans:
            self.notify("escalate", run_id=run_id, task_id=state.task_id,
                        reason="replan budget exhausted")
            raise Interrupt(run_id, Gate.REVIEW, state.task_id,
                            {"reason": "replan budget exhausted", "artifact": state.latest_artifact})
        # Re-plan with accumulated verdicts as context; reset per-step retry counters.
        task = self.storage.get_task(state.task_id)
        req = self._req("planner", state.agent, state, task)
        plan, tokens = self.runtime_for(state.agent).plan(req)
        state.plan = plan
        state.tokens_used += tokens
        state.retries = {}
        self.notify("progress", task=task, run_id=run_id, agent=state.agent,
                    text=f"replanned (attempt {state.replans + 1})")
        return "orchestrate"

    def _node_learn(self, run_id: str, state: RunState) -> str:
        """Self-improvement writeback (DESIGN.md §5.2 Learn)."""
        task = self.storage.get_task(state.task_id)
        req = self._req("learner", state.agent, state, task)
        lessons, tokens = self.runtime_for(state.agent).learn(req)
        state.tokens_used += tokens
        state.lessons = lessons
        for lesson in lessons:
            self.memory.write(state.agent, lesson, source="tester", task_id=state.task_id)
            amendment = self.memory.propose_soul_amendment(state.agent, lesson, state.task_id)
            if amendment:
                self.notify("soul_amendment_proposed", agent=state.agent,
                            lesson=lesson, task_id=state.task_id)
        self.storage.update_task(state.task_id, status=TaskStatus.DONE.value)
        self.notify("done", task=task, run_id=run_id, agent=state.agent, lessons=lessons)
        return END

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _first_unpassed_step(self, state: RunState) -> Optional[int]:
        assert state.plan is not None
        passed = {v.step for v in state.verdicts if v.passed}
        for i in range(len(state.plan.steps)):
            if i not in passed:
                return i
        return None

    def _record_human_rejection(self, state: RunState, reason: str) -> None:
        step = state.current_step
        state.verdicts.append(
            Verdict(step=step, passed=False,
                    reasons=[reason or "rejected by human reviewer"],
                    lessons=[reason] if reason else [], tester="human")
        )
        # Force the rejected step to be rebuilt.
        state.retries[step] = 0

    def _retrieve(self, agent: str, query: str):
        if self.knowledge is None:
            return []
        try:
            return self.knowledge.retrieve(agent, query, self.knowledge_k)
        except Exception:
            # A broken index must never take down a run — it degrades to ungrounded
            # work, and source health surfaces the failure separately (§4.1.5).
            return []

    def _step_query(self, state: RunState, task) -> str:
        step_desc = ""
        if state.plan and state.current_step < len(state.plan.steps):
            step_desc = state.plan.steps[state.current_step]
        return f"{task['title']} {step_desc}".strip()

    def _req(self, role: str, agent: str, state: RunState, task, *, step: int = 0,
             artifact=None, knowledge=None) -> RoleRequest:
        return RoleRequest(
            role=role,
            agent=agent,
            soul=self.registry.soul(agent),
            goals=self.registry.goals(agent),
            memory=state.recalled_memory,
            knowledge=list(knowledge if knowledge is not None else state.knowledge),
            task_title=task["title"],
            task_body=task["body"] or "",
            plan=state.plan,
            step=step,
            artifact=artifact,
            prior_verdicts=list(state.verdicts),
        )

    def _event_detail(self, state: RunState) -> str:
        return (f"status={state.status.value} step={state.current_step} "
                f"retries={state.retries} replans={state.replans} tokens={state.tokens_used}")
