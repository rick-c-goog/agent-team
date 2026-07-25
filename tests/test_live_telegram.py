"""Live Telegram adapter tests — no network (httpx.MockTransport) and pure normalizers."""

import json

import pytest

httpx = pytest.importorskip("httpx")

from teleraft.app import App
from teleraft.config import Config
from teleraft.telegram.client import Button
from teleraft.telegram.live_client import LiveTelegramClient
from teleraft.telegram.gateway import Update
from teleraft.telegram.runner import LiveRunner


def _mock_client(recorder):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = request.url.path.rsplit("/", 1)[-1]
        recorder.append((method, body))
        if method == "sendMessage":
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
        return httpx.Response(200, json={"ok": True, "result": {}})
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return LiveTelegramClient(
        token="t", group_chat_id="-100", channel_id="@feed",
        topic_threads={"# content": "2"}, http=http,
    )


def test_live_client_builds_correct_sendmessage_payload():
    calls = []
    client = _mock_client(calls)
    mid = client.send_message(
        "-100", "hello", buttons=[Button("✅ Approve", "approve|run1|review")],
        thread="#abc · # content",
    )
    assert mid == "42"
    method, body = calls[-1]
    assert method == "sendMessage"
    assert body["chat_id"] == "-100"
    assert body["message_thread_id"] == 2                     # topic → thread id
    assert body["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "approve|run1|review"


def test_live_client_channel_post():
    calls = []
    client = _mock_client(calls)
    client.send_channel("digest line")
    method, body = calls[-1]
    assert method == "sendMessage" and body["chat_id"] == "@feed"


def _runner():
    app = App(human_ids={"11111111"})
    cfg = Config(
        group_chat_id="-100",
        human_ids={"11111111"},
        topic_threads={"# content": "2"},
        agent_usernames={"Cole": "@Cole_TR_Bot"},
    )
    return app, LiveRunner(client=None, gateway=app.gateway, config=cfg)


def test_normalize_message_detects_mention_and_topic():
    app, runner = _runner()
    msg = {
        "chat": {"id": "-100"},
        "from": {"id": 11111111, "username": "rick", "is_bot": False},
        "message_thread_id": 2,
        "text": "@Cole_TR_Bot write the launch post",
        "entities": [{"type": "mention", "offset": 0, "length": 12}],
    }
    upd = runner.normalize_message(msg)
    assert upd is not None
    assert upd.mentions == ["Cole"]
    assert upd.topic == "# content"
    assert upd.as_task is True
    app.close()


def test_normalize_message_ignores_bots_and_foreign_chats():
    app, runner = _runner()
    assert runner.normalize_message(
        {"chat": {"id": "-100"}, "from": {"id": 1, "is_bot": True}, "text": "hi"}
    ) is None
    assert runner.normalize_message(
        {"chat": {"id": "-999"}, "from": {"id": 1, "is_bot": False}, "text": "hi"}
    ) is None
    app.close()


def test_gateway_posts_to_the_configured_chat_not_a_placeholder():
    """Regression: the gateway used to default group_chat_id to the sentinel
    'workspace', which Telegram rejects with 'chat not found'. The mock client ignores
    chat_id, so only a live-shaped client catches it."""
    calls = []
    client = _mock_client(calls)
    app = App(human_ids={"11111111"}, client=client, group_chat_id="-1001234567890")

    app.gateway.handle_message(
        Update(text="@Cole write the launch post", user_id="11111111",
               user_handle="rick", topic="# content", as_task=True, mentions=["Cole"])
    )

    sends = [body for method, body in calls if method == "sendMessage"]
    assert sends, "the gateway should have posted a task card"
    # Every send targets either the group or the broadcast channel — never a placeholder.
    assert {b["chat_id"] for b in sends} <= {"-1001234567890", "@feed"}
    assert any(b["chat_id"] == "-1001234567890" for b in sends), "no post reached the group"
    app.close()


def test_unset_chat_id_falls_through_to_the_client_default():
    """An App with no group_chat_id must not send a placeholder — the live client's
    own configured chat is used instead."""
    calls = []
    client = _mock_client(calls)          # configured with chat -100
    app = App(human_ids={"11111111"}, client=client)

    app.gateway.handle_message(
        Update(text="@Cole draft something", user_id="11111111", user_handle="rick",
               topic="# content", as_task=True, mentions=["Cole"])
    )
    sends = [b for m, b in calls if m == "sendMessage"]
    assert {b["chat_id"] for b in sends} <= {"-100", "@feed"}   # client's own chat
    assert any(b["chat_id"] == "-100" for b in sends)
    app.close()


def test_chat_not_found_error_names_the_misconfiguration():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": False, "error_code": 400,
            "description": "Bad Request: chat not found",
        })

    client = LiveTelegramClient(token="t", group_chat_id="workspace",
                                http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError) as e:
        client.send_message("workspace", "hi")
    message = str(e.value)
    # A structurally impossible ref gets the precise reason rather than generic advice.
    assert "workspace" in message
    assert "neither a numeric chat id" in message and "never resolve" in message


