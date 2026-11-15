"""测试：输入缓冲（src/core/inbox.py）与首条回复停顿（pacing.first_reply_delay）。"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from core.adapter import pacing  # noqa: E402
from core.adapter.base import Message  # noqa: E402
from core.inbox import Inbox, ReplyPacing, load_reply_pacing, merge_messages  # noqa: E402


def msg(content, *, owner="u1", sid="qq:private:1", **extra):
    return Message(id=f"m-{content}", platform="qq", session_id=sid, owner=owner,
                   direction="in", role="user", content=content, **extra)


class Recorder:
    def __init__(self, delay=0.0):
        self.batches, self.delay, self.active, self.peak = [], delay, 0, 0

    async def __call__(self, m):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(self.delay)
        self.batches.append(m.content)
        self.active -= 1


def fixed(wait, cap=10.0):
    return lambda: ReplyPacing(debounce_seconds=wait, debounce_max_seconds=cap)


# ---------- 合并 ----------


def test_merge_keeps_order_and_combines_lists():
    m = merge_messages([
        msg("在吗", images=["a.png"]),
        msg("我想问个事", mentions=["bot"], addressed=True),
        msg("明天天气怎么样", images=["b.png"], mentions=["bot"]),
    ])
    assert m.content == "在吗\n我想问个事\n明天天气怎么样"
    assert m.images == ["a.png", "b.png"]
    assert m.mentions == ["bot"]
    assert m.addressed is True
    assert m.id == "m-明天天气怎么样"  # 以最后一条为底


def test_merge_single_is_identity():
    one = msg("你好")
    assert merge_messages([one]) is one


# ---------- 缓冲 ----------


async def test_burst_is_answered_once_after_quiet():
    rec = Recorder()
    inbox = Inbox(rec, pacing=fixed(0.1))
    for t in ("在吗", "我想问个事", "明天天气怎么样"):
        await inbox.submit(msg(t))
        await asyncio.sleep(0.03)
    assert rec.batches == []  # 还在等他说完
    await asyncio.sleep(0.2)
    assert rec.batches == ["在吗\n我想问个事\n明天天气怎么样"]


async def test_new_message_restarts_the_timer():
    rec = Recorder()
    inbox = Inbox(rec, pacing=fixed(0.1))
    await inbox.submit(msg("a"))
    await asyncio.sleep(0.07)
    await inbox.submit(msg("b"))
    await asyncio.sleep(0.07)  # 距第一条 0.14 > 0.1，但距第二条才 0.07
    assert rec.batches == []
    await asyncio.sleep(0.1)
    assert rec.batches == ["a\nb"]


async def test_max_wait_caps_a_long_burst():
    rec = Recorder()
    inbox = Inbox(rec, pacing=fixed(0.1, cap=0.2))
    for i in range(10):  # 每 0.05 秒一条，一直不静默
        await inbox.submit(msg(str(i)))
        await asyncio.sleep(0.05)
    await inbox.drain()
    assert len(rec.batches) >= 2  # 封顶到了就先回一批
    assert "\n".join(rec.batches).split("\n") == [str(i) for i in range(10)]  # 不丢不乱


async def test_group_senders_are_buffered_separately_and_serially():
    rec = Recorder(delay=0.05)
    inbox = Inbox(rec, pacing=fixed(0.05))
    sid = "qq:group:9"
    await inbox.submit(msg("A1", owner="A", sid=sid))
    await inbox.submit(msg("B1", owner="B", sid=sid))
    await inbox.submit(msg("A2", owner="A", sid=sid))
    await inbox.drain()
    assert sorted(rec.batches) == ["A1\nA2", "B1"]
    assert rec.peak == 1  # 同一会话不并发：她正在回的时候，下一批排队


async def test_command_flushes_pending_then_runs_in_order():
    rec = Recorder()
    inbox = Inbox(rec, pacing=fixed(5))
    await inbox.submit(msg("等等"))
    await inbox.submit(msg("&newtale"))
    assert rec.batches == ["等等", "&newtale"]  # 不等 5 秒，也不颠倒


async def test_zero_window_is_passthrough():
    rec = Recorder()
    inbox = Inbox(rec, pacing=fixed(0))
    await inbox.submit(msg("a"))
    assert rec.batches == ["a"]  # submit 返回时已经处理完，与之前行为一致


async def test_handler_error_does_not_break_inbox():
    calls = []

    async def boom(m):
        calls.append(m.content)
        if m.content == "bad":
            raise RuntimeError("x")

    inbox = Inbox(boom, pacing=fixed(0))
    await inbox.submit(msg("bad"))
    await inbox.submit(msg("good"))
    assert calls == ["bad", "good"]


# ---------- 配置 ----------


def test_load_reply_pacing_reads_config_and_sanitizes(tmp_path, monkeypatch):
    from core.config import loader

    monkeypatch.setattr(loader.Config, "load", classmethod(lambda cls, d: {
        "reply": {"debounce_seconds": "3", "debounce_max_seconds": 1, "first_delay": "yes"}}))
    p = load_reply_pacing()
    assert p.debounce_seconds == 3.0
    assert p.debounce_max_seconds == 3.0  # 封顶不小于等待
    assert p.first_delay is True  # 非布尔回默认

    monkeypatch.setattr(loader.Config, "load", classmethod(lambda cls, d: {"reply": {"debounce_seconds": -1}}))
    assert load_reply_pacing().debounce_seconds == ReplyPacing().debounce_seconds


def test_defaults_match_field_specs():
    from core.config.default import DEFAULT_CONFIG
    from core.config.fields import fields_for

    specs = {s.key: s.default for s in fields_for("config") if s.key.startswith("reply.")}
    assert specs == {f"reply.{k}": v for k, v in DEFAULT_CONFIG["reply"].items()}


# ---------- 首条停顿 ----------


def test_first_reply_delay_subtracts_thinking_time(monkeypatch):
    monkeypatch.setattr(pacing, "JITTER_SEC", 0.0)
    full = pacing.typing_delay("你好呀朋友们")  # 6 字 → 1.32 秒
    assert abs(pacing.first_reply_delay("你好呀朋友们", 0.0) - full) < 1e-9
    assert abs(pacing.first_reply_delay("你好呀朋友们", 1.0) - (full - 1.0)) < 1e-9
    assert pacing.first_reply_delay("你好呀朋友们", 9.0) == 0.0  # 想得够久，不再等
