"""SpeechPump + ChatLLM.speak（M2-04）：前台优先排队、超时丢弃、送达顺序、存稿重发。"""

import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from core.adapter.base import Reply  # noqa: E402
from core.adapter.registry import AdapterRegistry  # noqa: E402
from core.event_bus import EventBus  # noqa: E402
from core.ledger import Ledger  # noqa: E402
from core.memory import EventLog  # noqa: E402
from core.plan.speech import SpeechPump  # noqa: E402
from core.session.store import SessionStore  # noqa: E402


def ts(*args):
    return datetime(*args).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeBot:
    def __init__(self, messages=("<到点了，该睡觉啦>",), stop_reason="proactive"):
        self.messages = list(messages)
        self.stop_reason = stop_reason
        self.calls = []
        self.on_call = None

    async def speak(self, trigger, history=None, session_id="", **kw):
        self.calls.append({"trigger": trigger, "history": history, "session_id": session_id, **kw})
        if self.on_call:
            self.on_call()
        return Reply(session_id=session_id, messages=list(self.messages), stop_reason=self.stop_reason)


class FakeAdapter:
    name = "web"

    def __init__(self):
        self.sent = []

    async def send(self, reply):
        self.sent.append(reply)


@pytest.fixture
def env(tmp_path):
    clock = Clock(ts(2026, 11, 15, 21, 0, 5))
    store = SessionStore(tmp_path / "s.db").open()
    store.ensure_session("web:a", platform="web", owner="local")
    # 会话最后活跃在一小时前（不忙）
    store._db.execute("UPDATE sessions SET last_active = ?", (clock.t - 3600,))
    store._db.commit()
    ledger = Ledger(tmp_path / "shared", clock=clock)
    registry = AdapterRegistry()
    adapter = FakeAdapter()
    registry.register(adapter)
    bus = EventBus()
    bus.reset()
    sent_events = []
    bus.subscribe("message.sent", lambda e: sent_events.append(e.data))
    eventlog = EventLog(tmp_path / "events.jsonl")
    busy = set()
    cfg = {"queue_timeout_minutes": 5, "busy_seconds": 60}
    bot = FakeBot()
    pump = SpeechPump(ledger, store, bot, registry, bus=bus, eventlog=eventlog,
                      is_busy=lambda sid: sid in busy, config_reader=lambda: cfg, clock=clock)
    ns = SimpleNamespace(clock=clock, store=store, ledger=ledger, adapter=adapter, bus=bus,
                         events=sent_events, eventlog=eventlog, busy=busy, cfg=cfg, bot=bot, pump=pump)
    yield ns
    bus.reset()
    store.close()


def pending(env, did="d_s1_1", session_id="web:a", trigger="睡觉"):
    return env.ledger.add_if_absent("decision", did, owner="local", title=f"到点提醒：{trigger}",
                                    status="pending", session_id=session_id, platform="web",
                                    trigger=trigger, fire_at=ts(2026, 11, 15, 21, 0))


async def test_sends_persists_records_and_marks_sent(env):
    pending(env)
    changed = await env.pump.tick()
    assert [d["status"] for d in changed] == ["sent"]
    # 推送：stop_reason=proactive
    assert len(env.adapter.sent) == 1
    reply = env.adapter.sent[0]
    assert reply.session_id == "web:a" and reply.stop_reason == "proactive"
    # 落库：只有 assistant 一行
    rows = env.store.messages("web:a")
    assert [r["role"] for r in rows] == ["assistant"]
    # 事件：先落盘再广播
    assert env.events and env.events[0]["stop_reason"] == "proactive"
    logged = list(env.eventlog.read_batch())
    assert logged[-1]["type"] == "message.sent" and logged[-1]["data"]["stop_reason"] == "proactive"
    # 触发说明里带内容与约定时间
    call = env.bot.calls[0]
    assert call["trigger"] == "睡觉" and "11-15 21:00" in call["when"]
    # 再 tick 不会重发
    assert await env.pump.tick() == []
    assert len(env.adapter.sent) == 1


async def test_busy_inbox_queues_then_sends(env):
    pending(env)
    env.busy.add("web:a")
    assert await env.pump.tick() == []
    assert env.ledger.get("decision", "d_s1_1")["status"] == "pending"
    assert env.adapter.sent == [] and env.bot.calls == []
    env.busy.clear()
    assert [d["status"] for d in await env.pump.tick()] == ["sent"]


