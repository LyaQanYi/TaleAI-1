"""测试共用夹具。"""

import pytest


@pytest.fixture
def no_reply_pacing(monkeypatch):
    """关掉输入缓冲和首条停顿：经过 serve_forever 的消息立即处理（节奏本身见 test_inbox.py）。"""
    import core.inbox

    no_pace = lambda: core.inbox.ReplyPacing(debounce_seconds=0, first_delay=False)  # noqa: E731
    monkeypatch.setattr(core.inbox, "load_reply_pacing", no_pace)
    import main

    monkeypatch.setattr(main, "load_reply_pacing", no_pace)
