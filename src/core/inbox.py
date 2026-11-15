"""输入缓冲：同一个人在同一会话里连发几条时，等他说完再一起回。

真人聊天常把一句话拆成几条发：「在吗」「我想问个事」「明天天气怎么样」。
来一条回一条，她会先对「在吗」答一句，再对后面每条各答一句，又快又碎。
这里在 `handle_message` 前面加一层按 **(会话, 发言者)** 的缓冲：

- 收到一条先不处理，等 `debounce_seconds` 秒；期间同一个人又来一条就重新计时。
- 从这一批第一条算起最多等 `debounce_max_seconds` 秒，一直有话也会开口。
- 静默到点后把这一批按顺序合成一条消息（正文换行拼接，图片 / 提及合并），
  交给 `handle_message` 走原来的完整处理链（唤醒、落库、装配、回复）。
- 同一会话的处理**串行**：她正在回上一批时，新的一批排在后面，不打断。
- 命令（`&` 开头）不缓冲：先把这个人已经攒着的话处理掉，再处理命令。
- `debounce_seconds` 为 0 = 关闭缓冲，行为与之前一致（来一条处理一条）。

群聊按发言者分开缓冲：A 和 B 交替说话不会被拼成一条。
配置每条消息现读（热重载）：面板里改完下一条就生效。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from core.adapter.base import Message

logger = logging.getLogger(__name__)

Handler = Callable[[Message], Awaitable[object]]


@dataclass(frozen=True)
class ReplyPacing:
    debounce_seconds: float = 2.5
    debounce_max_seconds: float = 10.0
    first_delay: bool = True


def _num(v: object, default: float) -> float:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return f if f >= 0 else default


def load_reply_pacing() -> ReplyPacing:
    """从 config 域读回复节奏，缺失 / 出错回默认（读配置失败不能让她不回话）。"""
    from core.config.loader import Config

    try:
        cfg = Config.load("config").get("reply", {}) or {}
    except Exception:
        return ReplyPacing()
    d = ReplyPacing()
    wait = _num(cfg.get("debounce_seconds", d.debounce_seconds), d.debounce_seconds)
    cap = _num(cfg.get("debounce_max_seconds", d.debounce_max_seconds), d.debounce_max_seconds)
    first = cfg.get("first_delay", d.first_delay)
    return ReplyPacing(debounce_seconds=wait, debounce_max_seconds=max(cap, wait),
                       first_delay=first if isinstance(first, bool) else d.first_delay)


def merge_messages(batch: list[Message]) -> Message:
    """把一批消息按顺序合成一条：以最后一条为底，正文换行拼接，列表字段合并。"""
    if len(batch) == 1:
        return batch[0]
    last = batch[-1]
    flags = [m.addressed for m in batch]
    addressed = True if any(f is True for f in flags) else (None if any(f is None for f in flags) else False)
    mentions: list[str] = []
    for m in batch:
        mentions.extend(x for x in m.mentions if x not in mentions)
    return dataclasses.replace(
        last,
        content="\n".join(m.content for m in batch if m.content),
        images=[i for m in batch for i in (m.images or [])],
        quoted_images=[i for m in batch for i in (m.quoted_images or [])],
        quoted=next((m.quoted for m in batch if m.quoted), ""),
        reply_to=next((m.reply_to for m in batch if m.reply_to), None),
        mentions=mentions,
        addressed=addressed,
    )


class Inbox:
    """按 (会话, 发言者) 缓冲输入，按会话串行交给 handler。"""

    def __init__(self, handler: Handler, pacing: Callable[[], ReplyPacing] | None = None) -> None:
        self._handler = handler
        # 不传就每条现读配置（按名字查找，测试可以替换模块里的 load_reply_pacing）
        self._pacing = pacing or (lambda: load_reply_pacing())
        self._buf: dict[tuple[str, str], list[Message]] = {}
        self._started: dict[tuple[str, str], float] = {}
        self._timers: dict[tuple[str, str], asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    async def submit(self, message: Message) -> None:
        cfg = self._pacing()
        key = (message.session_id, message.owner)
        if cfg.debounce_seconds <= 0 or message.content.lstrip().startswith("&"):
            # 关闭缓冲或命令：先处理这个人攒着的话，再在调用方里同步处理这一条
            pending = self._take(key)
            if pending:
                await self._run(key[0], pending)
            await self._run(key[0], [message])
            return
        now = time.monotonic()
        self._buf.setdefault(key, []).append(message)
        self._started.setdefault(key, now)
        timer = self._timers.pop(key, None)
        if timer is not None:
            timer.cancel()
        wait = min(cfg.debounce_seconds, max(0.0, self._started[key] + cfg.debounce_max_seconds - now))
        self._timers[key] = self._spawn(self._flush_later(key, wait))

    def _take(self, key: tuple[str, str]) -> list[Message]:
        timer = self._timers.pop(key, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        self._started.pop(key, None)
        return self._buf.pop(key, [])

    async def _flush_later(self, key: tuple[str, str], wait: float) -> None:
        await asyncio.sleep(wait)
        batch = self._take(key)
        if batch:
            await self._run(key[0], batch)

    async def _run(self, session_id: str, batch: list[Message]) -> None:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            try:
                await self._handler(merge_messages(batch))
            except Exception:
                logger.exception("处理消息时发生未预期的错误，已跳过这一批：session=%s", session_id)

    def _spawn(self, coro: Awaitable[None]) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        """立即处理所有攒着的消息并等它们处理完（关停 / 测试用）。"""
        for key in list(self._buf):
            batch = self._take(key)
            if batch:
                self._spawn(self._run(key[0], batch))
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
