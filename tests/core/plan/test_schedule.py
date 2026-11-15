"""日程存储（M2-02）：增删查、到点查询、CAS 领取、启停。"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from core.plan.cron import CronError  # noqa: E402
from core.plan.schedule import ScheduleStore  # noqa: E402


def ts(*args):
    return datetime(*args).timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock(ts(2026, 11, 15, 20, 0))


@pytest.fixture
def store(tmp_path, clock):
    s = ScheduleStore(tmp_path / "sessions.db", clock=clock).open()
    yield s
    s.close()


def test_add_computes_next_fire(store):
    row = store.add("0 21 * * *", {"content": "睡觉"}, owner="local", session_id="web:a", platform="web")
    assert row["id"].startswith("s_")
    assert row["next_fire"] == ts(2026, 11, 15, 21, 0)
    assert row["payload"] == {"content": "睡觉"}
    assert row["enabled"] is True and row["last_fire"] is None
    assert row["describe"] == "每天 21:00"
    assert (row["owner"], row["session_id"], row["platform"]) == ("local", "web:a", "web")


def test_add_rejects_bad_or_past(store):
    with pytest.raises(CronError):
        store.add("bad")
    with pytest.raises(CronError):
        store.add(f"@at {ts(2026, 11, 15, 19, 0):.0f}")


def test_due_and_claim_once(store, clock):
    row = store.add("0 21 * * *")
    assert store.due() == []
    clock.t = ts(2026, 11, 15, 21, 0, 10)
    due = store.due()
    assert [r["id"] for r in due] == [row["id"]]
    fire = due[0]["next_fire"]
    assert store.claim(row["id"], fire) is True
    assert store.claim(row["id"], fire) is False  # 同一触发点第二次领不到
    got = store.get(row["id"])
    assert got["last_fire"] == fire and got["next_fire"] == ts(2026, 11, 16, 21, 0)
    assert store.due() == []


def test_claim_after_long_downtime_skips_forward(store, clock):
    row = store.add("0 21 * * *")
    clock.t = ts(2026, 11, 18, 9, 0)  # 停机三天
    assert store.claim(row["id"], row["next_fire"]) is True
    assert store.get(row["id"])["next_fire"] == ts(2026, 11, 18, 21, 0)


def test_once_disables_after_claim(store, clock):
    at = ts(2026, 11, 15, 20, 30)
    row = store.add(f"@at {at:.0f}")
    clock.t = at + 5
    assert store.claim(row["id"], at) is True
    got = store.get(row["id"])
    assert got["enabled"] is False
    assert store.due() == []
    assert store.set_enabled(row["id"], True) is None  # 过去的一次性不能再启用


def test_disable_enable_recomputes(store, clock):
    row = store.add("0 21 * * *")
    store.set_enabled(row["id"], False)
    clock.t = ts(2026, 11, 16, 22, 0)
    assert store.due() == []
    got = store.set_enabled(row["id"], True)
    assert got["enabled"] is True and got["next_fire"] == ts(2026, 11, 17, 21, 0)


def test_claim_disabled_fails(store):
    row = store.add("0 21 * * *")
    store.set_enabled(row["id"], False)
    assert store.claim(row["id"], row["next_fire"]) is False


def test_list_and_delete(store):
    a = store.add("0 21 * * *", owner="u1")
    b = store.add("0 7 * * *", owner="u2")
    assert [r["id"] for r in store.list()] == [a["id"], b["id"]]  # 今天 21:00 早于明天 7:00
    assert [r["id"] for r in store.list(owner="u2")] == [b["id"]]
    assert store.delete(a["id"]) is True and store.delete(a["id"]) is False
    assert store.get(a["id"]) is None


def test_persists_across_reopen(tmp_path, clock):
    path = tmp_path / "sessions.db"
    s = ScheduleStore(path, clock=clock).open()
    row = s.add("0 21 * * *")
    s.close()
    s2 = ScheduleStore(path, clock=clock).open()
    assert s2.get(row["id"])["cron"] == "0 21 * * *"
    s2.close()


def test_coexists_with_session_store(tmp_path, clock):
    from core.session.store import SessionStore

    path = tmp_path / "sessions.db"
    sessions = SessionStore(path).open()
    sched = ScheduleStore(path, clock=clock).open()
    sessions.ensure_session("web:a")
    sched.add("0 21 * * *")
    assert sessions.sessions() and sched.list()
    sched.close()
    sessions.close()
