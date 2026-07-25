# TeleRaft — Telegram Setup & Configuration Guide

This guide takes you from nothing to a running TeleRaft workspace inside Telegram: a
supergroup where humans and AI agents collaborate, tasks flow through the
Planner → Orchestrator → Builder → Tester loop, and every external‑facing deliverable
waits for a human's tap on **Approve**.

It is grounded in the code in this repo — every command, file, and config key below
exists. Read [DESIGN.md](../DESIGN.md) for the architecture and [README.md](../README.md)
for the offline demo.

---

## Contents

1. [How it maps to Telegram](#1-how-it-maps-to-telegram)
2. [Prerequisites](#2-prerequisites)
3. [Step 1 — Create the bots (BotFather)](#3-step-1--create-the-bots-botfather)
4. [Step 2 — Create the workspace supergroup + topics](#4-step-2--create-the-workspace-supergroup--topics)
5. [Step 3 — Create the broadcast channel](#5-step-3--create-the-broadcast-channel)
6. [Step 4 — Collect the IDs you need](#6-step-4--collect-the-ids-you-need)
7. [Step 5 — Install and configure TeleRaft](#7-step-5--install-and-configure-teleraft)
8. [Step 6 — Define your agents](#8-step-6--define-your-agents)
9. [Step 7 — Run it](#9-step-7--run-it)
10. [Step 8 — Verify end-to-end](#10-step-8--verify-end-to-end)
11. [Daily use: commands, buttons, gates](#11-daily-use-commands-buttons-gates)
12. [Going live with real Claude](#12-going-live-with-real-claude)
13. [Security & permissions checklist](#13-security--permissions-checklist)
14. [Polling vs webhooks](#14-polling-vs-webhooks)
15. [Configuration reference](#15-configuration-reference)
16. [Troubleshooting](#16-troubleshooting)
17. [Advanced: one bot per agent](#17-advanced-one-bot-per-agent)
18. [The onboarding agent (Hermes / OpenClaw)](#18-the-onboarding-agent-hermes--openclaw)
19. [Knowledge bases: giving agents something to read](#19-knowledge-bases-giving-agents-something-to-read)

> **Shortcut:** §18 sets up the *onboarding agent*, which automates most of steps 4–8
> through a chat interview. Read §3 (bots) first either way — only a human can create a
> Telegram bot — then jump to §18 if you'd rather answer six questions than edit YAML.

---

## 1. How it maps to Telegram

| TeleRaft concept | Telegram primitive |
|---|---|
| Workspace / server | One **supergroup** with **Topics (forum mode)** enabled |
| Channel (e.g. `# content`) | A **forum topic** in that supergroup |
| Activity feed / daily digest | A separate **broadcast Channel** the bot posts to |
| Task card, board, `/task` command | The **workspace bot** (`@YourWorkspace_Bot`) |
| Agent identity (Cole, Ray, Penn) | Either the workspace bot with name attribution (**MVP**), or one **bot account per agent** (advanced) |
| Task thread | Messages inside a topic |
| Approve / Reject / Claim | **Inline keyboard** buttons on cards |

There are two topologies. Start with the first; graduate to the second when you want
each agent to literally post as itself:

- **MVP (this guide's default).** One **workspace bot** handles all input and output.
  Agents are addressed by name (`@Cole`) and the bot attributes messages
  (`🔧 Cole: …`). Simplest to stand up — one token, one bot in the group.
- **Full per-agent (see §17).** Each agent gets its own bot account with its own
  avatar, DMs, and native @mention. More setup; strongest sense of identity.

---

## 2. Prerequisites

- A Telegram account and the Telegram app (mobile or desktop).
- Python 3.11+ on the machine that will run the bot (your "computer" in Raft terms —
  a laptop for testing or a cloud VM for always-on).
- 10 minutes.
- (Optional, for real AI) an `ANTHROPIC_API_KEY`. You can do the **entire** setup and a
  full end-to-end dry run with the built-in **mock runtime** and **no API key**.

---

## 3. Step 1 — Create the bots (BotFather)

Open a chat with **[@BotFather](https://t.me/BotFather)** in Telegram.

### 3.1 Create the workspace bot

1. Send `/newbot`.
2. Give it a display name, e.g. **TeleRaft**.
3. Give it a username ending in `bot`, e.g. `MyTeam_TeleRaft_Bot`.
4. BotFather replies with a **token** like `123456:ABC-DEF...`. **Copy it** — this is
   your `workspace_bot_token`. Treat it like a password.

### 3.2 Turn OFF privacy mode (required)

By default a bot only sees messages that @mention it or reply to it. TeleRaft's runner
needs the workspace bot to read group messages (to catch `/task` and mentions of
agents), so disable privacy mode:

1. In BotFather send `/mybots` → pick your workspace bot.
2. **Bot Settings → Group Privacy → Turn off.**
3. It should read *"Privacy mode is disabled."*

> ⚠️ This is a deliberate trade-off (DESIGN.md §11): the workspace bot can read group
> messages. Keep the workspace group private and invite-only.

### 3.3 (Recommended) Create one bot per agent

If you want real agent identities now, repeat `/newbot` for each pillar agent
(`@Cole_TR_Bot`, `@Ray_TR_Bot`, `@Penn_TR_Bot`). For each, also set a profile that makes
identity tangible:

- `/setuserpic` — give each agent a distinct avatar.
- `/setdescription` — paste a one-line version of the agent's soul.

You do **not** need per-agent tokens for the MVP outbound path (§17 covers wiring them).
Creating the agent bots means their `@username` mentions resolve to real accounts and
they can be DM'd. Record each `name → @username` pair for the config.

---

## 4. Step 2 — Create the workspace supergroup + topics

1. In Telegram: **New Group** → add at least yourself (and teammates later) → name it,
   e.g. **"Acme Workspace"**.
2. Add your **workspace bot** to the group.
3. Promote the workspace bot to **admin** (Group → Edit → Administrators → Add):
   grant at least *Manage Topics* and *Pin/Delete Messages*. Admin also implicitly makes
   a group a supergroup, which topics require.
4. Enable **Topics**: Group → Edit → toggle **Topics** on. The group is now a forum.
5. Create one topic per channel your agents own. With the starter agents in
   [`agents/`](../agents/) that's:
   - `# content` (Cole)
   - `# delivery` (Ray)
   - `# finance` (Penn)
   - `# admin` (soul-amendment proposals, escalations)

   The topic name must match the `owns`/topic label in each `agents/*.yaml`.
6. If you created per-agent bots, add them to the group too. Promote **admin-role**
   agents (e.g. Penn is `role: admin`) to Telegram admins; leave member agents as plain
   members (DESIGN.md §11 — capabilities follow the Telegram role).

> Only a **human** should be the group **owner**. Never transfer ownership to a bot.

---

## 5. Step 3 — Create the broadcast channel

This is the "catch up in one place" activity feed: review-needed pings, completions,
escalations, and soul-amendment proposals.

1. **New Channel** → name it, e.g. **"Acme TeleRaft Feed"** → Private is fine.
2. Add your **workspace bot** as an **administrator** with *Post Messages* permission.
3. Note its `@handle` (public) or numeric id (private — see §6).

> ⚠️ **Telegram usernames cannot contain hyphens or dots.** A handle is 5–32 characters,
> starts with a letter, and may contain only letters, digits and underscores. So
> `@ai-quant-research-ch` is not a valid handle and will never resolve — it would be
> `@ai_quant_research_ch`. Check the real handle at `t.me/<handle>`. **A private channel
> has no handle at all**: use its numeric `-100…` id (§6.3). TeleRaft validates this at
> startup and tells you which rule was broken.

The channel is optional: if you leave `channel_id` blank, TeleRaft simply skips the feed
and everything still works inside the group.

---

## 6. Step 4 — Collect the IDs you need

You need four kinds of value. The easiest way to read them all is the bot's own update
feed.

### 6.1 Your human user id

Message **[@userinfobot](https://t.me/userinfobot)** — it replies with your numeric id
(e.g. `11111111`). This goes in `human_ids`. **Only ids in this list can Approve/Reject.**

### 6.2 The group id and topic thread ids (via getUpdates)

1. In the group, post a message **in each topic** (e.g. type `hello` in `# content`,
   `# delivery`, …). Also send `/task ping` once.
2. In a browser, open (replacing `<TOKEN>`):

   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

3. In the JSON, for each message find:
   - `message.chat.id` → your **`group_chat_id`** (a big negative number like
     `-1001234567890`; it's the same for every topic).
   - `message.message_thread_id` → the **thread id of that topic**. Match each id to the
     topic you posted in. These become `topic_threads` (label → thread id).

   > Tip: the *General* topic has no `message_thread_id`. Messages you posted inside a
   > named topic carry it.

### 6.3 The channel id

If your channel is public, use `@handle`. If private, forward one of its posts to
**[@userinfobot](https://t.me/userinfobot)**, or post in it and read
`channel_post.chat.id` from `getUpdates` (a `-100…` number). This is `channel_id`.

> `getUpdates` and webhooks are mutually exclusive, and `getUpdates` "consumes" updates.
> That's fine here — just don't run `python -m teleraft.main` at the same time you're
> hitting `getUpdates` by hand.

---

## 7. Step 5 — Install and configure TeleRaft

From the repo root:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[telegram]"          # add ,anthropic for real Claude; ,dev for tests
```

Create your config from the template:

```bash
cp teleraft.toml.example teleraft.toml
```

Edit `teleraft.toml` with the values from Step 4. A minimal working file:

```toml
[telegram]
workspace_bot_token = "123456:ABC-DEF..."     # better: leave blank, use env var
group_chat_id = "-1001234567890"
channel_id = "@acme_teleraft_feed"
human_ids = ["11111111"]

[telegram.topic_threads]
"# content"  = "2"
"# delivery" = "3"
"# finance"  = "4"
"# admin"    = "5"

[telegram.agent_usernames]                     # only if you made per-agent bots
Cole = "@Cole_TR_Bot"
Ray  = "@Ray_TR_Bot"
Penn = "@Penn_TR_Bot"

[app]
runtime_engine = "mock"                         # start offline; switch to "claude" later
db_path = "teleraft_data/teleraft.db"
```

**Keep secrets out of the file** — supply the token (and API key) via environment
variables, which always override the file:

```bash
export TELERAFT_BOT_TOKEN="123456:ABC-DEF..."
export TELERAFT_HUMAN_IDS="11111111 22222222"   # space- or comma-separated
# export TELERAFT_RUNTIME=claude
# export ANTHROPIC_API_KEY="sk-ant-..."
```

Config precedence: **environment variable → `teleraft.toml` → built-in default**. The
full key list is in [§15](#15-configuration-reference).

---

## 8. Step 6 — Define your agents

Agents live in [`agents/`](../agents/): one `*.yaml` per agent plus a soul markdown file.
This is the five-element model from DESIGN.md §4 — Name, Soul, Goals, Memory (grows at
runtime), Heartbeat.

```yaml
# agents/cole.yaml
name: Cole
role: member                 # member | admin  (admin agents can manage structure)
soul: souls/cole.md          # job description, tone, hard rules
goals:
  owns: ["content pipeline", "# content"]   # the topic label must match a Telegram topic
  escalate_when: ["pricing", "legal", "medical claim"]   # forces a human plan gate
heartbeat:
  - cron: "0 9 * * 1-5"      # weekday 09:00 autonomous activation (scheduler, see §12)
    prompt: "Review the # content backlog; claim the highest-priority unclaimed task."
runtime:
  engine: mock               # mock (offline) | claude
  model: claude-fable-5
```

Rules that matter for Telegram wiring:

- The topic in `owns` (e.g. `# content`) must exist as a real forum topic and be listed
  in `topic_threads`.
- If a task's text contains any `escalate_when` term, the **Planner gate** fires and the
  run pauses for your approval *before* building (DESIGN.md §5.2).
- You need **at least two agents** so the Tester is always someone other than the
  Builder — "no agent grades its own work." The starter set ships three (Cole, Ray,
  Penn).

Roll agents out **one pillar per week** (DESIGN.md §4) so each feedback loop is tuned
before the next joins.

---

## 9. Step 7 — Run it

```bash
source .venv/bin/activate
python -m teleraft.main
```

On startup it runs a **preflight check** against the Bot API — token valid, group
reachable, group is a forum if you configured topics, broadcast channel postable. If
anything is wrong it prints the problems and exits rather than failing later on your
first message:

```
ERROR preflight: group chat unreachable: Telegram API getChat failed: … chat not found
  → chat_id='-1001234567890' is not a chat this bot can post to. Check: …
SystemExit: Telegram configuration is not usable — fix the problems above
```

When healthy you should see:

```
… teleraft.runner TeleRaft runner online as @MyTeam_TeleRaft_Bot; polling…
```

The process long-polls Telegram. Leave it running (on a VM: run under `systemd`, `tmux`,
or a process manager). SQLite at `db_path` is the source of truth — **stop and restart
any time; in-flight runs resume from their last checkpoint** (DESIGN.md §5.2).

Prefer a dry run with **no keys and no risk**? Skip Telegram entirely:

```bash
python -m teleraft.demo     # the full loop as a printed transcript
pytest                       # 19 tests
```

---

## 10. Step 8 — Verify end-to-end

In the Telegram group, in the **`# content`** topic, send:

```
@Cole write the launch post for the June webinar
```

(If you didn't create per-agent bots, `@Cole` still works — the runner also matches a
plain `@Name` to an agent's display name.)

Watch it flow:

1. A **task card** appears (`🟡 Todo`), Cole auto-claims (`🔵 In Progress`).
2. In-thread you see the **Plan**, a **build** line, an adversarial **Tester** reject,
   a rebuild, then a pass.
3. The card flips to **`🟣 In Review`** with **✅ Approve / ❌ Reject** buttons, and the
   broadcast channel gets *"👀 Review needed."*
4. Tap **✅ Approve**. The card flips to **`🟢 Done`**, an in-thread **"🧠 Learned: …"**
   line shows the memory writeback, and the channel logs the completion.

To see the escalation path, send `@Cole write the pricing page` — because *pricing* is
in Cole's `escalate_when`, the run pauses at a **plan gate** first.

---

## 11. Daily use: commands, buttons, gates

**Creating tasks**
- `@AgentName <what to do>` in a topic → creates a task and that agent claims it.
- `/task <what to do>` → creates an unclaimed task (anyone can Claim it).

**Buttons on cards**
- **Claim** — take ownership (one owner at a time).
- **✅ Approve / ❌ Reject** — human-only review gate. Approve ships to Done + Learn;
  Reject prompts you to *reply with a reason*, which is fed back into the agent's memory
  and drives a replan.
- **✅ Approve plan / ✏️ Adjust** — the plan gate, shown only when a task touches an
  escalation area.

**Statuses:** `🟡 Todo → 🔵 In Progress → 🟣 In Review → 🟢 Done` (or `⚪️ Closed`).

**The broadcast channel** is your at-a-glance feed: review-needed, done, escalations, and
proposed soul amendments.

**Drafts only:** agents never send anything externally. Everything they produce is a
draft that a human approves; you perform the actual outward send (DESIGN.md §1.4, §11).

---

## 12. Going live with real Claude

1. `pip install -e ".[telegram,anthropic]"`
2. `export ANTHROPIC_API_KEY="sk-ant-..."`
3. Set `runtime_engine = "claude"` (or `export TELERAFT_RUNTIME=claude`) and pick a
   `model` (default `claude-fable-5`).
4. Restart `python -m teleraft.main`.

Now the Planner/Builder/Tester/Learner roles are played by real Claude calls
([`teleraft/runtime/anthropic_runtime.py`](../teleraft/runtime/anthropic_runtime.py)),
composing each prompt from soul + goals + recalled memory + task context. The graph,
gates, budgets, and Telegram wiring are unchanged — only the engine behind each role
differs (DESIGN.md §4 runtime model). Per-run token/step/replan budgets in
[`models.py`](../teleraft/models.py) (`Budget`) bound cost; a breach escalates to you
in-thread.

**Heartbeats / autonomous activation** (the `heartbeat:` blocks) are defined per agent
but are driven by a scheduler you attach around the runner — e.g. a cron entry or the
repo's `schedule`/`loop` tooling that calls `engine.start(task_id, agent)` on a claimed
heartbeat task. The MVP runner handles human-driven and @mention-driven work; wiring the
scheduler is the one piece to add for always-on agents (DESIGN.md §4, §9 Phase 3).

---

## 13. Security & permissions checklist

- [ ] **Human-only gates.** `human_ids` contains only trusted humans. The gateway rejects
      any Approve/Reject from a user id not in this list (DESIGN.md §11) — verified by the
      test suite. A confused or compromised agent bot **cannot** approve anything.
- [ ] **Human owns the group.** Never transfer group ownership to a bot; admin-role
      agents get Telegram admin, member agents don't.
- [ ] **Privacy mode is off only for the workspace bot**, and the group is private.
- [ ] **Secrets via env vars**, never committed. `teleraft.toml` is git-ignored (only
      `teleraft.toml.example` is tracked); still prefer `TELERAFT_BOT_TOKEN` and
      `ANTHROPIC_API_KEY` in the environment over writing tokens to the file.
- [ ] **Drafts only.** No agent has a tool that sends externally; outward actions are
      always a human step after Approve.
- [ ] **Prompt-injection boundary.** Task/file/web content is treated as data, not
      instructions (the role prompts pin this); nothing external-facing can happen
      without the human review node.
- [ ] **Not end-to-end encrypted.** Bot chats are cloud chats. Don't route regulated
      secrets through messages; link to artifacts instead.

---

## 14. Polling vs webhooks

This implementation uses **long polling** (`getUpdates`) — no public URL, no TLS, works
behind NAT, ideal for a laptop or a single VM. It's what `python -m teleraft.main` runs.

For higher throughput or serverless hosting you can switch to **webhooks**: register an
HTTPS endpoint with `setWebhook` and feed each received update into
`LiveRunner.process_update(update)` — the same normalizer the poller uses. The gateway
and graph don't change. (Remember: you can't use `getUpdates` and a webhook at once.)

---

## 15. Configuration reference

`[telegram]` in `teleraft.toml` (env override in parentheses):

| Key | Meaning |
|---|---|
| `workspace_bot_token` (`TELERAFT_BOT_TOKEN`) | Workspace bot token from BotFather. **Required.** |
| `group_chat_id` (`TELERAFT_GROUP_CHAT_ID`) | Supergroup id, e.g. `-1001234567890`. **Required.** |
| `channel_id` (`TELERAFT_CHANNEL_ID`) | Broadcast channel `@handle` or `-100…` id. Optional. |
| `human_ids` (`TELERAFT_HUMAN_IDS`) | List of numeric user ids allowed to approve/reject. **Required.** |
| `topic_threads` | Table of `topic label → message_thread_id`. |
| `agent_usernames` | Table of `agent name → @bot_username` for mention routing. |
| `agent_bot_tokens` | Table of `agent name → token` (only for §17). |
| `poll_timeout` | Long-poll seconds (default 30). |

`[app]`:

| Key | Meaning |
|---|---|
| `agents_dir` (`TELERAFT_AGENTS_DIR`) | Where agent YAMLs live (default `agents`). |
| `db_path` (`TELERAFT_DB_PATH`) | SQLite file (default `teleraft_data/teleraft.db`). |
| `runtime_engine` (`TELERAFT_RUNTIME`) | `mock` or `claude`. |
| `model` (`TELERAFT_MODEL`) | Claude model id (default `claude-fable-5`). |

`[knowledge]` (§19):

| Key | Meaning |
|---|---|
| `root` (`TELERAFT_KNOWLEDGE_ROOT`) | Allow-listed root for `file` sources; agents cannot read outside it. |
| `sync_on_start` | Ingest declared sources at startup (default `true`). |

`[onboarding]` (§18):

| Key | Meaning |
|---|---|
| `host` (`TELERAFT_ONBOARDING_HOST`) | `hermes` (default), `openclaw`, or `mock`. |
| `gateway_url` (`TELERAFT_HOST_GATEWAY_URL`) | Host daemon URL (Hermes `:8787`, OpenClaw `:3000`). |
| `api_key` (`TELERAFT_HOST_API_KEY`) | Gateway API key, if the host requires one. |

Also read from the environment: `ANTHROPIC_API_KEY` (when `runtime_engine=claude`),
`GOOGLE_DRIVE_ACCESS_TOKEN` (read-only, for `gdrive` sources), and `TELERAFT_WEBHOOK`
(where a Hermes heartbeat job pokes TeleRaft to start a run).

---

## 16. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Bad Request: chat not found` on send | The chat id being sent is not one the bot can post to. The error now prints the offending `chat_id` and what to check: `group_chat_id` must be the numeric supergroup id (`-100…`, from `getUpdates` §6.2), and the bot must be in that group. Startup preflight catches this before polling. |
| **`@Name …` does nothing at all** | Two causes, and the bot now tells you which. If it replies *"I don't have an agent called @Name"*, your `agents_dir` is wrong — a quant desk needs `agents_dir = "agents/quant"`, not the default. If it stays completely silent, the bot never received the message: **privacy mode is still on** (§3.2) — disable it, then **remove and re-add** the bot. Run `/agents` to see who is actually loaded; the startup log lists them too. |
| Bot ignores messages in the group | Privacy mode still **on** (§3.2), or the bot isn't in the group. Disable privacy, then **remove and re-add** the bot so it takes effect. |
| Startup logs `agents loaded (0): NONE` | `agents_dir` points at a directory with no `*.yaml`. Every @mention will fail and every task stays unclaimed. |
| Startup logs `ignoring message from chat …` | Messages are arriving from a different chat than `group_chat_id`. Copy the id from that log line if it is your workspace group. |
| Tasks land in the wrong topic / all in *general* | `topic_threads` ids don't match reality. Re-read `message_thread_id` from `getUpdates` (§6.2). |
| `missing required config for a live run` on start | `workspace_bot_token` or `group_chat_id` unset. Check env vars and `teleraft.toml`. |
| `human_ids is empty` on start | Add your numeric id (`@userinfobot`) to `human_ids`. Without it nobody can approve. |
| Approve/Reject seems ignored | You're not in `human_ids`. The channel logs `⛔ blocked non-human gate decision`. |
| `no eligible tester` error | You have only one agent. Add a second so no agent grades its own work. |
| `LiveTelegramClient needs httpx` | `pip install -e ".[telegram]"`. |
| `AnthropicRuntime needs the 'anthropic' package` | `pip install -e ".[anthropic]"` and set `ANTHROPIC_API_KEY`. |
| `PDF support requires pypdf` | `pip install -e ".[knowledge]"`. |
| A source shows `error: … needs OCR` | The PDF is a scan with no text layer. OCR it first; v1 doesn't. |
| A source shows `escapes the allowed knowledge root` | The path is outside `knowledge.root`. Move the file in, or widen the root. |
| Drive source shows `token rejected (expired or revoked)` | Refresh `GOOGLE_DRIVE_ACCESS_TOKEN` (read-only scope), or re-share the folder with the service account. |
| Agents answer from priors, not your docs | Check `/kb list` — a source may be `error` or empty. Retrieval degrades to ungrounded rather than failing. |
| Onboarding: `agents cannot approve the onboarding plan` | Working as designed — approve with a human Telegram user ID in `human_ids`. |
| Heartbeats never fire | The host daemon isn't running, or (OpenClaw) has no external ticker. Check `hermes gateway` status and `TELERAFT_WEBHOOK`. |
| Nothing posts to the feed | `channel_id` blank, the handle is invalid (no hyphens/dots — §5), or the bot isn't a channel admin with Post rights. Preflight warns and disables the feed; the group keeps working. |
| `channel_id … is not a valid Telegram username` | The handle breaks Telegram's rules (5–32 chars, letters/digits/underscores, starts with a letter). Use the real handle from `t.me/<handle>`, or the numeric `-100…` id for a private channel. |
| Two instances behave oddly | Only run **one** poller per bot token; don't hit `getUpdates` by hand while it runs. |

---

## 17. Advanced: one bot per agent

The MVP posts everything through the workspace bot with name attribution. To have agents
post **as themselves** (own avatar, own @mention, own DMs — DESIGN.md §3.2):

1. Create a bot per agent in BotFather (§3.3) and disable each one's privacy mode.
2. Add them to the group; give admin-role agents Telegram admin rights.
3. Fill `agent_bot_tokens` with each `agent name → token`.
4. Build one `LiveTelegramClient` per token and route the engine's `notify()` for a
   given agent through that agent's client. The engine already passes the acting
   `agent` on every notify event, so this is an outbound routing change in
   [`gateway.py`](../teleraft/telegram/gateway.py) — the graph, tasks, and inbound runner
   are untouched.

Telegram limits to plan around (DESIGN.md §11): a group holds at most ~20 bots (caps team
size — start with ~5 pillar agents), and the Bot API has per-chat and global rate limits,
so batch card edits and throttle bursts. The workspace bot stays present for the board,
commands, and the broadcast feed regardless.

---

## 18. The onboarding agent (Hermes / OpenClaw)

Instead of hand-writing agent YAML, topics, and schedules, you can DM an **onboarding
agent** that interviews you and provisions the whole team (DESIGN.md §3.3).

### 18.1 Try it offline first

```bash
python -m teleraft.onboarding_demo
```

That runs the entire flow with no host, no keys, and no network: six questions → a
workspace plan → an Approve tap → topics, agents, souls, knowledge sources and
heartbeats created → a verification pass → a real task running through the loop.

### 18.2 Choose a host

The onboarding agent runs on a self-hosted gateway that already speaks Telegram, already
runs as a daemon, and brings a **cron scheduler** (which becomes your agent heartbeats)
and **channel connectors**:

| | **Hermes Agent** (default) | **OpenClaw** |
|---|---|---|
| Pick it when | You want autonomous heartbeats to be rock-solid | You want the team reachable from WhatsApp/Slack/Discord/iMessage too |
| Scheduler | First-class cron; ticks every 60 s, isolated session per job | Message plane only — heartbeats need an external ticker |
| Install | `hermes gateway install` (user or system service) | Node ≥ 22 LTS; config at `~/.openclaw/openclaw.json` |
| Default endpoint | `http://127.0.0.1:8787` | `http://127.0.0.1:3000` |

Install your chosen host per its own docs, confirm its daemon is running, then point
TeleRaft at it:

```toml
[onboarding]
host = "hermes"                          # or "openclaw"
gateway_url = "http://127.0.0.1:8787"
```

```bash
export TELERAFT_HOST_API_KEY="..."       # if your gateway requires one
```

### 18.3 Run the interview

Start the agent against your workspace (Python, so you can script it):

```bash
python -c "
from teleraft.app import App
from teleraft.config import load_config
from teleraft.onboarding import OnboardingAgent
from teleraft.onboarding.host import build_host

cfg = load_config()
app = App(db_path=cfg.db_path, agents_dir=cfg.agents_dir, human_ids=cfg.human_ids)
host = build_host(cfg.onboarding_host, base_url=cfg.host_gateway_url, api_key=cfg.host_api_key)
onb = OnboardingAgent(app, host, user_ref='<your-telegram-user-id>')
print('session:', onb.session_id)
onb.start()
"
```

It asks six questions in your Telegram DM:

1. What does your business do, and who are the customers?
2. Which pillars do you want agents for? *(leads · content · sales · delivery · finance)*
3. What should an agent never do without asking you first? *(→ `escalate_when`)*
4. What should they read? *(URLs, `drive://folders/<id>`, file paths → knowledge bases)*
5. When should they work on their own? *(→ heartbeat cron on the host)*
6. Which Telegram user IDs may approve work?

Then it posts a **`workspace.plan.yaml`** with **[Approve] [Adjust]**. Nothing is created
until a human approves — and an agent *cannot* approve it, including the onboarding agent
itself.

### 18.4 What approving does

Applying the plan creates topics, registers agents with generated souls and goals,
registers and ingests knowledge sources, schedules heartbeats on the host, adds your
human IDs to the approver allow-list, then **verifies** the result against the plan's
acceptance criteria (every topic exists, every agent has a soul, at least two agents so
nobody grades their own work, at least one human can approve).

Two things it deliberately does *not* do:

- **Create bots.** Only a human can, in BotFather (§3). It lists the bots to create and
  asks you to send each token in DM.
- **Guess at failures.** An unreachable URL or a revoked Drive token is reported as a
  manual step and as source health — never silently skipped.

Apply is **idempotent and diff-based**: re-running the same plan creates nothing and
reports what already existed, so you can resume an interrupted setup, or add a sixth
agent months later ("one pillar per week"). Destructive changes are never auto-applied.

If the process dies mid-interview, resume it — answers are persisted:

```python
OnboardingAgent.resume(app, "<session-id>", host)
```

### 18.5 Heartbeats

Each agent's `heartbeat:` entry becomes a cron job on the host. When it fires, the host
starts a TeleRaft graph run for that agent — the loop, gates, and budgets are identical
to human-triggered work, and output still lands in **In Review** as a draft. Set
`TELERAFT_WEBHOOK` so the Hermes job knows where to poke TeleRaft.

---

## 19. Knowledge bases: giving agents something to read

An agent grounded only in its soul will confidently invent facts about your business.
Each agent therefore owns a **knowledge base**: curated sources, retrieved at plan and
build time, with **citations the Tester checks** (DESIGN.md §4.1).

### 19.1 Supported sources

| Type | How to add | Formats | Notes |
|---|---|---|---|
| **Local file / folder** | `/kb add kb/cole/brand-voice.md` | `.md` `.pdf` `.txt` `.csv` | Confined to `knowledge.root` — agents cannot read outside it |
| **Web page / site** | `/kb add https://docs.acme.com/handbook` | HTML (+ linked docs) | Honours `robots.txt`, size-capped; `crawl: sitemap` to follow a sitemap |
| **Google Drive** | `/kb add drive://folders/1A2B3C` | Docs/Sheets/Slides exported, plus native files | **Read-only** scope; TeleRaft never edits your documents |
| **Telegram upload** | Post the file with `/kb add` | same four | Mirrored to the object store, then indexed |

Install the extras you need:

```bash
pip install -e ".[telegram,knowledge]"    # httpx for web/Drive, pypdf for PDFs
```

For Drive, supply a **read-only** credential:

```bash
export GOOGLE_DRIVE_ACCESS_TOKEN="ya29..."   # drive.readonly scope, or a service
                                             # account shared into the folder
```

### 19.2 Declaring sources in an agent

```yaml
# agents/cole.yaml
knowledge:
  - {type: file,   uri: kb/cole/brand-voice.md}
  - {type: file,   uri: kb/shared/personas.csv, scope: team}
  - {type: web,    uri: "https://docs.acme.com/handbook", crawl: sitemap, refresh: "0 3 * * *"}
  - {type: gdrive, uri: "drive://folders/1A2B3C", recursive: true, refresh: "0 4 * * *"}
```

`scope: team` shares a source with every agent; the default (`agent`) is what keeps
Finance's contracts out of the content agent's context. `refresh` is a cron run by the
host scheduler; sync is incremental by content hash, and a document deleted at the source
is tombstoned.

### 19.3 Managing it from Telegram

| Command | Effect |
|---|---|
| `/agents` (or `/help`) | Who is on the team, what each owns, and what escalates — **check this first if an @mention seems to do nothing** |
| `/kb add <uri> [--team]` | Register + ingest. Scoped to the agent owning the current topic. |
| `/kb list` | Every source with status, doc and chunk counts, and any error |
| `/kb sync [source_id]` | Re-sync one source, or all of the topic agent's sources |
| `/kb remove <source_id>` | Drop the source and its chunks |

### 19.4 What it changes in the loop

- **Intake/Plan** — passages are retrieved and shown in-thread
  (`📚 Cole retrieved 3 passage(s): brand-voice.md # Brand > ## Launches …`), so the
  Planner writes criteria against real constraints.
- **Build** — the draft carries `citations[]`, rendered on the review card as
  `📚 Sources: brand-voice.md # Brand > ## Launches`.
- **Test** — the Tester sees the same passages and **rejects an uncited claim** or a
  citation that doesn't support its sentence. Citations are stored per run, so for any
  approved deliverable you can answer "which passage justified this?"

Locators are format-aware, which is what makes a citation actionable: `p.12` for PDFs,
`# Brand > ## Tone` for Markdown, `row 4` for CSV.

### 19.5 Failure modes (all visible, never silent)

Unreachable URL, revoked Drive token, unsupported file type, encrypted or **scanned** PDF
(needs OCR — unsupported in v1), oversized file, robots-disallowed page: each marks the
source `error` in `/kb list` with the reason. A run whose knowledge is unavailable
degrades to ungrounded work rather than failing, and the unhealthy source is logged at
startup and surfaced in the feed.

> **Security.** Fetched pages and shared documents are the classic prompt-injection
> vector. Retrieved passages are inserted as clearly delimited **data, not instructions**;
> no retrieval grants an agent new capability; and nothing external-facing happens
> without the human review gate (§13).

---

*Next:* skim [DESIGN.md](../DESIGN.md) §5 to understand what happens between Claim and
Approve, and [README.md](../README.md) to run the offline demo that mirrors exactly what
you'll see in Telegram.
