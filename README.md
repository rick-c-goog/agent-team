# TeleRaft

A reference implementation of the design in [DESIGN.md](DESIGN.md): a multi-agent
team workspace where humans and AI agents collaborate, built on the **Telegram
ecosystem**, orchestrated with the **Anthropic agent loop** (Planner → Orchestrator →
Builder → Tester) expressed as a **checkpointed state graph** with human-in-the-loop
interrupts.

Two things make it usable rather than just architectural:

- **One front door.** An **onboarding agent** hosted on **Hermes Agent** or **OpenClaw**
  interviews you in a DM and provisions the whole team — topics, agents, souls,
  knowledge sources, heartbeats — behind a human approval gate.
- **Grounded agents.** Every agent owns a **knowledge base** (web URLs, Google Drive,
  local `.md`/`.pdf`/`.txt`/`.csv`) retrieved via RAG at plan and build time, with
  **citations the Tester checks**.

It runs **fully offline** out of the box: a deterministic *mock runtime* stands in for
the LLM and a *mock Telegram client* stands in for the Bot API, so you can watch a task
flow through the whole Plan → Build → Test → Review → Learn loop without any API keys.
Real Claude and real Telegram are drop-in adapters behind the same interfaces.

## Quick start (offline demos)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Stand up a whole team by answering six questions (no keys, no network):
python -m teleraft.onboarding_demo

# Watch one task go through the full loop:
python -m teleraft.demo

# Watch a quant research desk catch its own overfitting:
python -m teleraft.quant_demo

# Run the eleven-node factor graph (producers → gates → join → attribution):
python -m teleraft.factor_demo

# Run the tests:
pytest
```

The demos above use synthetic prices, so they need no keys and no network. For real
market data, `pip install -e ".[yfinance]"` and add `--source yfinance` to either quant
demo — same loop, same gates, real adjusted closes.

## Example: an AI quant research desk

[docs/QUANT_TEAM_TUTORIAL.md](docs/QUANT_TEAM_TUTORIAL.md) builds a four-agent quant
research team on top of the same loop, following the *Self-Improving Trading Agent*,
*Multi-Agent Trading Teams*, and *Cross-Market Data & Backtesting* features of
[HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading): agents propose hypotheses,
search parameters in-sample, and a **different** agent re-runs them **out-of-sample** —
so overfitting is caught by held-out data rather than by a reviewer's opinion. A
hypothesis registry records what was tested and refuses to re-test a dead end.

Research spans US, HK, China A-share, crypto and FX, each with its own calendar, costs,
settlement and currency — so a crypto Sharpe is annualised with 365 rather than 252, an
A-share strategy cannot short, and a portfolio mixing HKD with USD is **refused** unless
you supply FX rates rather than being silently summed.

The eleven-node factor graph is built on the same pipeline engine: seven factor
producers, a validator and regime gate per factor, a risk-parity join over the
survivors, and an attribution gate that **refuses** to regress a portfolio against the
factors it was built from. Four of the seven factors block for want of point-in-time
fundamentals rather than being approximated.

Prices come from a one-method loader, so the desk runs on synthetic data offline or on
real adjusted closes via `yfinance` with no other change. The provenance line on every
artifact names which — including the survivorship caveat that free price feeds cannot
fix — so a reviewer never has to guess whether a number came from a market.

It is research tooling only: no broker connectors, no order placement, and every note
waits for a human to tap Approve.

The demo simulates a human handing a task to agent **Cole** in the `# content` topic,
the graph engine planning it, a Builder drafting it, a *different* agent (**Ray**)
adversarially testing it (rejecting v1, passing v2), a human approving via an inline
button, and the **Learn** node writing the lesson back into Cole's memory.

## Run it live on Telegram

To stand this up in a real Telegram workspace — BotFather bots, a supergroup with
topics, a broadcast channel, config, and `python -m teleraft.main` — follow the full
step-by-step guide: **[docs/TELEGRAM_SETUP.md](docs/TELEGRAM_SETUP.md)**.

```bash
pip install -e ".[telegram]"          # add ,anthropic for real Claude
cp teleraft.toml.example teleraft.toml # then fill in ids/tokens (see the guide)
export TELERAFT_BOT_TOKEN="123456:ABC..."
python -m teleraft.main                # long-polls Telegram
```

## How it maps to DESIGN.md

| DESIGN.md section | Code |
|---|---|
| §3.3 Onboarding agent (Hermes / OpenClaw) | [`teleraft/onboarding/`](teleraft/onboarding/agent.py), [`host.py`](teleraft/onboarding/host.py) |
| §4 Agent model (6 elements) | [`teleraft/agents/`](teleraft/agents/registry.py), [`agents/*.yaml`](agents/) |
| §4.1 Knowledge base & RAG | [`teleraft/knowledge/`](teleraft/knowledge/service.py), [`extractors.py`](teleraft/knowledge/extractors.py), [`fetchers.py`](teleraft/knowledge/fetchers.py) |
| §5 Graph engine (POBT loop) | [`teleraft/graph/`](teleraft/graph/engine.py) |
| §6 Task lifecycle | [`teleraft/tasks/service.py`](teleraft/tasks/service.py) |
| §7 Data model | [`teleraft/storage.py`](teleraft/storage.py) |
| §2/§3 Telegram gateway | [`teleraft/telegram/`](teleraft/telegram/gateway.py) |
| §5.2 Learn / self-improvement | [`teleraft/memory/service.py`](teleraft/memory/service.py) |
| Runtimes (Claude / mock / quant) | [`teleraft/runtime/`](teleraft/runtime/base.py) |
| Quant research desk (example) | [`teleraft/quant/`](teleraft/quant/backtest.py), [`runtime/quant.py`](teleraft/runtime/quant.py), [`agents/quant/`](agents/quant/) |

## Architecture at a glance

```
Telegram (mock or live)
   │  updates / callbacks
   ▼
Gateway ──► Task Service ──► Graph Engine ──► Runtime (mock / Claude)
   ▲            │                │   │
   │            ▼                ▼   ▼
 cards      Storage (SQLite)  Registry  Memory
```

Every graph node persists a checkpoint to SQLite, so a run survives a crash or restart
and resumes at the last node. Human gates are true interrupts: the run suspends until
an Approve/Reject callback arrives.
