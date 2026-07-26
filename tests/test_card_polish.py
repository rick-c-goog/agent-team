"""Live-observed presentation defects on the review card and task card."""

import json

import pytest

from teleraft.app import App
from teleraft.telegram.gateway import Callback, Update

httpx = pytest.importorskip("httpx")

from teleraft.telegram.live_client import LiveTelegramClient   # noqa: E402

HUMAN = "11111111"


def _run(app, text="@Quinn is there a momentum edge on SPY"):
    return app.gateway.handle_message(
        Update(text=text, user_id=HUMAN, user_handle="rick", topic="# research",
               as_task=True, mentions=["Quinn"])
    )


def _review_card(app) -> str:
    return next(m.text for m in app.client.messages.values()
                if "In Review" in m.text and "Draft" in m.text)


# --------------------------------------------------------------------------- #
# 1. Provenance: a Sharpe ratio must say what data produced it
# --------------------------------------------------------------------------- #
def test_review_card_states_that_synthetic_data_is_not_real_market_data():
    app = App(human_ids={HUMAN}, agents_dir="agents/quant")
    _run(app)
    card = _review_card(app)
    assert "Data:" in card
    assert "synthetic" in card and "NOT real market data" in card
    app.close()


def test_provenance_names_a_real_loader_without_the_warning():
    from teleraft.quant.data import CsvLoader
    from teleraft.quant.hypothesis import HypothesisRegistry
    from teleraft.runtime.quant import QuantRuntime
    from teleraft.storage import Storage

    runtime = QuantRuntime(HypothesisRegistry(Storage(":memory:")), loader=CsvLoader("data"))
    assert runtime.data_provenance() == "csv"
    assert "NOT real" not in runtime.data_provenance()


# --------------------------------------------------------------------------- #
# 2. Progress lines truncated mid-word ("In-sample: po")
# --------------------------------------------------------------------------- #
def test_progress_lines_are_cut_on_a_line_boundary_not_mid_word():
    app = App(human_ids={HUMAN}, agents_dir="agents/quant")
    _run(app)
    built = [m.text for m in app.client.messages.values() if "built step" in m.text]
    assert built
    for line in built:
        # One logical line, and never a dangling fragment of the next one.
        body = line.split("built step", 1)[1]
        assert "\n" not in body.strip(), body
        assert not body.rstrip().endswith(("In-sample: po", "(2020-01"))
    app.close()


# --------------------------------------------------------------------------- #
# 3. File paths auto-linked by Telegram (".md" looks like a domain)
# --------------------------------------------------------------------------- #
def test_file_paths_and_citations_are_wrapped_so_telegram_cannot_autolink_them():
    app = App(human_ids={HUMAN}, agents_dir="agents/quant")
    _run(app)
    card = _review_card(app)
    assert "<code>research/" in card, "file path must be in <code> to defeat autolinking"
    assert "📚 Sources: <code>" in card
    app.close()


# --------------------------------------------------------------------------- #
# 4. Buttons must retire once a gate is decided
# --------------------------------------------------------------------------- #
def test_gate_buttons_are_removed_after_a_decision():
    app = App(human_ids={HUMAN}, agents_dir="agents/quant")
    result = _run(app)

    # The gate card is the one offering the decision — not the task card, whose
    # status text also reads "In Review".
    gate_mid = next(mid for mid in app.client.messages
                    if app.client.has_button(mid, "Approve"))

    app.gateway.handle_callback(Callback(data=f"approve|{result.run_id}|review",
                                         user_id=HUMAN))
    assert not app.client.messages[gate_mid].buttons, "settled gate still offers buttons"
    assert "✅ Approved by" in app.client.messages[gate_mid].text
    app.close()


def test_a_second_tap_on_a_settled_gate_is_handled_quietly():
    """Previously raised ValueError out of the runner as an unhandled error."""
    app = App(human_ids={HUMAN}, agents_dir="agents/quant")
    result = _run(app)
    app.gateway.handle_callback(Callback(data=f"approve|{result.run_id}|review",
                                         user_id=HUMAN))
    # Second tap — must not raise.
    assert app.gateway.handle_callback(
        Callback(data=f"approve|{result.run_id}|review", user_id=HUMAN)) is None
    app.close()


def test_rejecting_marks_the_card_rejected():
    app = App(human_ids={HUMAN}, agents_dir="agents/quant")
    result = _run(app)
    gate_mid = next(mid for mid in app.client.messages
                    if app.client.has_button(mid, "Approve"))
    app.gateway.handle_callback(Callback(data=f"reject|{result.run_id}|review",
                                         user_id=HUMAN, reason="want a longer sample"))
    assert "❌ Rejected by" in app.client.messages[gate_mid].text
    app.close()


# --------------------------------------------------------------------------- #
# 5. No-op edits caused a 400 on every status refresh
# --------------------------------------------------------------------------- #
def _counting_client():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method = request.url.path.rsplit("/", 1)[-1]
        calls.append((method, body))
        if method == "editMessageText":
            # Real Telegram rejects an edit that changes nothing.
            prior = [b for m, b in calls[:-1]
                     if m == "editMessageText" and b.get("message_id") == body.get("message_id")]
            if prior and prior[-1].get("text") == body.get("text"):
                return httpx.Response(200, json={
                    "ok": False, "error_code": 400,
                    "description": "Bad Request: message is not modified"})
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(calls)}})

    client = LiveTelegramClient(token="t", group_chat_id="-100",
                                http=httpx.Client(transport=httpx.MockTransport(handler)))
    return client, calls


def test_identical_edits_are_not_sent_at_all():
    client, calls = _counting_client()
    client.edit_message_text("-100", "5", "same text")
    client.edit_message_text("-100", "5", "same text")
    client.edit_message_text("-100", "5", "same text")
    edits = [c for c in calls if c[0] == "editMessageText"]
    assert len(edits) == 1, "a no-op edit must not reach Telegram"

    client.edit_message_text("-100", "5", "changed")
    assert len([c for c in calls if c[0] == "editMessageText"]) == 2


def test_a_vanished_message_does_not_break_a_run():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ok": False, "error_code": 400,
            "description": "Bad Request: message to edit not found"})

    client = LiveTelegramClient(token="t", group_chat_id="-100",
                                http=httpx.Client(transport=httpx.MockTransport(handler)))
    client.edit_message_text("-100", "5", "text")      # must not raise


def test_full_live_run_produces_no_failed_api_calls():
    """The whole loop against a Telegram that enforces the not-modified rule."""
    client, calls = _counting_client()
    app = App(human_ids={HUMAN}, agents_dir="agents/quant", client=client,
              group_chat_id="-100")
    result = _run(app)
    app.gateway.handle_callback(Callback(data=f"approve|{result.run_id}|review",
                                         user_id=HUMAN))
    assert app.storage.load_run(result.run_id)[1].status.value == "done"
    app.close()
