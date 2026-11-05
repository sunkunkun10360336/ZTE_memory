"""定时调度：每天固定时刻，或按固定分钟间隔。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


def parse_hhmm(value: str) -> dt_time:
    """解析 ``HH:MM`` 格式的每日执行时间。"""
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式必须是 HH:MM，当前为 {value!r}")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"时间格式必须是 HH:MM，当前为 {value!r}") from exc
    return dt_time(hour, minute)


def next_run_at(now: datetime, at: dt_time, tz: ZoneInfo) -> datetime:
    """返回 ``now`` 之后（严格大于）的下一个 ``at`` 时刻。"""
    local = now.astimezone(tz) if now.tzinfo is not None else now.replace(tzinfo=tz)
    candidate = datetime.combine(local.date(), at, tzinfo=tz)
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate


@dataclass
class DailyScheduler:
    """每天在固定时刻执行任务的长驻调度器。

    任务抛出的异常会被记录并忽略，第二天继续运行；需要 cron 托管时可直接
    使用 ``python -m morning_report --once`` 配合系统定时器。
    """

    at: dt_time
    tz: ZoneInfo

    def next_run(self, now: Optional[datetime] = None) -> datetime:
        moment = now if now is not None else datetime.now(self.tz)
        return next_run_at(moment, self.at, self.tz)

    def run_forever(
        self,
        task: Callable[[datetime], None],
        *,
        sleep: Callable[[float], None] = time.sleep,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        while True:
            now = now_fn() if now_fn is not None else datetime.now(self.tz)
            target = next_run_at(now, self.at, self.tz)
            delay = (target - now.astimezone(self.tz)).total_seconds()
            log.info("下一次晨报时间：%s（等待 %.0f 秒）", target.isoformat(), delay)
            if delay > 0:
                sleep(delay)
            try:
                task(target)
            except Exception:  # noqa: BLE001 - 单次失败不应终止每日调度
                log.exception("本次晨报任务执行失败，将在下一个周期重试")


@dataclass
class IntervalScheduler:
    """每隔 ``minutes`` 分钟执行一次任务的长驻调度器。

    生产环境更推荐 ``--once`` + cron / systemd timer；这个调度器适合在
    没有系统定时器的环境里直接常驻。任务抛出的异常会被记录并忽略。
    """

    minutes: int

    def __post_init__(self) -> None:
        if isinstance(self.minutes, bool) or not isinstance(self.minutes, int):
            raise ValueError("间隔必须是整数分钟")
        if self.minutes < 1:
            raise ValueError("间隔不能小于 1 分钟")

    def run_forever(
        self,
        task: Callable[[datetime], None],
        *,
        sleep: Callable[[float], None] = time.sleep,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        interval = self.minutes * 60.0
        while True:
            started = now_fn() if now_fn is not None else datetime.now()
            try:
                task(started)
            except Exception:  # noqa: BLE001 - 单次失败不应终止调度
                log.exception("本次晨报任务执行失败，将在下一个间隔重试")
            finished = now_fn() if now_fn is not None else datetime.now()
            elapsed = (finished - started).total_seconds()
            delay = max(0.0, interval - elapsed)
            if delay > 0:
                log.info("下一次晨报在 %.0f 秒后", delay)
                sleep(delay)


__all__ = [
    "DailyScheduler",
    "IntervalScheduler",
    "next_run_at",
    "parse_hhmm",
]
