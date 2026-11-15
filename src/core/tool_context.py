"""工具调用的会话上下文（M2-05）：这次工具调用是"谁、在哪条会话里"发起的。

为什么需要：ToolCall 的形状 §18.3 固定（call_id / name / arguments），执行器的
签名也不该为某个插件改；但"设提醒"这类工具必须知道 owner 和会话，否则到点
不知道发给谁、清单也没法按人隔离。

做法：ChatLLM 执行工具时用 `tool_context(...)` 包一层（contextvars），插件里
`current_tool_context()` 读。contextvars 天然按协程隔离——两个会话同时在跑
FC 循环也不会串。

⚠️ 信任模型（§19-11）：外部插件同样读得到这份上下文（owner / 会话 id）。这和
"插件代码本身不受约束"是同一条边界，不额外承诺什么。
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class ToolContext:
    session_id: str = ""
    owner: str = ""
    platform: str = ""
    session_type: str = ""


_current: ContextVar[ToolContext | None] = ContextVar("taleai_tool_context", default=None)


def current_tool_context() -> ToolContext | None:
    """当前工具调用的上下文；不在 ChatLLM 的工具调用里（CLI 直调 / 单测）返回 None。"""
    return _current.get()


@contextmanager
def tool_context(ctx: ToolContext | None) -> Iterator[None]:
    token = _current.set(ctx)
    try:
        yield
    finally:
        _current.reset(token)
