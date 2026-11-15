"""公共清单存储（M2-01）：data/shared/{tasks,decisions,notes}.jsonl。

契约（§18.3，三种清单同构）：
  LedgerEntry: {id, owner, kind, title, body, status, changed, updated_at}
  补充键（只增不改）：session_id（在哪条会话里产生）、created_at、ref（关联对象，
  如 decision 指回触发它的日程 id）、fire_at（decision 的计划触发时刻）。

守住的约束（与 M1 的 CuratedStore 同一套）：
  - **只追加**：更新 = 追加同 id 的一行，读取时按 id 合并、后写赢（§19-14）
  - **删除 = 墓碑**：追加 status=deleted，绝不 rewrite 文件（§19-8 同精神）
  - **单写锁**：同一个 Ledger 实例里"查重 + 追加"原子（add_if_absent 靠它）
  - **坏行跳过**：崩溃写一半的行不能让整个清单读不出来
  - **时钟可注入**

status 取值：
  task      open / done / cancelled
  decision  recorded（普通决定）/ pending（待发言）/ sent / dropped / missed
  note      active
  三者共用墓碑 deleted
"""

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

# store.py 位于 src/core/ledger/ → parents[3] 是项目根
DEFAULT_LEDGER_DIR = Path(__file__).resolve().parents[3] / "data" / "shared"

#: kind → 文件名。键同时是 LedgerEntry.kind 的合法取值。
LEDGER_KINDS = {"task": "tasks.jsonl", "decision": "decisions.jsonl", "note": "notes.jsonl"}

#: 每种清单新条目的默认状态
_DEFAULT_STATUS = {"task": "open", "decision": "recorded", "note": "active"}

#: id 前缀：一眼看出是哪种清单（§18.3 示例 t_01）
_ID_PREFIX = {"task": "t_", "decision": "d_", "note": "n_"}

DELETED = "deleted"


class Ledger:
    """三种清单共用一个实例、一把写锁。"""

    def __init__(self, root: Path | str | None = None, clock: Callable[[], float] | None = None):
        self.root = Path(root) if root is not None else DEFAULT_LEDGER_DIR
        self.clock = clock or time.time
        self._lock = threading.Lock()

    # ---------- 路径 ----------

    def path(self, kind: str) -> Path:
        if kind not in LEDGER_KINDS:
            raise ValueError(f"未知清单类型: {kind!r}（见 LEDGER_KINDS）")
        return self.root / LEDGER_KINDS[kind]

    # ---------- 写 ----------

    def add(
        self,
        kind: str,
        *,
        owner: str,
        title: str,
        body: str = "",
        status: str | None = None,
        session_id: str = "",
        entry_id: str | None = None,
        **extra: Any,
    ) -> dict:
        """追加一条新条目，返回它。entry_id 不给就生成。"""
        with self._lock:
            return self._add_locked(kind, owner, title, body, status, session_id, entry_id, extra)

    def add_if_absent(self, kind: str, entry_id: str, **fields: Any) -> dict | None:
        """同 id 不存在才写；已存在返回 None（重扫时不重复写，M2-03 的去重落点）。"""
        with self._lock:
            if entry_id in self._read_merged(kind):
                return None
            return self._add_locked(
                kind,
                fields.pop("owner", ""),
                fields.pop("title", ""),
                fields.pop("body", ""),
                fields.pop("status", None),
                fields.pop("session_id", ""),
                entry_id,
                fields,
            )

    def update(self, kind: str, entry_id: str, **changes: Any) -> dict | None:
        """追加一条同 id 的更新，返回合并后的条目；不存在 / 已删除返回 None。"""
        with self._lock:
            current = self._read_merged(kind).get(entry_id)
            if current is None or current.get("status") == DELETED:
                return None
            row = {"id": entry_id, **changes, "changed": True, "updated_at": self.clock()}
            self._append(kind, row)
            return {**current, **row}

    def delete(self, kind: str, entry_id: str) -> bool:
        """追加墓碑。返回是否真的删了（不存在 / 已删除返回 False）。"""
        return self.update(kind, entry_id, status=DELETED) is not None

    # ---------- 读 ----------

    def get(self, kind: str, entry_id: str) -> dict | None:
        entry = self._read_merged(kind).get(entry_id)
        return None if entry is None or entry.get("status") == DELETED else entry

    def list(
        self,
        kind: str,
        *,
        owner: str | None = None,
        status: str | tuple[str, ...] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """按 updated_at 新→旧列出（不含墓碑）。status 可给单个或元组。"""
        wanted = (status,) if isinstance(status, str) else status
        rows = [
            e
            for e in self._read_merged(kind).values()
            if e.get("status") != DELETED
            and (owner is None or e.get("owner") == owner)
            and (wanted is None or e.get("status") in wanted)
        ]
        rows.sort(key=lambda e: float(e.get("updated_at") or 0), reverse=True)
        return rows[:limit] if limit is not None else rows

    # ---------- 内部 ----------

    def _add_locked(self, kind, owner, title, body, status, session_id, entry_id, extra) -> dict:
        self.path(kind)  # 未知 kind 先报错，别生成半个条目
        now = self.clock()
        entry = {
            "id": entry_id or f"{_ID_PREFIX[kind]}{uuid4().hex[:12]}",
            "owner": owner,
            "kind": kind,
            "title": title,
            "body": body,
            "status": status or _DEFAULT_STATUS[kind],
            "changed": True,
            "updated_at": now,
            "session_id": session_id,
            "created_at": now,
            **extra,
        }
        self._append(kind, entry)
        return entry

    def _append(self, kind: str, row: dict) -> None:
        path = self.path(kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _read_merged(self, kind: str) -> dict[str, dict]:
        path = self.path(kind)
        merged: dict[str, dict] = {}
        try:
            f = open(path, "r", encoding="utf-8")
        except FileNotFoundError:
            return merged
        with f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # 崩溃写一半的行
                entry_id = row.get("id") if isinstance(row, dict) else None
                if not entry_id:
                    continue
                if entry_id in merged:
                    merged[entry_id].update(row)
                else:
                    merged[entry_id] = row
        return merged
