"""End-to-end demo (offline). Run: python -m teleraft.demo

Simulates the DESIGN.md §8.1 flow:
  1. A human hands a task to Cole in the # content topic (@mention + as-task).
  2. The graph plans it, a Builder drafts it, and a *different* agent (chosen by the
     no-self-grading policy) adversarially tests it — rejecting v1, passing v2.
  3. The task lands In Review; the human taps Approve on their phone.
  4. The Learn node writes the lesson back into Cole's memory.

Everything is printed as a Telegram-style transcript so you can watch the loop.
"""

from __future__ import annotations

from .app import App
from .models import RunStatus
from .telegram.gateway import Callback, Update


def main() -> None:
    app = App(human_ids={"rick"})
    client = app.client  # MockTelegramClient with a transcript

    print("=" * 72)
    print("TeleRaft demo — Planner → Orchestrator → Builder → Tester loop on Telegram")
    print("=" * 72)
    print("\nTeam:", ", ".join(app.registry.names()))
    print()

    # 1) Human hands off a task in # content, mentioning Cole.
    result = app.gateway.handle_message(
        Update(
            text="@Cole write the launch post for the June webinar",
            user_id="rick",
            user_handle="rick",
            topic="# content",
            as_task=True,
            mentions=["Cole"],
        )
    )

    _dump(client)

    assert result.status is RunStatus.AWAITING_HUMAN, "should be suspended at the review gate"
    run_id = result.run_id
    task_id = app.storage.load_run(run_id)[1].task_id

    # 2) Human approves the draft via the inline Approve button.
    print("\n>>> Rick taps ✅ Approve on the review card\n")
    app.gateway.handle_callback(Callback(data=f"approve|{run_id}|review", user_id="rick"))

    _dump(client, tail=True)

    # 3) Show the durable results: memory writeback + audit trail.
    print("\n" + "-" * 72)
    print("Cole's memory after the run (the self-improvement writeback):")
    for row in app.storage.memories_for("Cole"):
        print(f"  • [{row['source']}] {row['content_md']}")

    print("\nAudit trail (graph node checkpoints) for", run_id + ":")
    for ev in app.storage.run_events(run_id):
        print(f"  {ev['seq']:>2}. {ev['node']:<14} {ev['detail']}")

    print("\nBroadcast activity feed:")
    for post in client.channel_posts:
        print("  •", post)

    # 4) Demonstrate a human-only gate is enforced: a bot cannot approve.
    print("\n" + "-" * 72)
    print("Security check: a non-human user_id tries to approve → blocked.")
    app2, run2 = _fresh_run_awaiting_review()
    before = len(app2.client.channel_posts)
    app2.gateway.handle_callback(Callback(data=f"approve|{run2}|review", user_id="Cole"))
    blocked = [p for p in app2.client.channel_posts[before:] if "blocked" in p]
    print("  ", blocked[0] if blocked else "(unexpected: not blocked!)")
    task2 = app2.storage.load_run(run2)[1]
    print("   task still In Review:", task2.status is RunStatus.AWAITING_HUMAN)

    app.close()
    app2.close()
    print("\nDone.")


def _fresh_run_awaiting_review():
    app = App(human_ids={"rick"})
    result = app.gateway.handle_message(
        Update(text="@Cole draft the weekly newsletter", user_id="rick", user_handle="rick",
               topic="# content", as_task=True, mentions=["Cole"])
    )
    return app, result.run_id


def _dump(client, tail: bool = False) -> None:
    label = "Telegram transcript (continued)" if tail else "Telegram transcript"
    print(f"\n----- {label} -----")
    for line in client.transcript:
        print(" ", line)
    client.transcript.clear()


if __name__ == "__main__":
    main()
