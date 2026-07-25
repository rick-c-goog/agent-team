"""Entry point and onboarding agent (DESIGN.md §3.3).

One front door: you DM the onboarding agent, answer questions in plain language, and it
provisions the whole team — topics, agents, souls, knowledge sources, heartbeats.

The agent is hosted on **Hermes Agent** or **OpenClaw** behind the small
``OnboardingHost`` adapter, so the host's cron scheduler becomes TeleRaft's heartbeat
substrate and its channel connectors carry the interview.
"""

from .host import OnboardingHost, MockHost, HermesHost, OpenClawHost, ScheduledJob
from .interview import INTERVIEW, Answers, Question
from .plan import WorkspacePlan, compile_plan, plan_to_yaml
from .agent import OnboardingAgent

__all__ = [
    "OnboardingHost",
    "MockHost",
    "HermesHost",
    "OpenClawHost",
    "ScheduledJob",
    "INTERVIEW",
    "Answers",
    "Question",
    "WorkspacePlan",
    "compile_plan",
    "plan_to_yaml",
    "OnboardingAgent",
]
