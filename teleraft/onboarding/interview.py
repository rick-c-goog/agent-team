"""The onboarding interview (DESIGN.md §3.3.2).

Six questions in plain language. The answers are the *only* input to plan compilation,
which is why the interview is a data structure rather than free-form prompting: given
the same answers you get the same plan, so onboarding is reproducible and testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Question:
    key: str
    text: str
    options: list[str] = field(default_factory=list)
    multi: bool = False
    help: str = ""


# The five pillars from the source article; a team picks the ones it needs.
PILLARS: dict[str, dict] = {
    "leads": {
        "agent": "June",
        "topic": "# leads",
        "owns": ["lead acquisition", "# leads"],
        "soul": "You own lead acquisition: prospect discovery and first-touch drafts.",
        "heartbeat": "0 9 * * 1-5",
        "heartbeat_prompt": "Scan for new prospects and draft first-touch messages.",
    },
    "content": {
        "agent": "Cole",
        "topic": "# content",
        "owns": ["content pipeline", "# content"],
        "soul": "You own audience-facing content: launch posts, newsletters, landing copy.",
        "heartbeat": "0 9 * * 1-5",
        "heartbeat_prompt": "Review the content backlog; claim the highest-priority task.",
    },
    "sales": {
        "agent": "Etta",
        "topic": "# sales",
        "owns": ["sales pipeline", "# sales"],
        "soul": "You own deal momentum: monitor replies and keep the pipeline moving.",
        "heartbeat": "0 */2 * * *",
        "heartbeat_prompt": "Check for unanswered prospect replies and draft follow-ups.",
    },
    "delivery": {
        "agent": "Ray",
        "topic": "# delivery",
        "owns": ["service delivery", "# delivery"],
        "soul": "You own client deliverables, and you review teammates' work rigorously.",
        "heartbeat": "",
        "heartbeat_prompt": "",
    },
    "finance": {
        "agent": "Penn",
        "topic": "# finance",
        "owns": ["finance", "# finance"],
        "soul": "You own invoicing and financial reporting.",
        "heartbeat": "0 8 * * 1",
        "heartbeat_prompt": "Check invoices due this week and draft reminders.",
    },
}


INTERVIEW: list[Question] = [
    Question("business", "What does your business do, and who are the customers?",
             help="One or two sentences is enough — it becomes every agent's context."),
    Question("pillars", "Which pillars do you want agents for?",
             options=list(PILLARS), multi=True,
             help="Start with two or three. You can add one per week later."),
    Question("escalate", "What should an agent never do without asking you first?",
             help="Comma-separated, e.g. pricing, legal, refunds."),
    Question("knowledge", "What should they read? Paste URLs, Drive links, or file paths.",
             help="Comma-separated. Add more any time with /kb add."),
    Question("schedule", "When should they work on their own?",
             options=["weekday-morning", "hourly", "off"]),
    Question("humans", "Which Telegram user IDs may approve work?",
             help="Only these humans can tap Approve — never an agent."),
]


@dataclass
class Answers:
    """Interview answers, normalized. `raw` keeps exactly what the human typed."""

    raw: dict[str, str] = field(default_factory=dict)

    def set(self, key: str, value: str) -> None:
        self.raw[key] = value

    def get(self, key: str, default: str = "") -> str:
        return self.raw.get(key, default)

    # -- normalizers ------------------------------------------------------- #
    def pillars(self) -> list[str]:
        picked = [p.strip().lower() for p in _split(self.get("pillars")) if p.strip()]
        valid = [p for p in picked if p in PILLARS]
        return valid or ["content", "delivery"]      # never fewer than two (see below)

    def escalations(self) -> list[str]:
        return [e.strip().lower() for e in _split(self.get("escalate")) if e.strip()]

    def knowledge_uris(self) -> list[str]:
        return [u.strip() for u in _split(self.get("knowledge")) if u.strip()]

    def human_ids(self) -> list[str]:
        return [h.strip() for h in _split(self.get("humans")) if h.strip()]

    def cron_for(self, pillar: str) -> str:
        choice = self.get("schedule", "weekday-morning").strip().lower()
        if choice == "off":
            return ""
        if choice == "hourly":
            return "0 * * * *"
        return PILLARS[pillar]["heartbeat"]

    def next_question(self) -> Optional[Question]:
        for q in INTERVIEW:
            if q.key not in self.raw:
                return q
        return None

    def complete(self) -> bool:
        return self.next_question() is None


def _split(value: str) -> list[str]:
    return value.replace(";", ",").replace("\n", ",").split(",")
