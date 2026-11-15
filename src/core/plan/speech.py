"""SpeechPump：取 pending 的发言 decision，借 ChatLLM 的嘴主动开口（M2-04）。

§18.4 消费端表第二行：门铃 = `plan.fired`（speech_queue），任务本体 = decisions.jsonl
里 `status=pending` 的条目；发完改 `status=sent`。队列丢了 / 进程重启 → tick 扫
pending 补上，**一次只取一条、串行**（§19-6"同一人格同一时刻仅一条主动消息"）。

**前台优先**（§19-6 / 验收②）：
- 会话"正在聊"（Inbox 里有话没回完，或最后活跃在 `plan.busy_seconds` 之内）→
  这条留 pending，下一轮再看；
- 排队（从 decision 写下起算）超过 `plan.queue_timeout_minutes` → `status=dropped`
  + 日志。从写下起算而不是从计划时刻起算：停机补发（宽限内）的那条不该一出生
  就超时。
- 生成完再判一次：生成期间用户开口了，就把已生成的话存进 decision 的 `draft`，
  继续排队，下次直接发，不重新生成。

**送达顺序**：先落库（assistant 一行）→ 再发给适配器 → 再记 `message.sent` 事件
→ 最后把 decision 改成 sent。页面没开着时 send 只记日志，下次打开历史里有。

主动消息**只落 assistant 一行、不伪造 user 行**：窗口按"条"切（ChatLLM._recent_history
不假设 1 回合 = 2 行），网页回放按行渲染，都不受影响。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Callable

from core.adapter.base import Reply
from core.adapter.registry import AdapterRegistry
from core.event_bus import Event, EventBus
from core.ledger import Ledger
from core.memory import EventLog, record_event
from core.plan.scheduler import plan_config
from core.session.store import SessionStore

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

DEFAULT_TICK_SECONDS = 10.0
_DOORBELL_MAXSIZE = 100


class SpeechPump:
    """常驻 asyncio 任务：串行处理 pending 的发言 decision。"""

    def __init__(
        self,
        ledger: Ledger,
        store: SessionStore,
        bot,
        registry: AdapterRegistry,
        bus: EventBus | None = None,
        eventlog: EventLog | None = None,
        is_busy: Callable[[str], bool] | None = None,
        config_reader: Callable[[], dict] | None = None,
        clock: Callable[[], float] | None = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        self._ledger = ledger
        self._store = store
        self._bot = bot
        self._registry = registry
        self._bus = bus if bus is not None else EventBus()
        self._eventlog = eventlog
        self._is_busy = is_busy or (lambda _sid: False)
        self._config_reader = config_reader or plan_config
        self._clock = clock or time.time
        self.tick_seconds = tick_seconds
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=_DOORBELL_MAXSIZE)
        if bus is not None:
            bus.subscribe("plan.fired", self._on_fired)

    # ---------- 门铃（总线 handler：只许 put_nowait / 记日志） ----------

    def _on_fired(self, event: Event) -> None:
        try:
            self._queue.put_nowait(str(event.data.get("decision_id") or ""))
        except asyncio.QueueFull:
            logger.debug("speech 门铃已满，丢一次唤醒（tick 会补）")

    # ---------- 常驻循环 ----------

    async def run(self) -> None:
        logger.info("SpeechPump 启动：tick=%.0fs", self.tick_seconds)
        while True:
            try:
                await asyncio.wait_for(self._queue.get(), timeout=self.tick_seconds)
            except asyncio.TimeoutError:
                pass
            while not self._queue.empty():
                self._queue.get_nowait()
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("SpeechPump tick 失败（下一轮重试）")

    async def tick(self) -> list[dict]:
        """处理一遍 pending（旧的先），返回本轮状态有变化的 decision。"""
        cfg = self._config_reader() or {}
        timeout = float(cfg.get("queue_timeout_minutes", 5)) * 60
        busy_seconds = float(cfg.get("busy_seconds", 60))
        pending = self._ledger.list("decision", status="pending")
        pending.sort(key=lambda d: float(d.get("created_at") or d.get("updated_at") or 0))

        changed: list[dict] = []
        for decision in pending:
            now = self._clock()
            queued_at = float(decision.get("created_at") or decision.get("updated_at") or now)
            if now - queued_at > timeout:
                logger.warning("主动消息排队超过 %.0f 分钟，丢弃：%s（%s）",
                               timeout / 60, decision["id"], decision.get("trigger"))
                done = self._ledger.update("decision", decision["id"], status="dropped",
                                           reason="queue_timeout")
                if done:
                    changed.append(done)
                continue
            session_id = str(decision.get("session_id") or "")
            if not session_id:
                done = self._ledger.update("decision", decision["id"], status="dropped",
                                           reason="no_session")
                if done:
                    changed.append(done)
                continue
            if self._busy(session_id, now, busy_seconds):
                continue  # 正在聊，下一轮再看
            done = await self._speak(decision, busy_seconds)
            if done is not None:
                changed.append(done)
        return changed

    # ---------- 内部 ----------

    def _session_row(self, session_id: str) -> dict | None:
        return next((r for r in self._store.sessions() if r.get("id") == session_id), None)

    def _busy(self, session_id: str, now: float, busy_seconds: float) -> bool:
        try:
            if self._is_busy(session_id):
                return True
        except Exception:
            logger.exception("忙判定回调出错，按不忙处理：%s", session_id)
        row = self._session_row(session_id)
        return row is not None and now - float(row.get("last_active") or 0) < busy_seconds

    async def _speak(self, decision: dict, busy_seconds: float) -> dict | None:
        session_id = str(decision["session_id"])
        row = self._session_row(session_id) or {}
        platform = str(decision.get("platform") or row.get("platform") or session_id.split(":", 1)[0])
        owner = str(decision.get("owner") or row.get("owner") or "")
        kind = str(row.get("kind") or "private")

        messages = [str(m) for m in (decision.get("draft") or []) if str(m).strip()]
        if not messages:
            history = self._store.history(session_id, with_parts=True, with_media=True)
            fire_at = float(decision.get("fire_at") or 0)
            when = (f"约定在 {datetime.fromtimestamp(fire_at).strftime('%m-%d %H:%M')}"
                    if fire_at else "")
            reply = await self._bot.speak(
                str(decision.get("trigger") or decision.get("title") or ""), history, session_id,
                session_type=kind, owner=owner, when=when,
            )
            messages = [m for m in reply.messages if m.strip()]
            if not messages:
                logger.warning("主动发言没生成出内容（%s），丢弃：%s", reply.stop_reason, decision["id"])
                return self._ledger.update("decision", decision["id"], status="dropped",
                                           reason=reply.stop_reason or "empty")
            if self._busy(session_id, self._clock(), busy_seconds):
                # 生成期间用户开口了：存稿继续排队，下次直接发
                self._ledger.update("decision", decision["id"], draft=messages)
                return None

        self._store.ensure_session(session_id, platform=platform, owner=owner or "local", kind=kind)
        try:
            self._store.append(session_id, "assistant", "\n\n".join(messages), parts=messages)
        except Exception:
            logger.exception("主动消息落库失败，丢弃：%s", decision["id"])
            return self._ledger.update("decision", decision["id"], status="dropped", reason="persist_failed")

        adapter = self._registry.lookup(platform)
        if adapter is None:
            logger.warning("没有平台 %r 的适配器，主动消息只落库不推送：%s", platform, session_id)
        else:
            try:
                await adapter.send(Reply(session_id=session_id, messages=messages, stop_reason="proactive"))
            except Exception:
                logger.exception("推送主动消息失败（已落库）：%s", session_id)

        data = {"messages": messages, "stop_reason": "proactive"}
        if self._eventlog is not None:
            record_event(bus=self._bus, eventlog=self._eventlog, event_type="message.sent",
                         session_id=session_id, owner=owner, data=data)
        else:
            self._bus.publish("message.sent", session_id=session_id, owner=owner, **data)

        logger.info("主动发言已发出：%s → %s", decision["id"], session_id)
        return self._ledger.update("decision", decision["id"], status="sent",
                                   sent_at=self._clock(), messages=messages)
