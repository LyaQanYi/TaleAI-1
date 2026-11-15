"""PlanScheduler：到点领取日程 → 写 decision → 叫醒发言队列（M2-03）。

§18.4 消费端表的第三行：**没有队列，自己定时轮询** SQLite schedules，取
`next_fire ≤ now`，事务领取 → 写下一次。这里是纯代码，不调任何模型——
"到点说什么"交给 SpeechPump 借 ChatLLM 的嘴（§八），"该不该说"的决策 LLM
（planner）M2 不做（见 M2 计划拍板点 1）。

**先写 decision、后推进 next_fire**——这是"重启后不丢、不重复"（验收③）的关键：
- decision 的 id 是确定性的 `d_<日程 id>_<触发时刻>`，`add_if_absent` 写；
- 写完再 CAS 推进 next_fire。
反过来（先推进再写）的话，"推进成功、写之前崩溃"这次提醒就永久丢了。现在的
顺序下：崩在写之后、推进之前 → 重启再领一次，同 id 不会写第二条；崩在推进
之后 → decision 早已落盘，SpeechPump 重启扫 pending 照样发。至少一次 × 幂等 =
恰好一次。

**错过太久不补**：停机跨过触发点超过 `plan.misfire_grace_minutes` 的，记一条
`status=missed` 的 decision（面板看得到"错过了"），不发言——半夜开机突然收到
一句"该睡觉了"比不说更怪。周期日程停机多个周期也只处理最近一次（claim 往后跳）。

总线：领到且要发言时 publish `plan.fired`（§18.2 事件目录第 6 个，生产者 PlanLLM，
订阅者发言队列）。门铃丢了无所谓——SpeechPump 自己也按 tick 扫 decisions。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from core.config.loader import Config
from core.event_bus import EventBus
from core.ledger import Ledger
from core.plan.schedule import ScheduleStore

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

#: 多久扫一次。cron 粒度是分钟，30 秒能保证不晚于触发点半分钟。
DEFAULT_TICK_SECONDS = 30.0


def plan_config() -> dict:
    """现读 plan 配置域（热重载：改完下一次 tick 生效）。读坏返回空 = 全按默认。"""
    try:
        return dict(Config.load("config").get("plan", {}) or {})
    except Exception:
        logger.exception("读 plan 配置失败，按默认值")
        return {}


def decision_id(schedule_id: str, fire_at: float) -> str:
    """同一日程同一触发点 → 同一个 decision id（去重的根）。"""
    return f"d_{schedule_id}_{int(fire_at)}"


class PlanScheduler:
    """常驻 asyncio 任务：每 tick 领一遍到点日程。"""

    def __init__(
        self,
        schedules: ScheduleStore,
        ledger: Ledger,
        bus: EventBus | None = None,
        config_reader: Callable[[], dict] | None = None,
        clock: Callable[[], float] | None = None,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
    ) -> None:
        self._schedules = schedules
        self._ledger = ledger
        self._bus = bus
        self._config_reader = config_reader or plan_config
        self._clock = clock or time.time
        self.tick_seconds = tick_seconds

    async def run(self) -> None:
        logger.info("PlanScheduler 启动：tick=%.0fs", self.tick_seconds)
        while True:
            try:
                self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("PlanScheduler tick 失败（下一轮重试）")
            await asyncio.sleep(self.tick_seconds)

    def tick(self) -> list[dict]:
        """领一遍到点日程，返回本轮新写下的 decision（测试与日志用）。"""
        cfg = self._config_reader() or {}
        if not cfg.get("enabled", True):
            return []
        grace = float(cfg.get("misfire_grace_minutes", 10)) * 60
        now = self._clock()
        written: list[dict] = []

        for row in self._schedules.due(now):
            fire_at = float(row["next_fire"])
            payload = row.get("payload") or {}
            content = str(payload.get("content") or "").strip() or "（没写内容的提醒）"
            status = "missed" if now - fire_at > grace else "pending"

            entry = self._ledger.add_if_absent(
                "decision",
                decision_id(row["id"], fire_at),
                owner=row.get("owner") or "",
                title=f"到点提醒：{content}",
                body=row.get("describe") or "",
                status=status,
                session_id=row.get("session_id") or "",
                platform=row.get("platform") or "",
                ref=row["id"],
                fire_at=fire_at,
                trigger=content,
            )
            if not self._schedules.claim(row["id"], fire_at, now):
                continue  # 别处已经领走（decision 同 id，写不出第二条）
            if entry is not None:
                written.append(entry)
                if status == "missed":
                    logger.warning("日程 %s 错过了 %.0f 秒（超过宽限），不补发：%s",
                                   row["id"], now - fire_at, content)
                else:
                    logger.info("日程到点：%s → %s（%s）", row["id"], row.get("session_id"), content)
            if status == "pending" and self._bus is not None:
                self._bus.publish(
                    "plan.fired",
                    decision_id=decision_id(row["id"], fire_at),
                    schedule_id=row["id"],
                    session_id=row.get("session_id") or "",
                    owner=row.get("owner") or "",
                    content=content,
                )
        return written
