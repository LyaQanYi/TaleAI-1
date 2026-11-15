"""日程与清单的读写 API（M2-07 · 面板「日程」页的后端）。

    GET  /api/plan                    一次给全：日程 + 待办 + 决定 + 备忘（各取最近若干）
    POST /api/plan/schedules/{id}     {"enabled": bool} 启停 / {"delete": true} 删除
    POST /api/plan/tasks/{id}         {"status": "open|done|cancelled"} / {"delete": true}
    POST /api/plan/notes/{id}         {"delete": true}

面板是本机单人自用（Host 白名单在适配器那层），这里列**全部 owner** 的条目——
看到 QQ 上谁设了什么正是面板的用处。写接口走 `_guard_post_json`（Content-Type /
同源 / JSON 校验）。决定（decisions）只读：那是她做过的事的记录，不该事后改。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from core.config.api import _bad, _guard_post_json
from core.ledger import Ledger
from core.plan.schedule import ScheduleStore

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: 各类最多给多少条。面板是"看一眼她在忙什么"，不是全量导出。
LIST_LIMIT = 100

_TASK_STATUSES = ("open", "done", "cancelled")


def build_plan_routes(schedules: ScheduleStore, ledger: Ledger) -> Callable[[FastAPI], None]:
    def _snapshot() -> dict:
        return {
            "ok": True,
            "now": schedules.clock(),
            "schedules": schedules.list()[:LIST_LIMIT],
            "tasks": ledger.list("task", limit=LIST_LIMIT),
            "decisions": ledger.list("decision", limit=LIST_LIMIT),
            "notes": ledger.list("note", limit=LIST_LIMIT),
        }

    def register(app: FastAPI) -> None:
        @app.get("/api/plan")
        async def read_plan() -> JSONResponse:
            try:
                return JSONResponse(await asyncio.to_thread(_snapshot))
            except Exception:
                logger.exception("读取日程与清单失败")
                return JSONResponse({"ok": False, "error": "读取日程与清单失败"})

        @app.post("/api/plan/schedules/{schedule_id}")
        async def write_schedule(schedule_id: str, request: Request) -> JSONResponse:
            body, err = await _guard_post_json(request)
            if err is not None:
                return err
            if schedules.get(schedule_id) is None:
                return _bad("没有这个日程", code=404)
            if body.get("delete") is True:
                await asyncio.to_thread(schedules.delete, schedule_id)
                return JSONResponse({"ok": True, "deleted": schedule_id})
            enabled = body.get("enabled")
            if not isinstance(enabled, bool):
                return _bad("要给 enabled（true/false）或 delete: true")
            row = await asyncio.to_thread(schedules.set_enabled, schedule_id, enabled)
            if row is None:
                return _bad("这个一次性日程的时间已经过了，不能再启用", code=409)
            return JSONResponse({"ok": True, "schedule": row})

        async def _write_entry(kind: str, entry_id: str, request: Request) -> JSONResponse:
            body, err = await _guard_post_json(request)
            if err is not None:
                return err
            if ledger.get(kind, entry_id) is None:
                return _bad("没有这一条", code=404)
            if body.get("delete") is True:
                await asyncio.to_thread(ledger.delete, kind, entry_id)
                return JSONResponse({"ok": True, "deleted": entry_id})
            status = body.get("status")
            if kind != "task" or status not in _TASK_STATUSES:
                return _bad("待办只能改 status（open/done/cancelled）或 delete: true；备忘只能删除")
            entry = await asyncio.to_thread(ledger.update, kind, entry_id, status=status)
            return JSONResponse({"ok": True, "entry": entry})

        @app.post("/api/plan/tasks/{entry_id}")
        async def write_task(entry_id: str, request: Request) -> JSONResponse:
            return await _write_entry("task", entry_id, request)

        @app.post("/api/plan/notes/{entry_id}")
        async def write_note(entry_id: str, request: Request) -> JSONResponse:
            return await _write_entry("note", entry_id, request)

    return register
