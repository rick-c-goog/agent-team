"""The task board, and callbacks that no longer crash the runner.

Live regression: every task card carried an "Open board" button whose callback action
was never handled, so tapping it raised ValueError out of the runner. A button that can
only error is worse than no button.
"""

from teleraft.app import App
from teleraft.models import TaskStatus
from teleraft.telegram.gateway import Callback, Update

HUMAN = "11111111"


def _app():
    return App(human_ids={HUMAN}, agents_dir="agents/quant")


def _msg(app, contains):
    return next((m.text for m in app.client.messages.values() if contains in m.text), "")


# --------------------------------------------------------------------------- #
# The button that used to crash
# --------------------------------------------------------------------------- #
def test_open_board_button_renders_a_board_instead_of_raising():
    app = _app()
    task_id = app.tasks.create(topic="# research", title="check SPY momentum",
                               created_by="rick")
    app.gateway._post_task_card(task_id, claimable=True)

    board_mid = next(mid for mid in app.client.messages
                     if app.client.has_button(mid, "Open board"))
    data = app.client.button_data(board_mid, "Open board")
    assert data == f"board|{task_id}", "the button should carry its task id"

    columns = app.gateway.handle_callback(Callback(data=data, user_id=HUMAN))
    assert columns is not None
    assert "📋" in _msg(app, "📋")
    assert task_id in _msg(app, "check SPY momentum")
    app.close()


def test_legacy_board_open_button_still_works():
    """Cards posted by an older deploy send `board|open` — they must not crash."""
    app = _app()
    app.tasks.create(topic="# research", title="older task", created_by="rick")
    assert app.gateway.handle_callback(Callback(data="board|open", user_id=HUMAN)) is not None
    assert "older task" in _msg(app, "older task")
    app.close()


def test_unknown_callback_answers_instead_of_raising():
    app = _app()
    assert app.gateway.handle_callback(Callback(data="whatever|123", user_id=HUMAN)) is None
    assert "older version" in _msg(app, "older version")
    app.close()


# --------------------------------------------------------------------------- #
# Board contents
# --------------------------------------------------------------------------- #
def test_board_groups_tasks_by_status_with_counts():
    app = _app()
    todo = app.tasks.create(topic="# research", title="todo one", created_by="rick")
    doing = app.tasks.create(topic="# research", title="in flight", created_by="rick")
    app.tasks.claim(doing, "Quinn")
    done = app.tasks.create(topic="# research", title="finished", created_by="rick")
    app.tasks.set_status(done, TaskStatus.DONE)

    app.gateway.render_board(topic="# research")
    board = _msg(app, "📋")
    assert "🟡 Todo (1)" in board and todo in board
    assert "🔵 In Progress (1)" in board and "Quinn" in board
    assert "🟢 Done (1)" in board and "finished" in board
    app.close()


def test_board_scoped_to_a_topic_excludes_other_topics():
    app = _app()
    app.tasks.create(topic="# research", title="research task", created_by="rick")
    app.tasks.create(topic="# risk", title="risk task", created_by="rick")

    app.gateway.render_board(topic="# research")
    board = _msg(app, "📋")
    assert "research task" in board and "risk task" not in board
    app.close()


def test_board_all_spans_the_workspace_and_labels_topics():
    app = _app()
    app.tasks.create(topic="# research", title="research task", created_by="rick")
    app.tasks.create(topic="# risk", title="risk task", created_by="rick")

    app.gateway.handle_message(
        Update(text="/board all", user_id=HUMAN, user_handle="rick", topic="# research")
    )
    board = _msg(app, "📋")
    assert "research task" in board and "risk task" in board
    assert "# risk" in board, "workspace-wide board should say which topic each task is in"
    app.close()


def test_empty_board_explains_how_to_open_work():
    app = _app()
    app.gateway.handle_message(
        Update(text="/board", user_id=HUMAN, user_handle="rick", topic="# research")
    )
    board = _msg(app, "📋")
    assert "Nothing here yet" in board and "/task" in board
    app.close()


def test_finished_columns_are_capped_so_live_work_stays_visible():
    app = _app()
    for i in range(9):
        tid = app.tasks.create(topic="# research", title=f"old task {i}", created_by="rick")
        app.tasks.set_status(tid, TaskStatus.DONE)
    app.tasks.create(topic="# research", title="current work", created_by="rick")

    app.gateway.render_board(topic="# research")
    board = _msg(app, "📋")
    assert "current work" in board
    assert "and 4 older" in board          # 9 done, 5 shown
    app.close()


def test_board_is_advertised_in_the_agents_listing():
    app = _app()
    app.gateway.handle_message(
        Update(text="/agents", user_id=HUMAN, user_handle="rick", topic="# research")
    )
    assert "/board" in _msg(app, "Agents in this workspace")
    app.close()


def test_board_after_a_real_run_shows_the_task_in_review():
    app = _app()
    app.gateway.handle_message(
        Update(text="@Quinn is there a momentum edge on SPY", user_id=HUMAN,
               user_handle="rick", topic="# research", as_task=True, mentions=["Quinn"])
    )
    app.client.transcript.clear()
    app.gateway.render_board(topic="# research")
    board = _msg(app, "📋")
    assert "🟣 In Review (1)" in board and "Quinn" in board
    app.close()
