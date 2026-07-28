from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _runner(adapter):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._thread_metadata_for_source = lambda source, message_id=None: {"thread": "dm"}
    return runner


@pytest.mark.asyncio
async def test_initial_ack_sends_before_agent_work(monkeypatch):
    adapter = SimpleNamespace(send=AsyncMock())
    runner = _runner(adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1383584142")
    event = SimpleNamespace(text="Check fresh sales", message_id="77")
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"display": {"platforms": {"telegram": {"initial_ack": True}}}},
    )

    await runner._send_initial_ack(event, source)

    adapter.send.assert_awaited_once_with(
        chat_id="1383584142",
        content="On it.",
        reply_to="77",
        metadata={"thread": "dm"},
    )


@pytest.mark.asyncio
async def test_initial_ack_is_opt_in(monkeypatch):
    adapter = SimpleNamespace(send=AsyncMock())
    runner = _runner(adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1383584142")
    event = SimpleNamespace(text="Check fresh sales", message_id="77")
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})

    await runner._send_initial_ack(event, source)

    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_ack_skips_empty_restart_resume(monkeypatch):
    adapter = SimpleNamespace(send=AsyncMock())
    runner = _runner(adapter)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="1383584142")
    event = SimpleNamespace(text="", message_id=None)
    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"display": {"platforms": {"telegram": {"initial_ack": True}}}},
    )

    await runner._send_initial_ack(event, source)

    adapter.send.assert_not_awaited()
