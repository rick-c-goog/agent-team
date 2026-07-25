"""Quant research team demo (offline). Run: python -m teleraft.quant_demo

Shows both Vibe-Trading features mapped onto the TeleRaft loop:

  * **Multi-Agent Trading Teams** — Quinn (factor researcher) claims the task, Bailey
    (backtester) verifies it out-of-sample. Nobody grades their own work.
  * **Self-Improving Trading Agent** — hypothesis → in-sample search → out-of-sample
    verification → supported or invalidated *with a reason* → the registry blocks the
    re-test of a dead idea.

Two scenarios run: one where an edge survives, and one where nothing does. The second
is the more important one — a research desk that never returns a negative result is
broken.

No API keys, no network, no orders. Research drafts only, approved by a human.
"""

from __future__ import annotations

from pathlib import Path

from .app import App
from .models import RunStatus
from .quant.hypothesis import DuplicateHypothesis
from .telegram.gateway import Callback, Update

QUANT_AGENTS = str(Path(__file__).resolve().parent.parent / "agents" / "quant")
HUMAN = "11111111"
RULE = "-" * 78


def main() -> None:
    print("=" * 78)
    print("TeleRaft quant desk — Planner → Orchestrator → Builder → Tester on real numbers")
    print("=" * 78)

    app = App(human_ids={HUMAN}, agents_dir=QUANT_AGENTS)
    print("\nDesk:", ", ".join(app.registry.names()))
    print("Data: synthetic, deterministic, offline — swap in a live loader per §8\n")

    # ================================================================== #
    print(RULE)
    print("SCENARIO A — an edge that survives out-of-sample\n")
    print("  👤 rick: @Quinn is there a momentum edge on SPY?\n")
    result_a = _ask(app, "@Quinn is there a momentum edge on SPY")
    _dump(app)

    if result_a is not None and result_a.status is RunStatus.AWAITING_HUMAN:
        print("  👤 rick taps ✅ Approve on the research note\n")
        app.gateway.handle_callback(
            Callback(data=f"approve|{result_a.run_id}|review", user_id=HUMAN))
        _dump(app)
        _tid, state = app.storage.load_run(result_a.run_id)
        print(f"  → built by {state.agent}, verified out-of-sample by "
              f"{state.tester_agent}, approved by a human → {state.status.value}")

    # ================================================================== #
    print("\n" + RULE)
    print("SCENARIO B — an idea that does not survive (the honest negative result)\n")
    print("  👤 rick: @Quinn does momentum have an edge on NVDA?\n")
    _ask(app, "@Quinn does momentum have an edge on NVDA")
    _dump(app, only_verdicts=True)

    # ================================================================== #
    print("\n" + RULE)
    print("THE RESEARCH RECORD — /hyp list\n")
    for h in app.hypotheses.list():
        print("  " + h.short())
        if h.invalidated_reason:
            print(f"      ↳ {h.invalidated_reason}")
        for r in h.results:
            sample = "IS " if r["start"] <= _midpoint(h) else "OOS"
            print(f"      · {sample} {r['spec']:<40} Sharpe {r['sharpe']:+.2f}  "
                  f"maxDD {r['max_drawdown']:.1%}  CAGR {r['cagr']:+.1%}")

    # ================================================================== #
    print("\n" + RULE)
    print("SELF-IMPROVEMENT — the desk refuses to re-test a dead idea\n")
    dead = [h for h in app.hypotheses.list() if h.status.value == "invalidated"]
    if dead:
        try:
            app.hypotheses.propose("Quinn", dead[0].statement, universe=dead[0].universe)
            print("  (unexpected: the registry allowed a re-test)")
        except DuplicateHypothesis as e:
            print(f"  ⛔ {e}")
            print("  → tomorrow's heartbeat must design around this, not repeat it")
    else:
        print("  (every hypothesis survived this run)")

    # ================================================================== #
    print("\n" + RULE)
    print("WHAT THE DESK LEARNED — written back to memory\n")
    for agent in ("Quinn", "Bailey"):
        for row in app.storage.memories_for(agent):
            print(f"  • {agent}: {row['content_md']}")

    print("\n" + RULE)
    print("Research output only. Not investment advice. This system places no orders.")
    app.close()


def _ask(app, text: str):
    return app.gateway.handle_message(
        Update(text=text, user_id=HUMAN, user_handle="rick", topic="# research",
               as_task=True, mentions=["Quinn"])
    )


def _midpoint(h) -> str:
    """Crude in-sample/out-of-sample label for display only."""
    starts = sorted({r["start"] for r in h.results})
    return starts[0] if starts else ""


def _dump(app, only_verdicts: bool = False) -> None:
    for line in app.client.transcript:
        if only_verdicts and not any(m in line for m in ("✅", "❌", "🚨", "🧭", "Candidate")):
            continue
        print("   ", line)
    app.client.transcript.clear()


if __name__ == "__main__":
    main()
