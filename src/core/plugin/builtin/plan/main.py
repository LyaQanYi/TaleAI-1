"""内置插件：plan（M2-05）——设提醒、记待办、记备忘。

§七"三级注入"里的**按需查**那一级：清单摘要平时强制注入（ledger_digest），
要细节 / 要改动时走这里的工具。

工具面对模型友好：不让模型写 cron，只填 time / repeat / after_minutes，表达式由
`core.plan.cron.from_friendly` 生成。设 / 删提醒同时记一条 decision——面板"她做过
的决定"看得到"答应了每天 21:00 提醒你睡觉"。

隔离：owner / 会话来自 `current_tool_context()`（ChatLLM 执行工具时设）。只能
看 / 改自己 owner 名下的东西；不在对话里（拿不到上下文）一律拒绝，不猜。
"""

import time
from datetime import datetime

from core.plan import cron, services
from core.plugin.registry import register
from core.tool_context import current_tool_context

_TASK_STATUSES = ("open", "done", "cancelled")


def _err(text: str) -> dict:
    return {"ok": False, "error": text}


def _ctx_or_error():
    ctx = current_tool_context()
    if ctx is None or not ctx.session_id:
        return None, _err("只能在对话里使用（拿不到当前会话）")
    return ctx, None


def _stamp(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")


# ---------- 提醒 ----------

_REMINDER_ADD = {
    "description": (
        "设一个提醒/日程：到点你会主动给对方发消息（问好、提醒做某事）。"
        "用户明确要你在某个时间提醒或问候他时才调用。"
        "相对时间（\"半小时后\"）用 after_minutes；绝对时间用 time（HH:MM，24 小时制）+ repeat。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "到点要做的事，如\"提醒他睡觉\"\"问早安\""},
            "time": {"type": "string", "description": "HH:MM，24 小时制，如 21:00"},
            "repeat": {
                "type": "string", "enum": list(cron.REPEATS), "default": "once",
                "description": "once=一次；daily=每天；weekdays=周一到周五；weekly=每周",
            },
            "date": {"type": "string", "description": "仅 once 用：YYYY-MM-DD；不填=下一个这个钟点"},
            "weekday": {"type": "integer", "description": "仅 weekly 用：1-7，周一=1；不填=今天是周几"},
            "after_minutes": {"type": "integer", "description": "多少分钟后提醒一次（给了就忽略 time/repeat）"},
        },
        "required": ["content"],
    },
}


@register.tool(name="reminder_add", schema=_REMINDER_ADD)
def reminder_add(content: str, time: str = "", repeat: str = "once", date: str = "",
                 weekday: int | None = None, after_minutes: int = 0) -> dict:
    ctx, error = _ctx_or_error()
    if error:
        return error
    store, ledger = services.schedules(), services.ledger()
    if store is None or ledger is None:
        return _err("作息没有启动，设不了提醒")
    content = str(content or "").strip()
    if not content:
        return _err("content 不能为空：到点要做什么？")
    now = store.clock()
    try:
        if after_minutes:
            minutes = int(after_minutes)
            if not 1 <= minutes <= 60 * 24 * 366:
                return _err("after_minutes 要在 1 分钟到一年之间")
            expr = f"@at {now + minutes * 60:.0f}"
        elif time:
            expr = cron.from_friendly(time, repeat or "once", date or None, weekday, now=now)
        else:
            return _err("要给 time（HH:MM）或 after_minutes 其中一个")
        row = store.add(expr, {"content": content, "kind": "reminder"}, owner=ctx.owner,
                        session_id=ctx.session_id, platform=ctx.platform)
    except cron.CronError as e:
        return _err(str(e))
    ledger.add("decision", owner=ctx.owner, session_id=ctx.session_id,
               title=f"答应了：{row['describe']}「{content}」", ref=row["id"])
    return {"ok": True, "id": row["id"], "when": row["describe"], "next": _stamp(row["next_fire"])}


@register.tool(name="reminder_list", schema={
    "description": "列出对方名下还在生效的提醒/日程（含 id，取消时要用）。",
    "parameters": {"type": "object", "properties": {}, "required": []},
})
def reminder_list() -> dict:
    ctx, error = _ctx_or_error()
    if error:
        return error
    store = services.schedules()
    if store is None:
        return _err("作息没有启动")
    rows = store.list(owner=ctx.owner, enabled=True)
    return {"ok": True, "reminders": [
        {"id": r["id"], "content": r["payload"].get("content", ""), "when": r["describe"],
         "next": _stamp(r["next_fire"])}
        for r in rows
    ]}


