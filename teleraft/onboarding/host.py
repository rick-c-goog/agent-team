"""OnboardingHost adapters — Hermes Agent and OpenClaw (DESIGN.md §3.3.1).

The host provides two things TeleRaft would otherwise have to build:

  * a **cron scheduler** — which becomes the heartbeat substrate (§4)
  * **channel connectors** — which carry the onboarding interview (and future
    non-Telegram reach)

The adapter surface is deliberately tiny, so swapping hosts changes nothing about the
graph, the gates, or the budgets.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol


@dataclass
class ScheduledJob:
    id: str
    cron: str
    prompt: str
    agent: str


class OnboardingHost(Protocol):
    """What the onboarding agent and the heartbeat scheduler need from a host."""

    channel: str

    def send(self, user_ref: str, message: str, buttons: Optional[list[str]] = None) -> None:
        ...

    def schedule(self, cron: str, prompt: str, agent: str) -> str:
        ...

    def cancel(self, job_id: str) -> None:
        ...

    def jobs(self) -> list[ScheduledJob]:
        ...


# --------------------------------------------------------------------------- #
# Mock host — offline interviews and tests
# --------------------------------------------------------------------------- #
class MockHost:
    """In-memory host: records what was asked and what got scheduled."""

    channel = "mock"

    def __init__(self):
        self.sent: list[tuple[str, str, list[str]]] = []
        self._jobs: dict[str, ScheduledJob] = {}
        self._seq = 0

    def send(self, user_ref: str, message: str, buttons: Optional[list[str]] = None) -> None:
        self.sent.append((user_ref, message, list(buttons or [])))

    def schedule(self, cron: str, prompt: str, agent: str) -> str:
        self._seq += 1
        job_id = f"job_{self._seq}"
        self._jobs[job_id] = ScheduledJob(job_id, cron, prompt, agent)
        return job_id

    def cancel(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    def jobs(self) -> list[ScheduledJob]:
        return list(self._jobs.values())


# --------------------------------------------------------------------------- #
# Hermes Agent (Nous Research) — default: strongest scheduler
# --------------------------------------------------------------------------- #
class HermesHost:
    """Hermes Agent adapter.

    Hermes runs a gateway daemon whose scheduler ticks every 60 s and executes due jobs
    in isolated agent sessions — exactly the semantics TeleRaft heartbeats need. Each
    scheduled job pokes the TeleRaft server, which starts a normal graph run, so the
    loop, gates, and budgets are unchanged.

    Talks to a local Hermes gateway over HTTP; `base_url` defaults to the daemon's
    loopback address.
    """

    channel = "telegram"

    def __init__(self, base_url: str = "", api_key: str = "", http=None,
                 teleraft_webhook: str = ""):
        try:
            import httpx
        except ImportError as e:  # pragma: no cover - optional dependency
            raise RuntimeError("HermesHost needs httpx: pip install teleraft[telegram]") from e
        self.base_url = (base_url or os.environ.get("HERMES_GATEWAY_URL",
                                                    "http://127.0.0.1:8787")).rstrip("/")
        self.api_key = api_key or os.environ.get("HERMES_API_KEY", "")
        self.teleraft_webhook = teleraft_webhook or os.environ.get(
            "TELERAFT_WEBHOOK", "http://127.0.0.1:8080/runs"
        )
        self._http = http or httpx.Client(timeout=30)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def _post(self, path: str, payload: dict) -> dict:
        resp = self._http.post(f"{self.base_url}{path}", json=payload, headers=self._headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"Hermes {path} failed: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else {}

    def send(self, user_ref: str, message: str, buttons: Optional[list[str]] = None) -> None:
        self._post("/messages", {
            "channel": "telegram",
            "to": user_ref,
            "text": message,
            "buttons": buttons or [],
        })

    def schedule(self, cron: str, prompt: str, agent: str) -> str:
        """Register a cron job that starts a TeleRaft graph run for `agent`."""
        result = self._post("/cron/jobs", {
            "schedule": cron,
            "name": f"teleraft-heartbeat-{agent}",
            "prompt": prompt,
            # A script-only job: no model call on the Hermes side — it just wakes
            # TeleRaft, which runs the full Planner/Orchestrator/Builder/Tester loop.
            "script": (
                f"curl -sS -X POST {self.teleraft_webhook} "
                f"-H 'Content-Type: application/json' "
                f"-d {json.dumps(json.dumps({'agent': agent, 'prompt': prompt}))}"
            ),
        })
        return str(result.get("id") or result.get("job_id") or "")

    def cancel(self, job_id: str) -> None:
        resp = self._http.delete(f"{self.base_url}/cron/jobs/{job_id}", headers=self._headers())
        if resp.status_code >= 400 and resp.status_code != 404:
            raise RuntimeError(f"Hermes cancel failed: HTTP {resp.status_code}")

    def jobs(self) -> list[ScheduledJob]:
        resp = self._http.get(f"{self.base_url}/cron/jobs", headers=self._headers())
        if resp.status_code >= 400:
            raise RuntimeError(f"Hermes list failed: HTTP {resp.status_code}")
        out = []
        for j in resp.json().get("jobs", []):
            out.append(ScheduledJob(
                id=str(j.get("id", "")),
                cron=j.get("schedule", ""),
                prompt=j.get("prompt", ""),
                agent=str(j.get("name", "")).removeprefix("teleraft-heartbeat-"),
            ))
        return out


# --------------------------------------------------------------------------- #
# OpenClaw — pick when the team needs multi-channel reach
# --------------------------------------------------------------------------- #
class OpenClawHost:
    """OpenClaw adapter (formerly Clawdbot/Moltbot).

    OpenClaw's gateway is "the single source of truth for sessions, routing, and channel
    connections" across Telegram, WhatsApp, Slack, Discord, Signal, iMessage and more —
    so the same onboarding interview reaches a user wherever they already chat.

    OpenClaw has no cron of Hermes' depth, so heartbeats registered here are kept in the
    gateway's config and fired by an external ticker (`external_scheduler`) — which is
    why Hermes is the default host in §3.3.1.
    """

    def __init__(self, base_url: str = "", api_key: str = "", channel: str = "telegram",
                 http=None, external_scheduler=None):
        try:
            import httpx
        except ImportError as e:  # pragma: no cover - optional dependency
            raise RuntimeError("OpenClawHost needs httpx: pip install teleraft[telegram]") from e
        self.base_url = (base_url or os.environ.get("OPENCLAW_GATEWAY_URL",
                                                    "http://127.0.0.1:3000")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENCLAW_API_KEY", "")
        self.channel = channel
        self._http = http or httpx.Client(timeout=30)
        self._external = external_scheduler
        self._jobs: dict[str, ScheduledJob] = {}
        self._seq = 0

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def send(self, user_ref: str, message: str, buttons: Optional[list[str]] = None) -> None:
        resp = self._http.post(
            f"{self.base_url}/api/messages",
            json={"channel": self.channel, "to": user_ref, "text": message,
                  "buttons": buttons or []},
            headers=self._headers(),
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"OpenClaw send failed: HTTP {resp.status_code}")

    def schedule(self, cron: str, prompt: str, agent: str) -> str:
        self._seq += 1
        job_id = f"oc_{self._seq}"
        self._jobs[job_id] = ScheduledJob(job_id, cron, prompt, agent)
        if self._external is not None:
            # Delegate the actual ticking; OpenClaw itself is the message plane.
            self._external.schedule(cron, prompt, agent)
        return job_id

    def cancel(self, job_id: str) -> None:
        self._jobs.pop(job_id, None)

    def jobs(self) -> list[ScheduledJob]:
        return list(self._jobs.values())


HOSTS: dict[str, Callable[..., OnboardingHost]] = {
    "mock": MockHost,
    "hermes": HermesHost,
    "openclaw": OpenClawHost,
}


def build_host(name: str, **kwargs) -> OnboardingHost:
    if name not in HOSTS:
        raise ValueError(f"unknown onboarding host {name!r}; choose from {sorted(HOSTS)}")
    return HOSTS[name](**kwargs)
