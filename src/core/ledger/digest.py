"""清单摘要（M2-06）：ChatLLM 每轮强制注入的"她手上有什么事"。

§七"三级注入"的第一级——**摘要强制注入**（最新几条 + 状态变化大的），不靠模型
自觉去查；要细节走工具（task_list / reminder_list，第二级）；到点主动说是第三级
（SpeechPump）。

纯代码拼装、不经 LLM（§19-19）。按 owner 隔离（硬规则 4）：只看当前说话人名下的。
条目文本是外部来源（用户口述的任务 / 提醒内容），由装配侧统一 escape。
"""

from __future__ import annotations

import time
from datetime import datetime

from core.ledger.store import Ledger

#: 各段条数上限。摘要是背景，不是清单本身——宁少勿多。
TASK_LIMIT = 8
UPCOMING_LIMIT = 5
DECISION_LIMIT = 5

#: "接下来"与"最近"的窗口
UPCOMING_WINDOW = 24 * 3600
RECENT_WINDOW = 24 * 3600

_DECISION_LABEL = {"sent": "已提醒", "dropped": "没发出", "missed": "错过了", "pending": "待发"}


def _when(t: float, now: float) -> str:
    d, n = datetime.fromtimestamp(t), datetime.fromtimestamp(now)
    if d.date() == n.date():
        return d.strftime("今天 %H:%M")
    if (d.date() - n.date()).days == 1:
        return d.strftime("明天 %H:%M")
    return d.strftime("%m-%d %H:%M")


class LedgerDigest:
    """把清单 + 日程压成几行文本。schedules 可缺省（只有清单时只出待办与决定）。"""

    def __init__(self, ledger: Ledger, schedules=None) -> None:
        self._ledger = ledger
        self._schedules = schedules

    def render(self, owner: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        sections: list[str] = []

        tasks = self._ledger.list("task", owner=owner, status="open", limit=TASK_LIMIT)
        if tasks:
            sections.append("待办：")
            sections.extend(f"- {t.get('title', '')}" for t in tasks)

        if self._schedules is not None:
            upcoming = [
                r for r in self._schedules.list(owner=owner, enabled=True)
                if float(r["next_fire"]) - now <= UPCOMING_WINDOW
            ][:UPCOMING_LIMIT]
            if upcoming:
                sections.append("接下来要主动找对方的：")
                sections.extend(
                    f"- {_when(float(r['next_fire']), now)} {r['payload'].get('content', '')}（{r['describe']}）"
                    for r in upcoming
                )

        decisions = [
            d for d in self._ledger.list("decision", owner=owner, limit=DECISION_LIMIT * 4)
            if now - float(d.get("updated_at") or 0) <= RECENT_WINDOW
        ][:DECISION_LIMIT]
        if decisions:
            sections.append("最近做过的决定：")
            for d in decisions:
                label = _DECISION_LABEL.get(str(d.get("status")), "")
                suffix = f"（{label}）" if label else ""
                sections.append(f"- {_when(float(d.get('updated_at') or now), now)} {d.get('title', '')}{suffix}")

        if not sections:
            return ""
        return "\n".join(["你手上的事（公共清单；细节用 task_list / reminder_list 查）：", *sections])
