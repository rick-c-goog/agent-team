"""Gateway — Telegram ⇄ TeleRaft (DESIGN.md §3.1, §6).

Two directions:

  inbound   Telegram updates/callbacks → router → task creation, claims, engine runs,
            and human gate decisions (approve/reject), with human-only enforcement.

  outbound  the engine's ``notify(event, ...)`` hook → Telegram messages: task cards,
            in-thread plan/progress/verdict lines, gate cards with inline keyboards,
            and the broadcast activity feed / digest.

Design rule (DESIGN.md §2): Telegram is the interaction surface, storage is the source
of truth. Every card carries a stable task_id / run_id so state reconciles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import TaskStatus
from ..tasks.service import TaskConflict
from .client import Button, TelegramClient

_STATUS_EMOJI = {
    "todo": "🟡",
    "in_progress": "🔵",
    "in_review": "🟣",
    "done": "🟢",
    "closed": "⚪️",
}


@dataclass
class Update:
    """A normalized inbound Telegram message."""

    text: str
    user_id: str
    user_handle: str = "human"
    topic: str = "general"
    is_bot: bool = False
    as_task: bool = False
    mentions: list[str] = field(default_factory=list)


@dataclass
class Callback:
    """A normalized inbound inline-button press."""

    data: str
    user_id: str
    reason: str = ""      # supplied via forced-reply for a Reject


class Gateway:
    def __init__(
        self,
        client: TelegramClient,
        storage,
        tasks,
        registry,
        human_ids: set[str],
        # Empty by default on purpose: an unset chat id falls through to the client's
        # own configured chat (LiveTelegramClient does `chat_id or self.group_chat_id`).
        # A non-empty placeholder here would be sent to Telegram verbatim and fail with
        # "chat not found" — which is exactly what a sentinel like "workspace" did.
        group_chat_id: str = "",
    ):
        self.client = client
        self.storage = storage
        self.tasks = tasks
        self.registry = registry
        self.human_ids = set(human_ids)
        self.group_chat_id = group_chat_id
        self.engine = None       # wired after construction (engine needs our notify hook)
        self.knowledge = None    # optional KnowledgeService for /kb commands
        self.hypotheses = None   # optional HypothesisRegistry for /hyp commands
        self.bot_username = ""   # set by the runner after getMe, to avoid self-replies

    def attach_engine(self, engine) -> None:
        self.engine = engine

    def attach_knowledge(self, knowledge) -> None:
        self.knowledge = knowledge

    def attach_hypotheses(self, registry) -> None:
        self.hypotheses = registry

    # ================================================================== #
    # Inbound
    # ================================================================== #
    def handle_message(self, u: Update):
        """Route a message. A mention/`as_task` becomes a task; a mentioned agent claims it."""
        if u.text.strip().startswith("/kb"):
            return self.handle_kb_command(u)
        if u.text.strip().startswith("/hyp"):
            return self.handle_hyp_command(u)

        if u.text.strip().startswith(("/agents", "/help")):
            return self.handle_help_command(u)

        agent_names = set(self.registry.names())
        mentioned_agents = [m for m in u.mentions if m in agent_names]

        is_task = u.as_task or u.text.strip().startswith("/task") or bool(mentioned_agents)
        if not is_task:
            # A message that clearly addresses someone (`@Name …`) but names no known
            # agent used to vanish in silence — the single most confusing failure mode
            # in a live workspace. Answer it instead.
            unresolved = self._unresolved_mention(u, agent_names)
            if unresolved:
                self._report_unknown_agent(u, unresolved, agent_names)
            return None  # ordinary chatter; nothing to orchestrate

        title = _strip_command(u.text)
        task_id = self.tasks.create(topic=u.topic, title=title, body="", created_by=u.user_handle)
        owner = mentioned_agents[0] if mentioned_agents else None
        self._post_task_card(task_id, claimable=owner is None)

        if owner is not None:
            # Agent auto-claims on @mention and the run starts (DESIGN.md §6).
            return self._run_task(task_id, owner)
        return task_id

    # -- addressing an agent that does not exist --------------------------- #
    def _unresolved_mention(self, u: Update, agent_names: set[str]) -> Optional[str]:
        """The @handle a message opens with, when it names no known agent.

        Only the *leading* mention counts: `@Someone do X` is unambiguously an attempt
        to address someone, whereas an @handle mid-sentence is usually just talk.
        """
        first = u.text.strip().split()[:1]
        if not first or not first[0].startswith("@"):
            return None
        handle = first[0][1:].strip(",:;!?").strip()
        if not handle:
            return None
        lowered = {n.lower() for n in agent_names}
        if handle.lower() in lowered:
            return None
        # Don't answer back when the workspace bot itself is addressed, or when the
        # handle resolved to an agent through the username map.
        if self.bot_username and handle.lower() == self.bot_username.lower():
            return None
        if any(m in agent_names for m in u.mentions):
            return None
        return handle

    def _report_unknown_agent(self, u: Update, handle: str, agent_names: set[str]) -> None:
        known = ", ".join(f"@{n}" for n in sorted(agent_names)) or "(none loaded)"
        text = (
            f"🤷 I don't have an agent called `@{handle}`.\n"
            f"Agents in this workspace: {known}\n"
            f"Use `/agents` for details, or `/task <what to do>` to open unclaimed work."
        )
        if not agent_names:
            text += ("\n⚠️ No agents are loaded at all — check `agents_dir` in "
                     "teleraft.toml (a quant desk needs `agents/quant`).")
        self.client.send_message(self.group_chat_id, text, thread=u.topic)

    # -- /agents · /help --------------------------------------------------- #
    def handle_help_command(self, u: Update):
        """Show who is on the team and what they own — the first thing to check when
        an @mention seems to do nothing."""
        names = self.registry.names()
        if not names:
            self.client.send_message(
                self.group_chat_id,
                "⚠️ No agents are loaded. Check `agents_dir` in teleraft.toml — "
                "e.g. `agents/quant` for a quant desk.",
                thread=u.topic,
            )
            return []

        lines = ["🤖 *Agents in this workspace*"]
        for name in sorted(names):
            goals = self.registry.goals(name) or {}
            owns = ", ".join(goals.get("owns", [])) or "—"
            role = self.registry.role(name)
            lines.append(f"  *@{name}* ({role}) — owns: {owns}")
            escalate = goals.get("escalate_when", [])
            if escalate:
                lines.append(f"      escalates on: {', '.join(escalate)}")
        lines += [
            "",
            "*Commands*: `/task <what to do>` · `/agents` · `/kb list` · `/hyp list`",
            "Address an agent directly: `@Name <what to do>`",
        ]
        self.client.send_message(self.group_chat_id, "\n".join(lines), thread=u.topic)
        return names

    # -- /kb — knowledge base management (DESIGN.md §4.1.4) ---------------- #
    def handle_kb_command(self, u: Update):
        """`/kb add <uri> [--team]` · `/kb list` · `/kb sync [id]` · `/kb remove <id>`."""
        if self.knowledge is None:
            self.client.send_message(self.group_chat_id, "Knowledge base is not configured.")
            return None

        parts = u.text.strip().split()
        sub = parts[1].lower() if len(parts) > 1 else "list"
        args = parts[2:]
        agent = self._agent_for_topic(u.topic)

        if sub == "list":
            rows = self.knowledge.health()
            if not rows:
                text = "📚 No knowledge sources registered yet. Add one with `/kb add <url>`."
            else:
                lines = ["📚 *Knowledge sources*"]
                for r in rows:
                    mark = {"ok": "✅", "error": "❌"}.get(r["status"], "⏳")
                    line = (f"{mark} `{r['id']}` {r['agent']} · {r['type']} · {r['uri']}"
                            f" — {r['docs']} docs, {r['chunks']} chunks")
                    if r["error"]:
                        line += f"\n     ⚠️ {r['error']}"
                    lines.append(line)
                text = "\n".join(lines)
            self.client.send_message(self.group_chat_id, text, thread=u.topic)
            return rows

        if sub == "add":
            if not args:
                self.client.send_message(self.group_chat_id,
                                         "Usage: `/kb add <url|drive://…|path> [--team]`",
                                         thread=u.topic)
                return None
            uri = args[0]
            scope = "team" if "--team" in args else "agent"
            if agent is None and scope == "agent":
                self.client.send_message(
                    self.group_chat_id,
                    f"No agent owns topic {u.topic}; use `--team` or post in an agent's topic.",
                    thread=u.topic,
                )
                return None
            source_id = self.knowledge.add_source(
                agent=agent, type_=_source_type(uri), uri=uri, scope=scope,
                created_by=u.user_handle,
            )
            report = self.knowledge.sync_source(source_id)
            self.client.send_message(self.group_chat_id,
                                     f"📚 {report.summary()}", thread=u.topic)
            return report

        if sub == "sync":
            reports = ([self.knowledge.sync_source(args[0])] if args
                       else self.knowledge.sync_all(agent))
            body = "\n".join(r.summary() for r in reports) or "nothing to sync"
            self.client.send_message(self.group_chat_id, f"📚 Sync:\n{body}", thread=u.topic)
            return reports

        if sub == "remove":
            if not args:
                self.client.send_message(self.group_chat_id, "Usage: `/kb remove <source_id>`",
                                         thread=u.topic)
                return None
            self.knowledge.remove_source(args[0])
            self.client.send_message(self.group_chat_id, f"📚 Removed `{args[0]}`.",
                                     thread=u.topic)
            return args[0]

        self.client.send_message(self.group_chat_id,
                                 "Usage: `/kb add|list|sync|remove`", thread=u.topic)
        return None

    # -- /hyp — hypothesis registry (quant research) ----------------------- #
    def handle_hyp_command(self, u: Update):
        """`/hyp list [status]` · `/hyp show <id>` — the research board.

        Read-only by design: hypotheses are created and killed by the research loop,
        not by chat, so the registry stays an honest record of what was actually tested.
        """
        if self.hypotheses is None:
            self.client.send_message(self.group_chat_id, "No hypothesis registry configured.")
            return None

        parts = u.text.strip().split()
        sub = parts[1].lower() if len(parts) > 1 else "list"
        args = parts[2:]

        if sub == "show" and args:
            try:
                h = self.hypotheses.get(args[0])
            except KeyError:
                self.client.send_message(self.group_chat_id, f"Unknown hypothesis `{args[0]}`.",
                                         thread=u.topic)
                return None
            lines = [h.short(), f"universe: {h.universe or '—'} · proposed by {h.agent or '—'}"]
            if h.invalidated_reason:
                lines.append(f"invalidated: {h.invalidated_reason}")
            for r in h.results:
                lines.append(
                    f"  · {r.get('spec','?')} [{r.get('start','')}→{r.get('end','')}] "
                    f"Sharpe {r.get('sharpe', 0):.2f}, maxDD {r.get('max_drawdown', 0):.1%}"
                )
            lineage = self.hypotheses.lineage(h.id)
            if len(lineage) > 1:
                lines.append("lineage: " + " → ".join(x.id for x in lineage))
            self.client.send_message(self.group_chat_id, "\n".join(lines), thread=u.topic)
            return h

        status = args[0] if (sub == "list" and args) else None
        rows = self.hypotheses.list(status=status)
        if not rows:
            text = "🔬 No hypotheses recorded yet."
        else:
            text = "\n".join(["🔬 *Hypothesis registry*"] + [
                "  " + h.short() + (f"\n      ↳ {h.invalidated_reason}"
                                    if h.invalidated_reason else "")
                for h in rows
            ])
        self.client.send_message(self.group_chat_id, text, thread=u.topic)
        return rows

    def _agent_for_topic(self, topic: str) -> Optional[str]:
        """Which agent owns a topic — used to scope `/kb add` and uploads."""
        for name in self.registry.names():
            owns = (self.registry.goals(name) or {}).get("owns", [])
            if any(topic == o or topic in o for o in owns):
                return name
        return None

    def handle_callback(self, cb: Callback):
        """Handle Claim / Approve / Reject / Adjust presses."""
        action, _, rest = cb.data.partition("|")
        if action == "claim":
            return self._on_claim(rest, cb)
        if action in ("approve", "reject", "adjust"):
            return self._on_gate_decision(action, rest, cb)
        raise ValueError(f"unknown callback action {action!r}")

    def _on_claim(self, task_id: str, cb: Callback):
        try:
            self.tasks.claim(task_id, cb.user_id)
        except TaskConflict:
            return None
        self._refresh_task_card(task_id)
        return task_id

    def _on_gate_decision(self, action: str, rest: str, cb: Callback):
        run_id, _, _gate = rest.partition("|")
        # Human-only gate enforcement (DESIGN.md §11): only allow-listed humans decide.
        if cb.user_id not in self.human_ids:
            self.client.send_channel(
                f"⛔ blocked non-human gate decision on {run_id} by {cb.user_id}"
            )
            return None
        assert self.engine is not None, "engine not attached"
        result = self.engine.resume(run_id, action, reason=cb.reason, user_id=cb.user_id)
        return self._present(result)

    # ================================================================== #
    # Running a task through the graph
    # ================================================================== #
    def _run_task(self, task_id: str, owner: str):
        assert self.engine is not None, "engine not attached"
        self._refresh_task_card(task_id)
        result = self.engine.start(task_id, owner)
        return self._present(result)

    def _present(self, result):
        """After a run advances, reflect any resulting state to Telegram."""
        # Gate cards are rendered by notify(gate_*). Here we only surface terminal state.
        self._refresh_task_card(result_task_id := self._task_of_run(result.run_id))
        return result

    def _task_of_run(self, run_id: str) -> Optional[str]:
        loaded = self.storage.load_run(run_id)
        return loaded[1].task_id if loaded else None

    # ================================================================== #
    # Outbound — engine notify hook (DESIGN.md §5.2 events → Telegram)
    # ================================================================== #
    def notify(self, event: str, **data):
        handler = getattr(self, f"_notify_{event}", None)
        if handler:
            handler(**data)

    def _thread(self, task) -> str:
        return f"#{task['id']} · {task['topic']}"

    def _notify_plan(self, task, plan, run_id, agent):
        lines = [f"🧭 *Plan* by {agent} — acceptance criteria:"]
        lines += [f"  {i+1}. {c}" for i, c in enumerate(plan.criteria)]
        lines.append("Steps: " + "; ".join(plan.steps))
        self.client.send_message(self.group_chat_id, "\n".join(lines), thread=self._thread(task))

    def _notify_gate_plan(self, task, plan, run_id, agent):
        buttons = [
            Button("✅ Approve plan", f"approve|{run_id}|plan"),
            Button("✏️ Adjust", f"adjust|{run_id}|plan"),
        ]
        self.client.send_message(
            self.group_chat_id,
            f"⏸️ Plan needs your sign-off (touches an escalation area). Agent: {agent}",
            buttons=buttons, thread=self._thread(task),
        )

    def _notify_knowledge(self, task, run_id, agent, passages):
        cited = " · ".join(p.cite() for p in passages[:4])
        self.client.send_message(
            self.group_chat_id,
            f"📚 {agent} retrieved {len(passages)} passage(s): {cited}",
            thread=self._thread(task),
        )

    def _notify_progress(self, task, run_id, agent, text):
        self.client.send_message(self.group_chat_id, f"🔧 {agent}: {text}", thread=self._thread(task))

    def _notify_verdict(self, task, run_id, verdict, tester):
        if verdict.passed:
            self.client.send_message(self.group_chat_id, f"✅ {tester} passed step {verdict.step + 1}",
                                     thread=self._thread(task))
        else:
            reasons = "; ".join(verdict.reasons)
            self.client.send_message(self.group_chat_id,
                                     f"❌ {tester} rejected step {verdict.step + 1}: {reasons}",
                                     thread=self._thread(task))

    def _notify_gate_review(self, task, run_id, artifact, agent, tester):
        self._refresh_task_card(task["id"])
        content = artifact.content if artifact else "(no artifact)"
        files = ", ".join(artifact.files) if artifact and artifact.files else "—"
        buttons = [
            Button("✅ Approve", f"approve|{run_id}|review"),
            Button("❌ Reject", f"reject|{run_id}|review"),
        ]
        text = (
            f"🟣 *In Review* — owner: {agent} 🤖 · tested by: {tester} 🤖 ✅\n"
            f"Draft: {content}\nFiles: {files}"
        )
        if artifact and artifact.citations:
            text += "\n📚 Sources: " + " · ".join(c.render() for c in artifact.citations)
        self.client.send_message(self.group_chat_id, text, buttons=buttons, thread=self._thread(task))
        self.client.send_channel(f"👀 Review needed: #{task['id']} {task['title']}")

    def _notify_escalate(self, run_id, task_id, reason):
        task = self.storage.get_task(task_id)
        self.client.send_message(self.group_chat_id, f"🚨 Escalation: {reason}",
                                 thread=self._thread(task))
        self.client.send_channel(f"🚨 Escalation on #{task_id}: {reason}")

    def _notify_done(self, task, run_id, agent, lessons):
        self._refresh_task_card(task["id"])
        if lessons:
            self.client.send_message(
                self.group_chat_id,
                "🧠 Learned: " + " | ".join(lessons),
                thread=self._thread(task),
            )
        self.client.send_channel(f"🟢 Done: #{task['id']} {task['title']} (by {agent})")

    def _notify_soul_amendment_proposed(self, agent, lesson, task_id):
        self.client.send_channel(
            f"📜 Soul amendment for {agent} (recurring lesson): {lesson}"
        )

    # ================================================================== #
    # Task cards
    # ================================================================== #
    def _post_task_card(self, task_id: str, claimable: bool):
        task = self.storage.get_task(task_id)
        buttons = [Button("Open board", "board|open")]
        if claimable:
            buttons.insert(0, Button("Claim", f"claim|{task_id}"))
        mid = self.client.send_message(self.group_chat_id, self._card_text(task),
                                       buttons=buttons, thread=self._thread(task))
        self.tasks.set_card_message(task_id, mid)

    def _refresh_task_card(self, task_id: Optional[str]):
        if not task_id:
            return
        task = self.storage.get_task(task_id)
        if not task or not task["tg_card_message_id"]:
            return
        claimable = task["status"] == TaskStatus.TODO.value and not task["owner"]
        buttons = [Button("Open board", "board|open")]
        if claimable:
            buttons.insert(0, Button("Claim", f"claim|{task_id}"))
        self.client.edit_message_text(self.group_chat_id, task["tg_card_message_id"],
                                      self._card_text(task), buttons=buttons)

    @staticmethod
    def _card_text(task) -> str:
        emoji = _STATUS_EMOJI.get(task["status"], "•")
        owner = task["owner"] or "unclaimed"
        status_label = task["status"].replace("_", " ").title()
        return (f"{emoji} #{task['id']} · {status_label} · {task['topic']}\n"
                f"{task['title']}\n"
                f"owner: {owner}")


def _source_type(uri: str) -> str:
    """Infer a knowledge source type from its URI (DESIGN.md §4.1.1)."""
    lowered = uri.lower()
    if lowered.startswith("drive://") or "drive.google.com" in lowered:
        return "gdrive"
    if lowered.startswith(("http://", "https://")):
        return "web"
    return "file"


def _strip_command(text: str) -> str:
    t = text.strip()
    for token in ("/task",):
        if t.startswith(token):
            t = t[len(token):].strip()
    # strip a leading @mention for a cleaner title
    words = t.split()
    if words and words[0].startswith("@"):
        words = words[1:]
    return " ".join(words).strip() or "Untitled task"
