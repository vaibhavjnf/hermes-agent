from types import SimpleNamespace

from gateway.run import GatewayRunner


GOAL_PROMPT = "[Continuing toward your standing goal]\nGoal: finish\n\nTake next step."


class DummyAdapter:
    def __init__(self):
        self._pending_messages = {}


def _runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._queued_events = {}
    return runner


def _event(text):
    return SimpleNamespace(text=text)


def test_goal_continuation_enqueues_when_queue_empty():
    runner = _runner()
    adapter = DummyAdapter()

    runner._enqueue_fifo("s1", _event(GOAL_PROMPT), adapter)

    assert adapter._pending_messages["s1"].text == GOAL_PROMPT
    assert runner._queued_events == {}


def test_goal_continuation_does_not_stack_behind_user_pending():
    runner = _runner()
    adapter = DummyAdapter()
    adapter._pending_messages["s1"] = _event("real user follow-up")

    runner._enqueue_fifo("s1", _event(GOAL_PROMPT), adapter)

    assert adapter._pending_messages["s1"].text == "real user follow-up"
    assert runner._queued_events == {}


def test_goal_continuation_does_not_stack_behind_overflow_queue():
    runner = _runner()
    adapter = DummyAdapter()
    runner._queued_events["s1"] = [_event("queued user follow-up")]

    runner._enqueue_fifo("s1", _event(GOAL_PROMPT), adapter)

    assert "s1" not in adapter._pending_messages
    assert [ev.text for ev in runner._queued_events["s1"]] == ["queued user follow-up"]


def test_normal_queue_items_still_fifo_when_pending_exists():
    runner = _runner()
    adapter = DummyAdapter()
    adapter._pending_messages["s1"] = _event("first")

    runner._enqueue_fifo("s1", _event("second"), adapter)

    assert adapter._pending_messages["s1"].text == "first"
    assert [ev.text for ev in runner._queued_events["s1"]] == ["second"]
