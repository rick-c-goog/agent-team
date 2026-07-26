"""Outbound message formatting.

Live regression: Telegram's legacy Markdown rejected ordinary agent output —
`[Quinn] Research note …` reads as an unterminated link, `hyp_a3b4` as italics — so the
review card failed with "can't parse entities", and then the failure reporter crashed
reporting that failure. Everything now goes out as HTML with dynamic content escaped.
"""

from html.parser import HTMLParser

import pytest

from teleraft.app import App
from teleraft.models import Artifact, Citation
from teleraft.runtime.mock import MockRuntime
from teleraft.telegram.gateway import Update, bold, esc, mono

httpx = pytest.importorskip("httpx")

from teleraft.telegram.live_client import LiveTelegramClient   # noqa: E402

# Tags Telegram's HTML parse mode accepts.
ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "s", "a", "code", "pre",
                "blockquote", "tg-spoiler"}

# The exact shapes that broke the live run, plus HTML injection attempts.
HOSTILE = ("[Quinn] Research note — momentum(lookback=60) on SPY "
           "<b>fake bold</b> hyp_a3b4 *unclosed 100% & <script>alert(1)</script> _x_ `y`")


class _Validator(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.problems.append(f"unsupported <{tag}>")
        else:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.problems.append(f"unbalanced </{tag}>")

    def verdict(self) -> list[str]:
        return self.problems + ([f"unclosed {self.stack}"] if self.stack else [])


def assert_valid_html(text: str) -> None:
    v = _Validator()
    v.feed(text)
    assert not v.verdict(), f"{v.verdict()} in: {text[:200]}"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_escaping_neutralises_markup_but_keeps_text():
    assert esc("a < b & c > d") == "a &lt; b &amp; c &gt; d"
    assert esc("[Quinn] hyp_a3b4 *x* `y`") == "[Quinn] hyp_a3b4 *x* `y`"
    assert bold("a<b") == "<b>a&lt;b</b>"
    assert mono("x&y") == "<code>x&amp;y</code>"


# --------------------------------------------------------------------------- #
# Every outbound message must be valid, escaped HTML
# --------------------------------------------------------------------------- #
class _HostileRuntime(MockRuntime):
    def build(self, req):
        return Artifact(step=req.step, content=HOSTILE, files=["research/a_b[1].md"],
                        notes="n",
                        citations=[Citation("s", "doc_[1].md", "p_2", "q<>&")]), 0


def test_every_message_of_a_full_run_is_valid_html():
    app = App(human_ids={"1"})
    app.engine.runtime_for = lambda agent: _HostileRuntime()
    app.gateway.handle_message(
        Update(text="@Cole write it", user_id="1", user_handle="rick",
               topic="# content", as_task=True, mentions=["Cole"])
    )
    assert app.client.messages
    for msg in app.client.messages.values():
        assert_valid_html(msg.text)
    app.close()


def test_agent_output_cannot_forge_markup():
    """An agent writing '<b>fake bold</b>' must not produce real bold — otherwise
    agent text could imitate the system's own UI."""
    app = App(human_ids={"1"})
    app.engine.runtime_for = lambda agent: _HostileRuntime()
    app.gateway.handle_message(
        Update(text="@Cole write it", user_id="1", user_handle="rick",
               topic="# content", as_task=True, mentions=["Cole"])
    )
    review = next(m.text for m in app.client.messages.values()
                  if "In Review" in m.text and "Draft" in m.text)
    assert "&lt;b&gt;fake bold&lt;/b&gt;" in review      # neutralised
    assert "<b>In Review</b>" in review                  # our own markup survives
    assert "&lt;script&gt;" in review
    assert "[Quinn]" in review and "hyp_a3b4" in review   # content preserved verbatim
    app.close()


def test_failure_report_is_valid_html_even_with_a_hostile_error():
    """The failure reporter itself crashed in production; its input is an exception
    message, which is exactly where odd characters live."""
    class Exploding(MockRuntime):
        def plan(self, req):
            raise RuntimeError("401 invalid x-api-key <b>bad</b> [key] a_b & c")

    app = App(human_ids={"1"})
    app.engine.runtime_for = lambda agent: Exploding()
    app.gateway.handle_message(
        Update(text="@Cole write it", user_id="1", user_handle="rick",
               topic="# content", as_task=True, mentions=["Cole"])
    )
    failure = next(m.text for m in app.client.messages.values() if "Run failed" in m.text)
    assert_valid_html(failure)
    assert "&lt;b&gt;bad&lt;/b&gt;" in failure
    app.close()


@pytest.mark.parametrize("command", ["/agents", "/kb list", "/hyp list", "/kb add x&y<z"])
def test_command_replies_are_valid_html(command):
    app = App(human_ids={"1"}, agents_dir="agents/quant", sync_knowledge=False)
    app.gateway.handle_message(
        Update(text=command, user_id="1", user_handle="rick", topic="# research")
    )
    for msg in app.client.messages.values():
        assert_valid_html(msg.text)
    app.close()


def test_unknown_agent_reply_is_valid_html():
    app = App(human_ids={"1"})
    app.gateway.handle_message(
        Update(text="@No_Such_Agent <do> this & that", user_id="1", user_handle="rick",
               topic="# content", mentions=["No_Such_Agent"])
    )
    for msg in app.client.messages.values():
        assert_valid_html(msg.text)
    app.close()


# --------------------------------------------------------------------------- #
# Client: HTML mode + plain-text fallback
# --------------------------------------------------------------------------- #
def test_client_sends_html_parse_mode():
    calls = []

    def handler(request):
        import json
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = LiveTelegramClient(token="t", group_chat_id="-100",
                                http=httpx.Client(transport=httpx.MockTransport(handler)))
    client.send_message("-100", "<b>hi</b>")
    assert calls[-1]["parse_mode"] == "HTML"


def test_unparseable_text_is_resent_as_plain_text_rather_than_lost():
    """Defence in depth: even if some future message is malformed, the content must
    still reach the group."""
    attempts = []

    def handler(request):
        import json
        body = json.loads(request.content)
        attempts.append(body)
        if "parse_mode" in body:
            return httpx.Response(200, json={
                "ok": False, "error_code": 400,
                "description": "Bad Request: can't parse entities: "
                               "Can't find end of the entity starting at byte offset 293",
            })
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 9}})

    client = LiveTelegramClient(token="t", group_chat_id="-100",
                                http=httpx.Client(transport=httpx.MockTransport(handler)))
    mid = client.send_message("-100", "<b>unclosed")

    assert mid == "9", "the message must still be delivered"
    assert len(attempts) == 2
    assert "parse_mode" in attempts[0] and "parse_mode" not in attempts[1]
    assert attempts[1]["text"] == "<b>unclosed"     # content unchanged
