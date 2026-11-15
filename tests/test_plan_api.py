"""日程与清单 API（M2-07）：读快照、启停 / 删日程、改待办、删备忘、防线。"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fastapi.testclient import TestClient  # noqa: E402

from core.adapter.web.adapter import WebAdapter  # noqa: E402
from core.ledger import Ledger  # noqa: E402
from core.plan.schedule import ScheduleStore  # noqa: E402
from core.plan_api import build_plan_routes  # noqa: E402

J = {"Content-Type": "application/json"}


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clock = Clock(datetime(2026, 11, 15, 20, 0).timestamp())
    schedules = ScheduleStore(tmp_path / "s.db", clock=clock).open()
    ledger = Ledger(tmp_path / "shared", clock=clock)
    client = TestClient(WebAdapter(extra_routes=build_plan_routes(schedules, ledger)).app)
    yield client, schedules, ledger, clock
    schedules.close()


def test_snapshot(env):
    client, schedules, ledger, _ = env
    schedules.add("0 21 * * *", {"content": "睡觉"}, owner="local", session_id="web:a")
    ledger.add("task", owner="qq1", title="交周报")
    ledger.add("decision", owner="local", title="答应了")
    ledger.add("note", owner="local", title="不加糖")
    data = client.get("/api/plan").json()
    assert data["ok"]
    assert data["schedules"][0]["describe"] == "每天 21:00"
    assert data["schedules"][0]["payload"]["content"] == "睡觉"
    assert [t["title"] for t in data["tasks"]] == ["交周报"]
    assert len(data["decisions"]) == 1 and len(data["notes"]) == 1


def test_schedule_toggle_and_delete(env):
    client, schedules, _, _ = env
    row = schedules.add("0 21 * * *")
    r = client.post(f"/api/plan/schedules/{row['id']}", json={"enabled": False}, headers=J)
    assert r.json()["ok"] and r.json()["schedule"]["enabled"] is False
    r = client.post(f"/api/plan/schedules/{row['id']}", json={"enabled": True}, headers=J)
    assert r.json()["schedule"]["enabled"] is True
    r = client.post(f"/api/plan/schedules/{row['id']}", json={"delete": True}, headers=J)
    assert r.json()["ok"] and schedules.get(row["id"]) is None
    assert client.post(f"/api/plan/schedules/{row['id']}", json={"delete": True}, headers=J).status_code == 404


def test_past_once_cannot_reenable(env):
    client, schedules, _, clock = env
    at = clock.t + 60
    row = schedules.add(f"@at {at:.0f}")
    clock.t = at + 5
    schedules.claim(row["id"], at)
    r = client.post(f"/api/plan/schedules/{row['id']}", json={"enabled": True}, headers=J)
    assert r.status_code == 409


def test_schedule_bad_body(env):
    client, schedules, _, _ = env
    row = schedules.add("0 21 * * *")
    assert client.post(f"/api/plan/schedules/{row['id']}", json={"enabled": "yes"}, headers=J).status_code == 400


def test_task_status_and_delete(env):
    client, _, ledger, _ = env
    t = ledger.add("task", owner="local", title="交周报")
    r = client.post(f"/api/plan/tasks/{t['id']}", json={"status": "done"}, headers=J)
    assert r.json()["ok"] and ledger.get("task", t["id"])["status"] == "done"
    assert client.post(f"/api/plan/tasks/{t['id']}", json={"status": "weird"}, headers=J).status_code == 400
    assert client.post(f"/api/plan/tasks/{t['id']}", json={"delete": True}, headers=J).json()["ok"]
    assert ledger.get("task", t["id"]) is None


def test_note_only_delete(env):
    client, _, ledger, _ = env
    n = ledger.add("note", owner="local", title="x")
    assert client.post(f"/api/plan/notes/{n['id']}", json={"status": "done"}, headers=J).status_code == 400
    assert client.post(f"/api/plan/notes/{n['id']}", json={"delete": True}, headers=J).json()["ok"]


def test_guards(env):
    client, schedules, _, _ = env
    row = schedules.add("0 21 * * *")
    url = f"/api/plan/schedules/{row['id']}"
    assert client.post(url, content="enabled=false").status_code == 415
    assert client.post(url, json={"enabled": False},
                       headers={**J, "Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/plan/tasks/t_nope", json={"status": "done"}, headers=J).status_code == 404
