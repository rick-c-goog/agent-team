"""Live Telegram adapter tests — no network (httpx.MockTransport) and pure normalizers."""

import json

import pytest

httpx = pytest.importorskip("httpx")

from teleraft.app import App
from teleraft.config import Config
from teleraft.telegram.client import Button
from teleraft.telegram.live_client import LiveTelegramClient
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


def test_normalize_callback():
    app, runner = _runner()
    cb = runner.normalize_callback({"data": "approve|run1|review", "from": {"id": 11111111}})
    assert cb.data == "approve|run1|review" and cb.user_id == "11111111"
    app.close()
