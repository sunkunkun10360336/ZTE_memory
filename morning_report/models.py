"""每日晨报的核心数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional, Tuple


@dataclass(frozen=True)
class ContextItem:
    """一条可以进入晨报的上下文。

    Attributes:
        title: 一句话标题，必填。
        source: 来源名称，例如“日历”“待办”。
        detail: 补充说明，可以为空。
        happened_at: 事件发生（或计划发生）的时间；未知时为空。
        priority: 优先级，数值越大越靠前，默认 0。
        tags: 标签，帮助模型归类。
        url: 相关链接，可以为空。
    """

    title: str
    source: str = ""
    detail: str = ""
    happened_at: Optional[datetime] = None
    priority: int = 0
    tags: Tuple[str, ...] = ()
    url: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("上下文标题不能为空")
        if not isinstance(self.priority, int):
            raise TypeError("上下文优先级必须是整数")
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "tags", tuple(self.tags))


@dataclass(frozen=True)
class TimeWindow:
    """晨报的上下文时间窗口 ``[start, end]``。"""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("时间窗口的 start 不能晚于 end")

    def contains(self, moment: datetime) -> bool:
        """判断某个时间是否落在窗口内；无时区信息时按窗口时区处理。"""
        normalized = moment
        if normalized.tzinfo is None and self.start.tzinfo is not None:
            normalized = normalized.replace(tzinfo=self.start.tzinfo)
        return self.start <= normalized <= self.end

    def __contains__(self, moment: object) -> bool:
        return isinstance(moment, datetime) and self.contains(moment)


@dataclass(frozen=True)
class ReportRun:
    """一次晨报运行的完整结果，便于测试、调试和调用方复用。"""

    report_date: date
    window: TimeWindow
    items: Tuple[ContextItem, ...]
    content: str
    delivered: Tuple[str, ...] = ()
    prompt: str = ""
    generated: bool = True