@register.tool(name="reminder_cancel", schema={
    "description": "取消一个提醒/日程。id 从 reminder_list 拿，不要猜。",
    "parameters": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "提醒 id，如 s_ab12cd34ef"}},
        "required": ["id"],
    },
})
def reminder_cancel(id: str) -> dict:  # noqa: A002  参数名就是给模型看的 id
    ctx, error = _ctx_or_error()
    if error:
        return error
    store, ledger = services.schedules(), services.ledger()
    if store is None or ledger is None:
        return _err("作息没有启动")
    row = store.get(str(id))
    if row is None or row["owner"] != ctx.owner:
        return _err(f"没有这个提醒：{id!r}（先用 reminder_list 查 id）")
    store.delete(row["id"])
    content = row["payload"].get("content", "")
    ledger.add("decision", owner=ctx.owner, session_id=ctx.session_id,
               title=f"取消了：{row['describe']}「{content}」", ref=row["id"])
    return {"ok": True, "id": row["id"], "cancelled": content}


# ---------- 待办 / 备忘 ----------

@register.tool(name="task_add", schema={
    "description": "把一件待办记进清单（对方让你\"记一下要做某事\"时用）。只是记录，不会到点提醒——要提醒用 reminder_add。",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "待办，一句话"},
            "body": {"type": "string", "description": "补充细节，可不填"},
        },
        "required": ["title"],
    },
})
def task_add(title: str, body: str = "") -> dict:
    ctx, error = _ctx_or_error()
    if error:
        return error
    ledger = services.ledger()
    if ledger is None:
        return _err("清单没有启动")
    title = str(title or "").strip()
    if not title:
        return _err("title 不能为空")
    e = ledger.add("task", owner=ctx.owner, session_id=ctx.session_id, title=title, body=str(body or ""))
    return {"ok": True, "id": e["id"], "title": title}


@register.tool(name="task_update", schema={
    "description": "改待办状态：做完了 done、不做了 cancelled、重新打开 open。id 从 task_list 拿。",
    "parameters": {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "status": {"type": "string", "enum": list(_TASK_STATUSES)},
        },
        "required": ["id", "status"],
    },
})
def task_update(id: str, status: str) -> dict:  # noqa: A002
    ctx, error = _ctx_or_error()
    if error:
        return error
    ledger = services.ledger()
    if ledger is None:
        return _err("清单没有启动")
    if status not in _TASK_STATUSES:
        return _err(f"status 只能是 {'/'.join(_TASK_STATUSES)}")
    current = ledger.get("task", str(id))
    if current is None or current.get("owner") != ctx.owner:
        return _err(f"没有这条待办：{id!r}（先用 task_list 查 id）")
    ledger.update("task", current["id"], status=status)
    return {"ok": True, "id": current["id"], "title": current.get("title"), "status": status}


@register.tool(name="task_list", schema={
    "description": "列出对方的待办（默认只列没做完的）。",
    "parameters": {
        "type": "object",
        "properties": {"status": {"type": "string", "enum": [*_TASK_STATUSES, "all"], "default": "open"}},
        "required": [],
    },
})
def task_list(status: str = "open") -> dict:
    ctx, error = _ctx_or_error()
    if error:
        return error
    ledger = services.ledger()
    if ledger is None:
        return _err("清单没有启动")
    wanted = None if status == "all" else (status if status in _TASK_STATUSES else "open")
    rows = ledger.list("task", owner=ctx.owner, status=wanted, limit=50)
    return {"ok": True, "tasks": [
        {"id": e["id"], "title": e.get("title"), "status": e.get("status"),
         "updated": _stamp(float(e.get("updated_at") or time.time()))}
        for e in rows
    ]}


@register.tool(name="note_add", schema={
    "description": "记一条备忘（对方让你记住的资料、约定、偏好等不需要\"做完\"的东西）。",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "一句话概括"},
            "body": {"type": "string", "description": "具体内容，可不填"},
        },
        "required": ["title"],
    },
})
def note_add(title: str, body: str = "") -> dict:
    ctx, error = _ctx_or_error()
    if error:
        return error
    ledger = services.ledger()
    if ledger is None:
        return _err("清单没有启动")
    title = str(title or "").strip()
    if not title:
        return _err("title 不能为空")
    e = ledger.add("note", owner=ctx.owner, session_id=ctx.session_id, title=title, body=str(body or ""))
    return {"ok": True, "id": e["id"], "title": title}
