"""Onboarding demo (offline). Run: python -m teleraft.onboarding_demo

Shows DESIGN.md §8.1 — the entry point:
  1. A human DMs the onboarding agent (hosted on Hermes Agent / OpenClaw).
  2. It interviews them in plain language.
  3. It compiles a workspace plan and asks a **human** to approve it.
  4. On approve it provisions topics, agents, souls, knowledge sources and heartbeats,
     verifies the result (Tester role), and reports what only a human can do.
  5. The provisioned team immediately runs a real task through the full loop.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from .app import App
from .models import RunStatus
from .onboarding import MockHost, OnboardingAgent
from .telegram.gateway import Callback, Update

ANSWERS = [
    "We run a dev-tools consultancy for platform teams",
    "content, delivery, finance",
    "pricing, legal, refunds",
    "kb/cole/brand-voice.md, kb/shared/personas.csv",
    "weekday-morning",
    "11111111",
]


def main() -> None:
    print("=" * 74)
    print("TeleRaft onboarding — one DM stands up the whole team")
    print("=" * 74)

    # A fresh workspace: no agents, no topics but `general`.
    with tempfile.TemporaryDirectory() as tmp:
        empty_agents = Path(tmp) / "agents"
        empty_agents.mkdir()
        app = App(human_ids=set(), agents_dir=str(empty_agents), sync_knowledge=False)
        host = MockHost()

        print("\nWorkspace before:", app.registry.names() or "(no agents)")

        onb = OnboardingAgent(app, host, user_ref="rick")
        onb.start()
        print("\n----- the interview (Telegram DM) -----")
        for answer in ANSWERS:
            question = onb.answers.next_question()
            print(f"\n  🤖 {question.text}")
            if question.options:
                print(f"     [{' | '.join(question.options)}]")
            print(f"  👤 {answer}")
            onb.answer(answer)

        print("\n----- the plan (human gate) -----")
        print(f"  🤖 {onb.plan.summary()}")
        for line in host.sent[-1][1].splitlines():
            if line.startswith(("agents:", "topics:", "- name:", "  ", "human_ids:")):
                print("    " + line)
        print(f"     buttons: [{' | '.join(host.sent[-1][2])}]")

        print("\n>>> Rick taps ✅ Approve\n")
        report = onb.approve(by_user_id="11111111")

        print("----- what got provisioned -----")
        print("  topics    :", ", ".join(report.topics_created))
        print("  agents    :", ", ".join(report.agents_created))
        print("  sources   :", len(report.sources_added), "registered and ingested")
        print("  heartbeats:", [(j.agent, j.cron) for j in host.jobs()])
        print("  verified  :", "✅ passed" if report.verdict_passed else report.verdict_reasons)
        print("  needs you :")
        for step in report.manual_steps:
            print("     •", step)

        print("\n----- guardrail: an agent cannot approve its own plan -----")
        try:
            onb.approve(by_user_id="Cole")
        except PermissionError as e:
            print("  ⛔", e)

        print("\n----- idempotency: re-running the plan changes nothing -----")
        again = onb.apply(onb.plan)
        print(f"  created: {again.agents_created or '[]'} {again.topics_created or '[]'}"
              f" · skipped {len(again.skipped)} existing items")

        print("\n----- the new team runs a real task immediately -----")
        result = app.gateway.handle_message(
            Update(text="@Cole draft the launch post for the June webinar",
                   user_id="11111111", user_handle="rick", topic="# content",
                   as_task=True, mentions=["Cole"])
        )
        for line in app.client.transcript:
            print("   ", line)
        assert result.status is RunStatus.AWAITING_HUMAN
        app.gateway.handle_callback(
            Callback(data=f"approve|{result.run_id}|review", user_id="11111111")
        )
        _tid, state = app.storage.load_run(result.run_id)
        print(f"\n  built by {state.agent}, tested by {state.tester_agent}, "
              f"approved by a human → {state.status.value}")
        app.close()

    print("\nDone.")


if __name__ == "__main__":
    main()
