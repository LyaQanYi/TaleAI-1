"""作息（plan_llm 的纯代码部分，§八 / §18.4）：日程存储、定时领取、主动发言。

M2 产出：
  - cron.py:      时间表达式（5 段 cron 子集 + @at 一次性）
  - schedule.py:  ScheduleStore（SQLite schedules 表）
  - scheduler.py: PlanScheduler（到点 → 写 decision → plan.fired 门铃）
  - speech.py:    SpeechPump（取 pending decision → 借 ChatLLM 的嘴发言）
"""
