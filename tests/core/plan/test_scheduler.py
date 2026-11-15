"""PlanScheduler（M2-03）：到点写 decision、只触发一次、崩溃重扫不重复、宽限外记 missed。"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from core.event_bus import EventBus  # noqa: E402
from core.ledger import Ledger  # noqa: E402
from core.plan.schedule import ScheduleStore  # noqa: E402
from core.plan.scheduler import PlanScheduler, decision_id  # noqa: E402


def ts(*args):
    return datetime(*args).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class Bell:
    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event.data)


@pytest.fixture
def env(tmp_path):
    clock = Clock(ts(2026, 11, 15, 20, 0))
    schedules = ScheduleStore(tmp_path / "s.db", clock=clock).open()
    ledger = Ledger(tmp_path / "shared", clock=clock)
    bus = EventBus()
    bus.reset()
    bell = Bell()
    bus.subscribe("plan.fired", bell)
    cfg = {"enabled": True, "misfire_grace_minutes": 10}
    sched = PlanScheduler(schedules, ledger, bus=bus, clock=clock, config_reader=lambda: cfg)
    yield clock, schedules, ledger, sched, bell, cfg
    bus.reset()
    schedules.close()


def add_daily(schedules):
    return schedules.add("0 21 * * *", {"content": "睡觉"}, owner="local",
                         session_id="web:a", platform="web")


def test_nothing_before_fire(env):
    clock, schedules, ledger, sched, bell, _ = env
    add_daily(schedules)
    assert sched.tick() == []
    assert ledger.list("decision") == [] and bell.events == []


def test_fires_once_at_time(env):
    clock, schedules, ledger, sched, bell, _ = env
    row = add_daily(schedules)
    clock.t = ts(2026, 11, 15, 21, 0, 5)
    written = sched.tick()
    assert len(written) == 1
    d = written[0]
    assert d["id"] == decision_id(row["id"], ts(2026, 11, 15, 21, 0))
    assert d["status"] == "pending" and d["trigger"] == "睡觉"
    assert (d["owner"], d["session_id"], d["platform"], d["ref"]) == ("local", "web:a", "web", row["id"])
    assert bell.events and bell.events[0]["decision_id"] == d["id"]
    # 同一分钟再扫：不再触发
    clock.t += 20
    assert sched.tick() == []
    assert len(ledger.list("decision")) == 1 and len(bell.events) == 1
    # 第二天再触发一次
    clock.t = ts(2026, 11, 16, 21, 0, 1)
    assert len(sched.tick()) == 1
    assert len(ledger.list("decision")) == 2


def test_crash_between_write_and_claim_does_not_duplicate(env):
    clock, schedules, ledger, sched, bell, _ = env
    row = add_daily(schedules)
    clock.t = ts(2026, 11, 15, 21, 0, 5)
    fire = row["next_fire"]
    # 模拟：上次进程写完 decision、还没推进 next_fire 就崩了
    ledger.add_if_absent("decision", decision_id(row["id"], fire), owner="local",
                         title="到点提醒：睡觉", status="pending", trigger="睡觉")
    assert schedules.get(row["id"])["next_fire"] == fire
    assert sched.tick() == []  # 没有新写
    assert len(ledger.list("decision")) == 1
    assert schedules.get(row["id"])["next_fire"] == ts(2026, 11, 16, 21, 0)  # 推进了
    assert len(bell.events) == 1  # 仍叫醒发言队列去发那条 pending


def test_restart_within_grace_fires_once(env, tmp_path):
    clock, schedules, ledger, sched, bell, cfg = env
    add_daily(schedules)
    clock.t = ts(2026, 11, 15, 21, 6)  # 停机，21:06 才起来（宽限 10 分钟内）
    fresh = PlanScheduler(schedules, Ledger(tmp_path / "shared", clock=clock),
                          clock=clock, config_reader=lambda: cfg)
    written = fresh.tick()
    assert [d["status"] for d in written] == ["pending"]
    assert fresh.tick() == [] and sched.tick() == []


def test_beyond_grace_records_missed_without_doorbell(env):
    clock, schedules, ledger, sched, bell, _ = env
    row = add_daily(schedules)
    clock.t = ts(2026, 11, 15, 23, 0)
    written = sched.tick()
    assert [d["status"] for d in written] == ["missed"]
    assert bell.events == []
    assert schedules.get(row["id"])["next_fire"] == ts(2026, 11, 16, 21, 0)


def test_disabled_config_does_nothing(env):
    clock, schedules, ledger, sched, bell, cfg = env
    add_daily(schedules)
    cfg["enabled"] = False
    clock.t = ts(2026, 11, 15, 21, 0, 5)
    assert sched.tick() == []
    assert schedules.due()  # 没领走，开回来还能处理


def test_once_schedule_fires_then_disables(env):
    clock, schedules, ledger, sched, bell, _ = env
    at = ts(2026, 11, 15, 20, 30)
    row = schedules.add(f"@at {at:.0f}", {"content": "喝水"}, owner="local", session_id="web:a")
    clock.t = at + 1
    assert len(sched.tick()) == 1
    assert schedules.get(row["id"])["enabled"] is False
    clock.t += 3600
    assert sched.tick() == []
