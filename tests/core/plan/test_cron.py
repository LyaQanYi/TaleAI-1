"""时间表达式（M2-02）：cron 子集解析、next_fire、友好参数生成、描述。"""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "src"))

from core.plan import cron  # noqa: E402


def ts(*args):
    return datetime(*args).timestamp()


def dt(t):
    return datetime.fromtimestamp(t)


def test_daily_same_day_and_next_day():
    assert dt(cron.next_fire("0 21 * * *", ts(2026, 11, 15, 20, 59))) == datetime(2026, 11, 15, 21, 0)
    # 严格晚于：正好 21:00 时下一次是明天
    assert dt(cron.next_fire("0 21 * * *", ts(2026, 11, 15, 21, 0))) == datetime(2026, 11, 16, 21, 0)
    assert dt(cron.next_fire("0 21 * * *", ts(2026, 11, 15, 21, 0, 30))) == datetime(2026, 11, 16, 21, 0)


def test_month_and_year_rollover():
    assert dt(cron.next_fire("30 8 1 * *", ts(2026, 12, 15))) == datetime(2027, 1, 1, 8, 30)
    assert dt(cron.next_fire("0 0 29 2 *", ts(2026, 3, 1))) == datetime(2028, 2, 29, 0, 0)


def test_weekdays_and_sunday_alias():
    # 2026-11-14 是周六 → 下一个工作日是 11-16 周一
    assert dt(cron.next_fire("0 8 * * 1-5", ts(2026, 11, 14, 9))) == datetime(2026, 11, 16, 8, 0)
    assert cron.next_fire("0 8 * * 0", ts(2026, 11, 14)) == cron.next_fire("0 8 * * 7", ts(2026, 11, 14))
    assert dt(cron.next_fire("0 8 * * 0", ts(2026, 11, 14))) == datetime(2026, 11, 15, 8, 0)


def test_dom_or_dow_when_both_restricted():
    # 13 号或周五：2026-11-13 是周五也是 13 号；之后最近的是 11-20 周五
    assert dt(cron.next_fire("0 9 13 * 5", ts(2026, 11, 13, 10))) == datetime(2026, 11, 20, 9, 0)


def test_steps_lists_ranges():
    assert dt(cron.next_fire("*/15 * * * *", ts(2026, 1, 1, 10, 16))) == datetime(2026, 1, 1, 10, 30)
    assert dt(cron.next_fire("5,35 9-10 * * *", ts(2026, 1, 1, 9, 40))) == datetime(2026, 1, 1, 10, 5)
    assert dt(cron.next_fire("5/20 * * * *", ts(2026, 1, 1, 10, 6))) == datetime(2026, 1, 1, 10, 25)


@pytest.mark.parametrize("bad", ["", "* * * *", "60 * * * *", "* 24 * * *", "a * * * *",
                                 "*/0 * * * *", "5-1 * * * *", "1,,2 * * * *", "@at", "@at x"])
def test_invalid_expressions(bad):
    with pytest.raises(cron.CronError):
        cron.validate(bad)


def test_at_once():
    at = ts(2026, 11, 16, 21, 0)
    assert cron.next_fire(f"@at {at:.0f}", at - 1) == at
    assert cron.next_fire(f"@at {at:.0f}", at) is None
    assert cron.is_once(f"@at {at:.0f}")


def test_from_friendly():
    now = ts(2026, 11, 15, 22, 0)  # 周日 22:00
    assert cron.from_friendly("21:00", "daily", now=now) == "0 21 * * *"
    assert cron.from_friendly("8:05", "weekdays", now=now) == "5 8 * * 1-5"
    assert cron.from_friendly("21:00", "weekly", weekday=3, now=now) == "0 21 * * 3"
    assert cron.from_friendly("21:00", "weekly", weekday=7, now=now) == "0 21 * * 0"
    assert cron.from_friendly("21:00", "weekly", now=now) == "0 21 * * 0"  # 今天周日
    # once 没给日期：今天 21:00 已过 → 明天
    expr = cron.from_friendly("21：00", "once", now=now)
    assert dt(cron.next_fire(expr, now)) == datetime(2026, 11, 16, 21, 0)
    expr = cron.from_friendly("23:30", "once", now=now)
    assert dt(cron.next_fire(expr, now)) == datetime(2026, 11, 15, 23, 30)
    expr = cron.from_friendly("07:00", "once", date_text="2026-12-01", now=now)
    assert dt(cron.next_fire(expr, now)) == datetime(2026, 12, 1, 7, 0)


@pytest.mark.parametrize("kwargs", [
    {"time_text": "25:00"}, {"time_text": "9点"}, {"time_text": "9:00", "repeat": "hourly"},
    {"time_text": "9:00", "repeat": "weekly", "weekday": 8},
    {"time_text": "9:00", "date_text": "2026/12/01"},
    {"time_text": "9:00", "date_text": "2026-11-01"},  # 已过去
])
def test_from_friendly_errors(kwargs):
    with pytest.raises(cron.CronError):
        cron.from_friendly(now=ts(2026, 11, 15, 22, 0), **kwargs)


def test_describe():
    assert cron.describe("0 21 * * *") == "每天 21:00"
    assert cron.describe("5 8 * * 1-5") == "工作日 08:05"
    assert cron.describe("0 21 * * 3") == "每周三 21:00"
    assert cron.describe("0 21 * * 0") == "每周日 21:00"
    assert cron.describe(f"@at {ts(2026, 11, 16, 21, 0):.0f}") == "2026-11-16 21:00（一次）"
    assert cron.describe("*/5 * * * *") == "cron */5 * * * *"
