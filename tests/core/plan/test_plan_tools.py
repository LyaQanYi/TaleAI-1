"""内置 plan 插件（M2-05）：设提醒 / 列 / 取消、待办、备忘；owner 隔离；经 FC 循环带上会话上下文。"""

import json
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from core.ledger import Ledger  # noqa: E402
from core.plan import services  # noqa: E402
from core.plan.schedule import ScheduleStore  # noqa: E402
from core.plugin.registry import PluginRegistry  # noqa: E402
from core.tool_context import ToolContext, current_tool_context, tool_context  # noqa: E402


def ts(*args):
    return datetime(*args).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clock = Clock(ts(2026, 11, 15, 20, 0))
    store = ScheduleStore(tmp_path / "s.db", clock=clock).open()
    ledger = Ledger(tmp_path / "shared", clock=clock)
    services.configure(store, ledger)
    reg = PluginRegistry()
    reg.reset()
    reg.scan()
    yield store, ledger, reg, clock
    reg.reset()
    services.configure(None, None)
    store.close()


def call(reg, name, ctx=ToolContext("web:a", "local", "web", "private"), **kwargs):
    with tool_context(ctx):
        return reg.tools()[name].handler(**kwargs)


def test_plugin_registered(env):
    _, _, reg, _ = env
    names = {s["function"]["name"] for s in reg.tool_schemas()}
    assert {"reminder_add", "reminder_list", "reminder_cancel", "task_add",
            "task_update", "task_list", "note_add"} <= names


def test_reminder_add_daily(env):
    store, ledger, reg, _ = env
    out = call(reg, "reminder_add", content="提醒他睡觉", time="21:00", repeat="daily")
    assert out["ok"] and out["when"] == "每天 21:00" and out["next"] == "2026-11-15 21:00"
    row = store.get(out["id"])
    assert (row["owner"], row["session_id"], row["platform"]) == ("local", "web:a", "web")
    assert row["payload"] == {"content": "提醒他睡觉", "kind": "reminder"}
    decisions = ledger.list("decision")
    assert len(decisions) == 1 and "每天 21:00" in decisions[0]["title"]
    assert decisions[0]["ref"] == out["id"] and decisions[0]["status"] == "recorded"


def test_reminder_after_minutes(env):
    store, _, reg, clock = env
    out = call(reg, "reminder_add", content="喝水", after_minutes=30)
    assert out["ok"]
    assert store.get(out["id"])["next_fire"] == clock.t + 1800


@pytest.mark.parametrize("kwargs", [
    {"content": "x"}, {"content": "", "time": "21:00"}, {"content": "x", "time": "25:00"},
    {"content": "x", "time": "21:00", "repeat": "hourly"}, {"content": "x", "after_minutes": -5},
])
def test_reminder_add_errors(env, kwargs):
    _, _, reg, _ = env
    out = call(reg, "reminder_add", **kwargs)
    assert out["ok"] is False and out["error"]


def test_no_context_refused(env):
    _, _, reg, _ = env
    assert current_tool_context() is None
    out = reg.tools()["reminder_add"].handler(content="x", time="21:00")
    assert out["ok"] is False


def test_services_not_configured(env):
    _, _, reg, _ = env
    services.configure(None, None)
    assert call(reg, "reminder_add", content="x", time="21:00")["ok"] is False
    assert call(reg, "task_add", title="x")["ok"] is False


def test_list_and_cancel_are_owner_scoped(env):
    store, ledger, reg, _ = env
    mine = call(reg, "reminder_add", content="睡觉", time="21:00", repeat="daily")
    other = ToolContext("qq:p2", "u2", "qq", "private")
    call(reg, "reminder_add", ctx=other, content="开会", time="9:00", repeat="weekdays")
    listed = call(reg, "reminder_list")["reminders"]
    assert [r["content"] for r in listed] == ["睡觉"]
    # 别人取消不了我的
    assert call(reg, "reminder_cancel", ctx=other, id=mine["id"])["ok"] is False
    assert call(reg, "reminder_cancel", id=mine["id"])["ok"] is True
    assert store.get(mine["id"]) is None
    assert any("取消了" in d["title"] for d in ledger.list("decision", owner="local"))


def test_tasks_and_notes(env):
    _, ledger, reg, _ = env
    t = call(reg, "task_add", title="交周报", body="周五前")
    assert t["ok"]
    assert [x["title"] for x in call(reg, "task_list")["tasks"]] == ["交周报"]
    assert call(reg, "task_update", id=t["id"], status="done")["ok"]
    assert call(reg, "task_list")["tasks"] == []
    assert [x["status"] for x in call(reg, "task_list", status="all")["tasks"]] == ["done"]
    assert call(reg, "task_update", id=t["id"], status="bogus")["ok"] is False
    other = ToolContext("qq:p2", "u2", "qq", "private")
    assert call(reg, "task_update", ctx=other, id=t["id"], status="open")["ok"] is False
    n = call(reg, "note_add", title="咖啡不加糖")
    assert n["ok"] and ledger.get("note", n["id"])["owner"] == "local"


def test_fc_loop_passes_session_context(env):
    """经 ChatLLM.run_loop：模型调 reminder_add，日程落到当前会话 / owner 名下。"""
    from test_chat_loop import assistant, make_bot, tool_call

    store, _, reg, _ = env
    args = json.dumps({"content": "提醒他睡觉", "time": "21:00", "repeat": "daily"}, ensure_ascii=False)
    bot = make_bot([
        assistant(None, [tool_call("c1", "reminder_add", args)]),
        assistant("<msg>好，每天九点叫你睡觉</msg>"),
    ])
    bot.registry = reg
    from core.executor import ToolExecutor
    from core.plugin.guard import PermissionGuard

    bot.executor = ToolExecutor(reg, PermissionGuard(reg))
    import asyncio

    reply = asyncio.run(bot.run_loop("每天 21:00 提醒我睡觉", [], "web:abc",
                                     session_type="private", owner="local"))
    assert reply.messages == ["好，每天九点叫你睡觉"] and reply.tool_calls_made == 1
    rows = store.list()
    assert len(rows) == 1
    assert (rows[0]["session_id"], rows[0]["owner"], rows[0]["platform"]) == ("web:abc", "local", "web")
    assert current_tool_context() is None  # 用完即复位
