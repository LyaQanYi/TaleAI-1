"""公共清单存储（M2-01）：同 id 后写赢、墓碑、幂等写、坏行跳过、并发追加。"""

import json
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from core.ledger import Ledger  # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "shared", clock=Clock())


def test_add_has_schema_fields(ledger):
    e = ledger.add("task", owner="u1", title="买牛奶", session_id="web:a")
    assert e["id"].startswith("t_")
    for key in ("id", "owner", "kind", "title", "body", "status", "changed", "updated_at"):
        assert key in e  # §18.3 契约键
    assert e["status"] == "open" and e["kind"] == "task" and e["session_id"] == "web:a"
    assert ledger.path("task").name == "tasks.jsonl"


def test_default_status_per_kind(ledger):
    assert ledger.add("decision", owner="u", title="x")["status"] == "recorded"
    assert ledger.add("note", owner="u", title="x")["status"] == "active"


def test_unknown_kind_raises(ledger):
    with pytest.raises(ValueError):
        ledger.add("todo", owner="u", title="x")


def test_update_appends_and_last_write_wins(ledger):
    clock = ledger.clock
    e = ledger.add("task", owner="u1", title="写周报")
    clock.t = 2000.0
    merged = ledger.update("task", e["id"], status="done")
    assert merged["status"] == "done" and merged["title"] == "写周报"
    assert ledger.get("task", e["id"])["status"] == "done"
    assert ledger.get("task", e["id"])["updated_at"] == 2000.0
    # 只追加：两行
    lines = ledger.path("task").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


def test_update_missing_returns_none(ledger):
    assert ledger.update("task", "t_nope", status="done") is None


def test_delete_is_tombstone(ledger):
    e = ledger.add("note", owner="u1", title="密码提示")
    assert ledger.delete("note", e["id"]) is True
    assert ledger.get("note", e["id"]) is None
    assert ledger.list("note") == []
    assert ledger.delete("note", e["id"]) is False  # 已删
    assert ledger.update("note", e["id"], title="x") is None
    assert len(ledger.path("note").read_text(encoding="utf-8").splitlines()) == 2


def test_add_if_absent_is_idempotent(ledger):
    first = ledger.add_if_absent("decision", "d_s1_100", owner="u", title="提醒", status="pending")
    assert first is not None and first["status"] == "pending"
    assert ledger.add_if_absent("decision", "d_s1_100", owner="u", title="提醒") is None
    assert len(ledger.list("decision")) == 1


def test_list_filters_and_orders(ledger):
    clock = ledger.clock
    a = ledger.add("task", owner="u1", title="a")
    clock.t = 1001
    ledger.add("task", owner="u2", title="b")
    clock.t = 1002
    c = ledger.add("task", owner="u1", title="c")
    clock.t = 1003
    ledger.update("task", a["id"], status="done")
    assert [e["title"] for e in ledger.list("task", owner="u1")] == ["a", "c"]
    assert [e["title"] for e in ledger.list("task", owner="u1", status="open")] == ["c"]
    assert len(ledger.list("task", status=("open", "done"))) == 3
    assert len(ledger.list("task", limit=1)) == 1
    assert c["id"] in {e["id"] for e in ledger.list("task")}


def test_restart_reads_back(tmp_path):
    root = tmp_path / "shared"
    e = Ledger(root).add("task", owner="u", title="持久")
    assert Ledger(root).get("task", e["id"])["title"] == "持久"


def test_bad_lines_skipped(ledger):
    e = ledger.add("task", owner="u", title="ok")
    with open(ledger.path("task"), "a", encoding="utf-8") as f:
        f.write('{"id": "t_half", "tit\n')
        f.write(json.dumps(["not", "dict"]) + "\n")
    assert [x["id"] for x in ledger.list("task")] == [e["id"]]


def test_concurrent_adds_not_lost(ledger):
    def worker(n):
        for i in range(25):
            ledger.add("task", owner="u", title=f"{n}-{i}")

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(ledger.list("task")) == 100


def test_concurrent_add_if_absent_writes_once(ledger):
    results = []

    def worker():
        results.append(ledger.add_if_absent("decision", "d_same", owner="u", title="x"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(r is not None for r in results) == 1