def _preflight_client(chat_ok=True, forum=True, channel_id="", chat_type="supergroup"):
    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": "bot"}})
        if not chat_ok:
            return httpx.Response(200, json={"ok": False, "error_code": 400,
                                             "description": "Bad Request: chat not found"})
        return httpx.Response(200, json={"ok": True, "result": {"type": chat_type,
                                                                "is_forum": forum}})
    return LiveTelegramClient(token="t", group_chat_id="-100", channel_id=channel_id,
                              topic_threads={"# content": "2"},
                              http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_preflight_treats_an_unreachable_group_as_fatal():
    report = _preflight_client(chat_ok=False).preflight()
    assert not report.ok
    assert any("group chat unreachable" in p for p in report.fatal)


def test_preflight_passes_on_a_healthy_forum_group():
    report = _preflight_client().preflight()
    assert report.ok and report.warnings == []


def test_preflight_warns_when_topics_are_configured_without_forum_mode():
    report = _preflight_client(forum=False).preflight()
    assert report.ok, "missing forum mode degrades threading, it does not block startup"
    assert any("Topics enabled" in w for w in report.warnings)


@pytest.mark.parametrize("handle,reason", [
    ("@ai-quant-research-ch", "hyphen is not a legal username character"),
    ("@my.channel", "dot is not legal either"),
    ("@abc", "too short (min 5)"),
    ("workspace", "not numeric and not an @handle"),
])
def test_structurally_invalid_chat_refs_are_caught_without_calling_telegram(handle, reason):
    from teleraft.telegram.live_client import chat_ref_problem
    problem = chat_ref_problem(handle)
    assert problem, reason
    assert handle in problem


@pytest.mark.parametrize("ref", ["-1001234567890", "@good_channel", "@Channel123"])
def test_plausible_chat_refs_pass_structural_validation(ref):
    from teleraft.telegram.live_client import chat_ref_problem
    assert chat_ref_problem(ref) is None


def test_bad_channel_handle_warns_and_disables_the_feed_but_starts():
    """Regression: an invalid channel handle used to abort startup entirely."""
    client = _preflight_client(channel_id="@ai-quant-research-ch")
    report = client.preflight()

    assert report.ok, "an optional feed must not block startup"
    assert any("channel_id" in w and "hyphen" not in w.lower() or "-" in w
               for w in report.warnings)
    assert any("valid Telegram username" in w for w in report.warnings)
    assert client.channel_id == "", "the unusable feed should be disabled"

    # And posting to the feed is now a silent no-op rather than an exception.
    assert client.send_channel("digest") == ""


def test_channel_failure_at_runtime_degrades_instead_of_raising():
    state = {"fail": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(200, json={"ok": False, "error_code": 400,
                                             "description": "Bad Request: chat not found"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = LiveTelegramClient(token="t", group_chat_id="-100", channel_id="@feed",
                                http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert client.send_channel("first") == "1"
    state["fail"] = True
    assert client.send_channel("second") == ""      # degraded, not raised
    assert client.channel_id == ""


def test_channel_error_hint_names_channel_id_not_group_chat_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error_code": 400,
                                         "description": "Bad Request: chat not found"})

    client = LiveTelegramClient(token="t", group_chat_id="-100", channel_id="@missing_chan",
                                http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(RuntimeError) as e:
        client._call("getChat", _hint_key="channel_id", chat_id="@missing_chan")
    assert "channel_id" in str(e.value)
    assert "administrator of the channel" in str(e.value)


def test_mentioning_an_unknown_agent_answers_instead_of_going_silent():
    """Regression: '@Quinn ...' against a workspace without Quinn produced total
    silence — no reply, no log, no task. The user cannot tell it from a dead bot."""
    app = App(human_ids={"11111111"})           # default agents: Cole, Penn, Ray
    assert "Quinn" not in app.registry.names()

    app.gateway.handle_message(
        Update(text="@Quinn is there a momentum edge on SPY?", user_id="11111111",
               user_handle="rick", topic="# research", mentions=["Quinn"])
    )
    reply = " ".join(app.client.transcript)
    assert "don't have an agent called" in reply and "@Quinn" in reply
    assert "@Cole" in reply and "@Penn" in reply     # tells you who *does* exist
    app.close()


def test_empty_workspace_says_agents_dir_is_the_problem(tmp_path):
    empty = tmp_path / "agents"
    empty.mkdir()
    app = App(human_ids={"11111111"}, agents_dir=str(empty))

    app.gateway.handle_message(
        Update(text="@Quinn do some research", user_id="11111111", user_handle="rick",
               topic="# research", mentions=["Quinn"])
    )
    reply = " ".join(app.client.transcript)
    assert "No agents are loaded" in reply and "agents_dir" in reply
    app.close()


def test_known_agent_still_starts_a_task_and_does_not_get_a_complaint():
    app = App(human_ids={"11111111"})
    app.gateway.handle_message(
        Update(text="@Cole write the launch post", user_id="11111111", user_handle="rick",
               topic="# content", as_task=True, mentions=["Cole"])
    )
    reply = " ".join(app.client.transcript)
    assert "don't have an agent" not in reply
    assert "In Review" in reply                  # the loop actually ran
    app.close()


def test_ordinary_chatter_is_not_answered():
    app = App(human_ids={"11111111"})
    for text in ("good morning everyone", "we should look at SPY", "thanks @rick"):
        app.gateway.handle_message(
            Update(text=text, user_id="11111111", user_handle="rick", topic="# content")
        )
    assert not app.client.transcript, "the bot must not chime in on normal conversation"
    app.close()


def test_bot_does_not_complain_when_addressed_itself():
    app = App(human_ids={"11111111"})
    app.gateway.bot_username = "MyTeam_TeleRaft_Bot"
    app.gateway.handle_message(
        Update(text="@MyTeam_TeleRaft_Bot status?", user_id="11111111",
               user_handle="rick", topic="# content")
    )
    assert not app.client.transcript
    app.close()


def test_agents_command_lists_the_team():
    app = App(human_ids={"11111111"})
    names = app.gateway.handle_message(
        Update(text="/agents", user_id="11111111", user_handle="rick", topic="# content")
    )
    assert set(names) == {"Cole", "Penn", "Ray"}
    listing = " ".join(app.client.transcript)
    assert "@Cole" in listing and "owns:" in listing and "/task" in listing
    app.close()


def test_runner_reads_agents_live_rather_than_caching_them():
    """Agents registered after startup must be mentionable without a restart."""
    from teleraft.agents.registry import AgentConfig

    app, runner = _runner()
    assert "Quinn" not in runner.agent_names
    app.registry.register(AgentConfig(name="Quinn", soul_md="factor researcher", goals={}))
    assert "Quinn" in runner.agent_names
    app.close()


def test_message_from_the_wrong_chat_is_logged_not_swallowed(caplog):
    app, runner = _runner()
    with caplog.at_level("WARNING"):
        assert runner.normalize_message({
            "chat": {"id": "-999"},
            "from": {"id": 1, "username": "rick", "is_bot": False},
            "text": "hello",
        }) is None
    assert "group_chat_id" in caplog.text
    app.close()


def test_normalize_callback():
    app, runner = _runner()
    cb = runner.normalize_callback({"data": "approve|run1|review", "from": {"id": 11111111}})
    assert cb.data == "approve|run1|review" and cb.user_id == "11111111"
    app.close()