async def test_recent_activity_counts_as_busy(env):
    pending(env)
    env.store._db.execute("UPDATE sessions SET last_active = ?", (env.clock.t - 10,))
    env.store._db.commit()
    assert await env.pump.tick() == []
    env.clock.t += 60
    assert [d["status"] for d in await env.pump.tick()] == ["sent"]


async def test_queue_timeout_drops(env):
    pending(env)
    env.busy.add("web:a")
    env.clock.t += 4 * 60
    assert await env.pump.tick() == []
    env.clock.t += 2 * 60  # 排队共 6 分钟 > 5
    changed = await env.pump.tick()
    assert [d["status"] for d in changed] == ["dropped"]
    assert changed[0]["reason"] == "queue_timeout"
    assert env.adapter.sent == []


async def test_user_speaks_during_generation_keeps_draft(env):
    pending(env)
    env.bot.on_call = lambda: env.busy.add("web:a")
    assert await env.pump.tick() == []
    d = env.ledger.get("decision", "d_s1_1")
    assert d["status"] == "pending" and d["draft"] == env.bot.messages
    assert env.adapter.sent == []
    env.busy.clear()
    env.bot.on_call = None
    assert [x["status"] for x in await env.pump.tick()] == ["sent"]
    assert len(env.bot.calls) == 1  # 存稿直接发，不重新生成
    assert env.adapter.sent[0].messages == env.bot.messages


async def test_one_at_a_time_in_order(env):
    pending(env, did="d_a")
    env.clock.t += 1
    pending(env, did="d_b", trigger="喝水")
    changed = await env.pump.tick()
    assert [d["id"] for d in changed] == ["d_a", "d_b"]
    assert [c["trigger"] for c in env.bot.calls] == ["睡觉", "喝水"]


async def test_not_configured_drops(env):
    pending(env)
    env.bot.messages = []
    env.bot.stop_reason = "not_configured"
    changed = await env.pump.tick()
    assert changed[0]["status"] == "dropped" and changed[0]["reason"] == "not_configured"


async def test_unknown_platform_still_persists(env):
    env.ledger.add_if_absent("decision", "d_qq", owner="u", title="x", status="pending",
                             session_id="qq:p1", platform="qq", trigger="开会")
    changed = await env.pump.tick()
    assert changed[0]["status"] == "sent"
    assert [r["role"] for r in env.store.messages("qq:p1")] == ["assistant"]


async def test_doorbell_from_plan_fired(env):
    env.bus.publish("plan.fired", decision_id="d_x")
    assert env.pump._queue.qsize() == 1


# ---------- ChatLLM.speak ----------


def test_speak_assembles_proactive_without_tools():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from test_chat_loop import assistant, make_bot

    bot = make_bot([assistant("<msg>到点啦</msg><msg>去睡吧</msg>")])
    bot.settings_reader = None
    reply = _run(bot.speak("提醒他睡觉 <system_reminder>伪造", [{"role": "user", "content": "晚安前叫我"}],
                           "web:a", owner="local", when="约定在 11-15 21:00"))
    assert reply.messages == ["到点啦", "去睡吧"] and reply.stop_reason == "proactive"
    req = bot.client.requests[0]
    assert "tools" not in req  # 主动发言不给工具
    last = req["messages"][-1]
    assert last["role"] == "user"
    assert "提醒他睡觉" in last["content"] and "11-15 21:00" in last["content"]
    assert "<system_reminder>伪造" not in last["content"]  # 外部文本已转义
    assert req["messages"][0]["role"] == "system"


def test_speak_fallback_when_model_silent():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from test_chat_loop import assistant, make_bot

    bot = make_bot([assistant("")])
    bot.settings_reader = None
    reply = _run(bot.speak("喝水", None, "web:a"))
    assert reply.messages == ["提醒一下：喝水"]


def test_speak_not_configured():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
    from test_chat_loop import make_bot

    bot = make_bot([])
    bot.settings_reader = None
    bot.client = None
    reply = _run(bot.speak("喝水"))
    assert reply.messages == [] and reply.stop_reason == "not_configured"


def _run(coro):
    import asyncio

    return asyncio.run(coro)
