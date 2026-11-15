"""日程存储（M2-02）：SQLite `schedules` 表（§18.3 / §18.4）。

放在 `data/sessions.db`（§18.3 把它和 sessions 写在同一份 schema 里），但用
**独立的类 + 独立连接**——SessionStore 已经很长，日程与会话也没有跨表事务；
WAL 下两条连接各自短事务互不阻塞。

§18.3 原样的六列之外补了四列（只增不改）：owner / session_id / platform（谁设的、
到点发回哪条会话）与 created_at。

**领取防重复**（§18.4"事务领取"）：`claim()` 是一条带 `AND next_fire = ?` 的
UPDATE（compare-and-set），只有 rowcount=1 才算领到。两次 tick 撞车、或者
两个进程同时跑，同一个触发点也只会被领走一次。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable
from uuid import uuid4

from core.plan import cron
from core.session.store import DEFAULT_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules(
  id TEXT PRIMARY KEY,
  cron TEXT NOT NULL,
  payload TEXT,
  next_fire REAL NOT NULL,
  last_fire REAL,
  enabled INTEGER NOT NULL DEFAULT 1,
  owner TEXT NOT NULL DEFAULT '',
  session_id TEXT NOT NULL DEFAULT '',
  platform TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_schedules_due ON schedules(enabled, next_fire);
"""


def _row(r: sqlite3.Row) -> dict:
    out = dict(r)
    try:
        out["payload"] = json.loads(r["payload"]) if r["payload"] else {}
    except (TypeError, ValueError):
        out["payload"] = {}
    out["enabled"] = bool(r["enabled"])
    out["describe"] = cron.describe(r["cron"])
    return out


class ScheduleStore:
    """schedules 表的读写。时钟可注入（测试拨时间用）。"""

    def __init__(self, path: Path | str | None = None, clock: Callable[[], float] | None = None):
        self.path = Path(path) if path is not None else DEFAULT_DB_PATH
        self.clock = clock or time.time
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def open(self) -> "ScheduleStore":
        if self._conn is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        except Exception:
            conn.close()
            raise
        self._conn = conn
        return self

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("ScheduleStore 未打开，请先调用 open()")
        return self._conn

    # ---------- 写 ----------

    def add(
        self,
        expr: str,
        payload: dict | None = None,
        *,
        owner: str = "",
        session_id: str = "",
        platform: str = "",
        schedule_id: str | None = None,
    ) -> dict:
        """新建一条日程。表达式不合法 / 不会再触发（一次性的已过去）→ CronError。"""
        now = self.clock()
        cron.validate(expr)
        first = cron.next_fire(expr, now)
        if first is None:
            raise cron.CronError(f"这个时间不会再到了：{expr!r}")
        sid = schedule_id or f"s_{uuid4().hex[:10]}"
        with self._lock:
            self._db.execute(
                "INSERT INTO schedules(id, cron, payload, next_fire, last_fire, enabled,"
                " owner, session_id, platform, created_at) VALUES(?,?,?,?,NULL,1,?,?,?,?)",
                (sid, expr.strip(), json.dumps(payload or {}, ensure_ascii=False), first,
                 owner, session_id, platform, now),
            )
            self._db.commit()
        return self.get(sid)  # type: ignore[return-value]

    def claim(self, schedule_id: str, expected_next_fire: float, now: float | None = None) -> bool:
        """领取一次触发：推进 next_fire（CAS）。返回是否领到。

        下一次从 max(now, 本次触发点) 往后算——停机跨过好几个周期时只往后跳，
        不会补出一串。一次性日程领取即停用。
        """
        now = self.clock() if now is None else now
        with self._lock:
            row = self._db.execute("SELECT cron FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
            if row is None:
                return False
            nxt = cron.next_fire(row["cron"], max(now, expected_next_fire))
            cur = self._db.execute(
                "UPDATE schedules SET next_fire = ?, last_fire = ?, enabled = ?"
                " WHERE id = ? AND next_fire = ? AND enabled = 1",
                (nxt if nxt is not None else expected_next_fire, expected_next_fire,
                 1 if nxt is not None else 0, schedule_id, expected_next_fire),
            )
            self._db.commit()
            return cur.rowcount == 1

    def set_enabled(self, schedule_id: str, enabled: bool) -> dict | None:
        """启停。重新启用时从现在起算 next_fire；一次性且已过去的不能再启用（返回 None）。"""
        with self._lock:
            row = self._db.execute("SELECT cron FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
            if row is None:
                return None
            if enabled:
                nxt = cron.next_fire(row["cron"], self.clock())
                if nxt is None:
                    return None
                self._db.execute(
                    "UPDATE schedules SET enabled = 1, next_fire = ? WHERE id = ?", (nxt, schedule_id)
                )
            else:
                self._db.execute("UPDATE schedules SET enabled = 0 WHERE id = ?", (schedule_id,))
            self._db.commit()
        return self.get(schedule_id)

    def delete(self, schedule_id: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            self._db.commit()
            return cur.rowcount == 1

    # ---------- 读 ----------

    def get(self, schedule_id: str) -> dict | None:
        r = self._db.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        return _row(r) if r is not None else None

    def list(self, *, owner: str | None = None, enabled: bool | None = None) -> list[dict]:
        """按 next_fire 近→远。"""
        sql, args = "SELECT * FROM schedules WHERE 1=1", []
        if owner is not None:
            sql += " AND owner = ?"
            args.append(owner)
        if enabled is not None:
            sql += " AND enabled = ?"
            args.append(1 if enabled else 0)
        sql += " ORDER BY enabled DESC, next_fire ASC"
        return [_row(r) for r in self._db.execute(sql, args).fetchall()]

    def due(self, now: float | None = None) -> list[dict]:
        """到点了的（enabled 且 next_fire ≤ now），先到先领。"""
        now = self.clock() if now is None else now
        rows = self._db.execute(
            "SELECT * FROM schedules WHERE enabled = 1 AND next_fire <= ? ORDER BY next_fire ASC",
            (now,),
        ).fetchall()
        return [_row(r) for r in rows]
