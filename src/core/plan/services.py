"""作息服务的进程内登记点（M2-05）：内置 plan 插件从这里拿 ScheduleStore / Ledger。

插件在 import 时由注册表加载，没有构造参数可传；main 启动时 `configure()` 一次，
插件在**调用时**取（不是 import 时），所以加载顺序无所谓。没配置（CLI / 单测
没起作息）→ 工具返回"作息没启动"，不炸。
"""

from __future__ import annotations

from core.ledger import Ledger
from core.plan.schedule import ScheduleStore

_schedules: ScheduleStore | None = None
_ledger: Ledger | None = None


def configure(schedules: ScheduleStore | None, ledger: Ledger | None) -> None:
    global _schedules, _ledger
    _schedules, _ledger = schedules, ledger


def schedules() -> ScheduleStore | None:
    return _schedules


def ledger() -> Ledger | None:
    return _ledger
