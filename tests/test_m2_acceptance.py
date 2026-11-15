"""M2 验收（§二十）：三条全过才算 M2 完成。不碰真 API（假 client 脚本化 ChatLLM）。

① 设"每天 21:00 提醒我睡觉"，到点 WebUI 弹出
② 正在聊天时不打断（排队）
③ 重启后不丢、不重复触发
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from core.adapter.base import Message  # noqa: E402
from core.adapter.registry import AdapterRegistry  # noqa: E402
from core.adapter.web.adapter import WebAdapter  # noqa: E402
from core.event_bus import EventBus  # noqa: E402
from core.executor import ToolExecutor  # noqa: E402
from core.inbox import Inbox, ReplyPacing  # noqa: E402
from core.ledger import Ledger  # noqa: E402
from core.memory import EventLog  # noqa: E402
from core.plan import services  # noqa: E402
from core.plan.schedule import ScheduleStore  # noqa: E402
from core.plan.scheduler import PlanScheduler  # noqa: E402
from core.plan.speech import SpeechPump  # noqa: E402
from core.plugin.guard import PermissionGuard  # noqa: E402
from core.plugin.registry import PluginRegistry  # noqa: E402
from core.session.store import SessionStore  # noqa: E402
from test_chat_loop import assistant, make_bot, tool_call  # noqa: E402

SID = "web:m2"
CFG = {"enabled": True, "misfire_grace_minutes": 10, "queue_timeout_minutes": 5, "busy_seconds": 60}


def ts(*args):
    return datetime(*args).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class FakeSocket:
    """WebAdapter 连接表里的一条"浏览器连接"。"""

    def __init__(self):
        self.frames = []

    async def send_json(self, payload):
        self.frames.append(payload)


def build(tmp_path, clock, script, is_busy=None):
    """按 main._build 的接法组一套 M2（存储都落在 tmp_path，可重复调用 = 重启）。"""
    store = SessionStore(tmp_path / "sessions.db").open()
    schedules = ScheduleStore(store.path, clock=clock).open()
    ledger = Ledger(tmp_path / "shared", clock=clock)
    services.configure(schedules, ledger)
    reg = PluginRegistry()
    bot = make_bot(script)
    bot.settings_reader = None
    bot.registry = reg
    bot.executor = ToolExecutor(reg, PermissionGuard(reg))
    bus = EventBus()
    web = WebAdapter(bus=bus)
    sock = FakeSocket()
    web._connections[SID] = sock
    registry = AdapterRegistry()
    registry.register(web)
    eventlog = EventLog(tmp_path / "events.jsonl")
    scheduler = PlanScheduler(schedules, ledger, bus=bus, clock=clock, config_reader=lambda: CFG)
    pump = SpeechPump(ledger, store, bot, registry, bus=bus, eventlog=eventlog,
                      is_busy=is_busy, config_reader=lambda: CFG, clock=clock)
    return SimpleNamespace(store=store, schedules=schedules, ledger=ledger, bot=bot, sock=sock,
                           scheduler=scheduler, pump=pump, eventlog=eventlog)


def close(env):
    env.schedules.close()
    env.store.close()


@pytest.fixture(autouse=True)
def isolation():
    EventBus().reset()
    reg = PluginRegistry()
    reg.reset()
    reg.scan()
    yield
    reg.reset()
    EventBus().reset()
    services.configure(None, None)


def set_reminder(env, clock):
    """用户在网页里说"每天 21:00 提醒我睡觉"——模型调 reminder_add。"""
    env.store.ensure_session(SID, platform="web", owner="local")
    env.store._db.execute("UPDATE sessions SET last_active = ?", (clock.t,))
    env.store._db.commit()
    args = json.dumps({"content": "提醒他睡觉", "time": "21:00", "repeat": "daily"}, ensure_ascii=False)
    env.bot.client.script[:0] = [
        assistant(None, [tool_call("c1", "reminder_add", args)]),
        assistant("<msg>好，每天九点叫你睡觉</msg>"),
    ]
    return asyncio.run(env.bot.run_loop("每天 21:00 提醒我睡觉", [], SID,
                                        session_type="private", owner="local"))


def test_1_daily_2100_reminder_pops_up_in_webui(tmp_path):
    clock = Clock(ts(2026, 11, 15, 20, 0))
    env = build(tmp_path, clock, [assistant("<msg>九点啦</msg><msg>该睡觉了哦</msg>")])
    reply = set_reminder(env, clock)
    assert reply.tool_calls_made == 1
    [row] = env.schedules.list()
    assert row["describe"] == "每天 21:00" and row["session_id"] == SID

    clock.t = ts(2026, 11, 15, 21, 0, 10)
    assert len(env.scheduler.tick()) == 1
    changed = asyncio.run(env.pump.tick())
    assert [d["status"] for d in changed] == ["sent"]

    # 到点 WebUI 弹出：连接收到 proactive 帧
    [frame] = env.sock.frames
    assert frame["session_id"] == SID and frame["stop_reason"] == "proactive"
    assert frame["messages"] == ["九点啦", "该睡觉了哦"]
    # 页面没开着也不丢：历史里有这条
    assert env.store.messages(SID)[-1]["content"] == "九点啦\n\n该睡觉了哦"
    # 触发说明进了最后一条 user 消息、带提醒内容
    speak_req = env.bot.client.requests[-1]
    assert "提醒他睡觉" in speak_req["messages"][-1]["content"]
    close(env)


async def test_2_does_not_interrupt_ongoing_chat(tmp_path):
    clock = Clock(ts(2026, 11, 15, 20, 0))
    handled = []

    async def handler(m):
        handled.append(m.content)

    # 输入缓冲开着：用户刚发一条、还没回 → Inbox 认为"正在聊"
    inbox = Inbox(handler, pacing=lambda: ReplyPacing(debounce_seconds=100, debounce_max_seconds=100))
    env = build(tmp_path, clock, [assistant("<msg>该睡觉啦</msg>")], is_busy=inbox.busy)
    await asyncio.to_thread(set_reminder, env, clock)

    clock.t = ts(2026, 11, 15, 21, 0, 10)
    await inbox.submit(Message(id="m1", platform="web", session_id=SID, owner="local",
                               direction="in", role="user", content="等下，我还有个问题"))
    env.scheduler.tick()
    assert await env.pump.tick() == []           # 不打断：排队
    assert env.sock.frames == []
    assert [d["status"] for d in env.ledger.list("decision", status="pending")] == ["pending"]

    await inbox.drain()                           # 她回完了
    assert handled == ["等下，我还有个问题"]
    clock.t += 61                                 # 过了"刚说过话"的窗口
    assert [d["status"] for d in await env.pump.tick()] == ["sent"]
    assert env.sock.frames[0]["stop_reason"] == "proactive"
    close(env)


def test_2b_queue_too_long_is_dropped(tmp_path):
    clock = Clock(ts(2026, 11, 15, 20, 0))
    env = build(tmp_path, clock, [], is_busy=lambda sid: True)   # 一直在聊
    set_reminder(env, clock)
    clock.t = ts(2026, 11, 15, 21, 0, 10)
    env.scheduler.tick()
    clock.t += 6 * 60
    changed = asyncio.run(env.pump.tick())
    assert [(d["status"], d["reason"]) for d in changed] == [("dropped", "queue_timeout")]
    assert env.sock.frames == []
    close(env)


def test_3_restart_neither_loses_nor_duplicates(tmp_path):
    clock = Clock(ts(2026, 11, 15, 20, 0))
    env = build(tmp_path, clock, [])
    set_reminder(env, clock)

    # 到点：scheduler 写了 decision，但发言之前进程挂了
    clock.t = ts(2026, 11, 15, 21, 0, 10)
    assert len(env.scheduler.tick()) == 1
    close(env)

    # 重启：新实例、同一份磁盘
    clock.t += 30
    env2 = build(tmp_path, clock, [assistant("<msg>该睡觉啦</msg>")])
    assert env2.scheduler.tick() == []                      # 不重复触发
    assert [d["status"] for d in asyncio.run(env2.pump.tick())] == ["sent"]   # 不丢
    assert len(env2.sock.frames) == 1
    assert env2.scheduler.tick() == [] and asyncio.run(env2.pump.tick()) == []
    assert len(env2.ledger.list("decision", status=("pending", "sent"))) == 1
    close(env2)

    # 停机跨过第二天 21:00（宽限内）→ 启动补发一次；再重启也不再发
    clock.t = ts(2026, 11, 16, 21, 5)
    env3 = build(tmp_path, clock, [assistant("<msg>今天也早点睡</msg>")])
    assert [d["status"] for d in env3.scheduler.tick()] == ["pending"]
    assert [d["status"] for d in asyncio.run(env3.pump.tick())] == ["sent"]
    close(env3)
    env4 = build(tmp_path, clock, [])
    assert env4.scheduler.tick() == [] and asyncio.run(env4.pump.tick()) == []
    assert len(env4.ledger.list("decision", status="sent")) == 2
    close(env4)
