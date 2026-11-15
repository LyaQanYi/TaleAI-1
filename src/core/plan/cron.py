"""时间表达式（M2-02）：schedules.cron 列里存的东西。

两种形态：
- **5 段 cron 子集**：`分 时 日 月 周`。每段支持 `*`、数字、`a-b`、`a,b`、`*/n`、
  `a-b/n`；周取 0–7（0 与 7 都是周日）。日与周**都**受限时按 cron 惯例取"或"。
- **`@at <epoch>`**：一次性，触发后由 ScheduleStore 置 enabled=0。

时区 = 本机本地时间（与 chat_env 注入的"当前时间"一致）。夏令时跳变时可能偏
一小时——单机自用可接受（见 M2 计划"风险"）。

为什么不引 croniter：与 time_query"不引 tzdata"同一取舍，这里要的子集很小。
模型**不直接写 cron**——工具面给 time / repeat（见 `from_friendly`），表达式由
代码生成，模型写错表达式这类失败从源头没有。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

#: 往后最多找几天。闰年 2/29 这种最坏情况也在 8 年内出现一次。
_SEARCH_DAYS = 366 * 8

_WEEKDAY_CN = "日一二三四五六"  # cron 周号 0=周日


class CronError(ValueError):
    """表达式不合法。"""


@dataclass(frozen=True)
class _Spec:
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # cron 周号，0=周日
    day_restricted: bool
    weekday_restricted: bool


def _parse_field(text: str, lo: int, hi: int, name: str) -> tuple[set[int], bool]:
    """解析一段，返回 (取值集合, 是否受限)。受限 = 不是以 * 开头的整段通配。"""
    values: set[int] = set()
    restricted = not text.startswith("*")
    for item in text.split(","):
        if not item:
            raise CronError(f"{name} 段有空项：{text!r}")
        step = 1
        if "/" in item:
            item, _, step_text = item.partition("/")
            if not step_text.isdigit() or int(step_text) <= 0:
                raise CronError(f"{name} 段步长不合法：{text!r}")
            step = int(step_text)
        if item == "*":
            start, end = lo, hi
        elif "-" in item:
            a, _, b = item.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"{name} 段范围不合法：{text!r}")
            start, end = int(a), int(b)
        elif item.isdigit():
            start = end = int(item)
            if step != 1:
                end = hi  # `5/15` 按 cron 惯例 = 从 5 起每 15
        else:
            raise CronError(f"{name} 段不认识：{text!r}")
        if not (lo <= start <= hi and lo <= end <= hi) or start > end:
            raise CronError(f"{name} 段超出范围 {lo}-{hi}：{text!r}")
        values.update(range(start, end + 1, step))
    return values, restricted


def _parse(expr: str) -> _Spec:
    parts = expr.split()
    if len(parts) != 5:
        raise CronError(f"cron 要 5 段（分 时 日 月 周），收到 {len(parts)} 段：{expr!r}")
    minutes, _ = _parse_field(parts[0], 0, 59, "分")
    hours, _ = _parse_field(parts[1], 0, 23, "时")
    days, day_r = _parse_field(parts[2], 1, 31, "日")
    months, _ = _parse_field(parts[3], 1, 12, "月")
    weekdays, wd_r = _parse_field(parts[4], 0, 7, "周")
    weekdays = {0 if w == 7 else w for w in weekdays}
    return _Spec(
        tuple(sorted(minutes)), tuple(sorted(hours)), frozenset(days), frozenset(months),
        frozenset(weekdays), day_r, wd_r,
    )


def _parse_at(expr: str) -> float:
    try:
        return float(expr.split(None, 1)[1])
    except (IndexError, ValueError):
        raise CronError(f"@at 后面要跟时间戳：{expr!r}") from None


def is_once(expr: str) -> bool:
    return expr.strip().startswith("@at")


def validate(expr: str) -> None:
    """不合法抛 CronError。"""
    expr = expr.strip()
    if is_once(expr):
        _parse_at(expr)
    else:
        _parse(expr)


def _day_matches(spec: _Spec, d: date) -> bool:
    if d.month not in spec.months:
        return False
    dom = d.day in spec.days
    dow = (d.weekday() + 1) % 7 in spec.weekdays
    if spec.day_restricted and spec.weekday_restricted:
        return dom or dow
    if spec.day_restricted:
        return dom
    if spec.weekday_restricted:
        return dow
    return True


def next_fire(expr: str, after: float) -> float | None:
    """严格晚于 after 的下一次触发时刻（epoch 秒）；不会再触发返回 None。"""
    expr = expr.strip()
    if is_once(expr):
        at = _parse_at(expr)
        return at if at > after else None
    spec = _parse(expr)
    start = datetime.fromtimestamp(after).replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.date()
    for offset in range(_SEARCH_DAYS):
        d = day + timedelta(days=offset)
        if not _day_matches(spec, d):
            continue
        first_day = offset == 0
        for h in spec.hours:
            if first_day and h < start.hour:
                continue
            for m in spec.minutes:
                if first_day and h == start.hour and m < start.minute:
                    continue
                return datetime(d.year, d.month, d.day, h, m).timestamp()
    return None


# ---------- 给工具用：友好参数 → 表达式 ----------

#: repeat 的合法取值（工具 schema 的 enum 也从这里取）
REPEATS = ("once", "daily", "weekdays", "weekly")


def _parse_hhmm(text: str) -> tuple[int, int]:
    try:
        h, m = (int(x) for x in str(text).strip().replace("：", ":").split(":"))
    except ValueError:
        raise CronError(f"时间要写成 HH:MM：{text!r}") from None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise CronError(f"时间超出范围：{text!r}")
    return h, m


def from_friendly(
    time_text: str,
    repeat: str = "once",
    date_text: str | None = None,
    weekday: int | None = None,
    now: float | None = None,
) -> str:
    """time="21:00" + repeat → 表达式。

    - once：给了 date（YYYY-MM-DD）用它；没给就取"下一个这个钟点"（今天过了就明天）
    - daily / weekdays（周一到周五）
    - weekly：weekday 取 1–7（周一=1 … 周日=7）；没给用今天是周几
    """
    now = time.time() if now is None else now
    h, m = _parse_hhmm(time_text)
    if repeat == "daily":
        return f"{m} {h} * * *"
    if repeat == "weekdays":
        return f"{m} {h} * * 1-5"
    if repeat == "weekly":
        wd = weekday if weekday is not None else datetime.fromtimestamp(now).isoweekday()
        if not isinstance(wd, int) or not 1 <= wd <= 7:
            raise CronError(f"weekday 要 1–7（周一=1）：{weekday!r}")
        return f"{m} {h} * * {wd % 7}"
    if repeat != "once":
        raise CronError(f"repeat 只能是 {'/'.join(REPEATS)}：{repeat!r}")
    if date_text:
        try:
            d = datetime.strptime(str(date_text).strip(), "%Y-%m-%d")
        except ValueError:
            raise CronError(f"日期要写成 YYYY-MM-DD：{date_text!r}") from None
        at = d.replace(hour=h, minute=m).timestamp()
        if at <= now:
            raise CronError(f"{date_text} {h:02d}:{m:02d} 已经过去了")
        return f"@at {at:.0f}"
    base = datetime.fromtimestamp(now)
    target = base.replace(hour=h, minute=m, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += timedelta(days=1)
    return f"@at {target.timestamp():.0f}"


def describe(expr: str) -> str:
    """给人看的说法：每天 21:00 / 工作日 08:00 / 每周三 21:00 / 11-16 21:00（一次）。"""
    expr = expr.strip()
    try:
        if is_once(expr):
            return datetime.fromtimestamp(_parse_at(expr)).strftime("%Y-%m-%d %H:%M（一次）")
        mi, ho, dom, mon, dow = expr.split()
        if mi.isdigit() and ho.isdigit() and dom == "*" and mon == "*":
            hhmm = f"{int(ho):02d}:{int(mi):02d}"
            if dow == "*":
                return f"每天 {hhmm}"
            if dow == "1-5":
                return f"工作日 {hhmm}"
            if dow.isdigit() and 0 <= int(dow) <= 7:
                return f"每周{_WEEKDAY_CN[int(dow) % 7]} {hhmm}"
    except (ValueError, CronError):
        pass
    return f"cron {expr}"
