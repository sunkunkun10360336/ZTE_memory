"""每日定时调度。"""

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


__all__ = ["DailyScheduler", "next_run_at", "parse_hhmm"]
