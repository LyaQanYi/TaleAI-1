"""http_guard：Host 白名单判定、监听地址判定、被拒请求的限流日志。"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from core.adapter.http_guard import LOCAL_HOSTS, RejectLog, host_allowed, is_loopback


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8321", "localhost:8321", "LOCALHOST", "[::1]:8321"])
def test_local_names_allowed(host):
    assert host_allowed(host, LOCAL_HOSTS)


@pytest.mark.parametrize("host", ["", "evil.example", "evil.example:8321", "127.0.0.1.evil.example", "localhost.", "[::1"])
def test_other_names_rejected(host):
    assert not host_allowed(host, LOCAL_HOSTS)


@pytest.mark.parametrize("host,expect", [("127.0.0.1", True), ("127.0.0.2", True), ("localhost", True), ("::1", True),
                                         ("[::1]", True), ("0.0.0.0", False), ("192.168.1.5", False), ("", False)])
def test_is_loopback(host, expect):
    assert is_loopback(host) is expect


def test_reject_log_is_rate_limited(caplog):
    """同一来源一个窗口只记一条；下个窗口再记时带上中间压掉的次数。"""
    now = [1000.0]
    log = RejectLog("测试请求", window=60, clock=lambda: now[0])
    with caplog.at_level(logging.WARNING, logger="core.adapter.http_guard"):
        for _ in range(50):
            log.note("evil.example", "/api/overview")
        assert len(caplog.records) == 1
        now[0] += 61
        log.note("evil.example", "/api/overview")
    assert len(caplog.records) == 2
    assert "又拒绝了 49 次" in caplog.records[1].getMessage()


def test_reject_log_counts_hosts_separately(caplog):
    log = RejectLog("测试请求", clock=lambda: 0.0)
    with caplog.at_level(logging.WARNING, logger="core.adapter.http_guard"):
        log.note("a.example")
        log.note("b.example")
        log.note("a.example")
    assert len(caplog.records) == 2


def test_reject_log_does_not_grow_without_bound():
    now = [0.0]
    log = RejectLog("测试请求", window=60, clock=lambda: now[0])
    for i in range(300):
        log.note(f"h{i}.example")
    now[0] += 120
    log.note("fresh.example")
    assert len(log._seen) <= 257
