"""晨报应用服务：串起收集、生成、渲染和投递。"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Optional, Sequence
from zoneinfo import ZoneInfo

from .config import AppConfig
from .delivery import Sink, build_sinks, deliver_to_all
from .generator import EMPTY_REPORT_BODY, MorningReportGenerator
from .models import ContextItem, ReportRun, TimeWindow
from .pipeline import collect_items
from .render import messages_to_text, render_document
from .sources import ContextSource, build_sources

log = logging.getLogger(__name__)


class MorningReportApp:
    """一次“收集 → 生成 → 投递”的晨报应用。

    依赖都可以注入（``sources``、``sinks``、``generator``、``now_fn``），
    便于单元测试、替换模型和嵌入其他调度系统。
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        sources: Optional[Sequence[ContextSource]] = None,
        sinks: Optional[Sequence[Sink]] = None,
        generator: Optional[MorningReportGenerator] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.config = config
        self.tz = ZoneInfo(config.schedule.timezone)
        self.sources = (
            list(sources)
            if sources is not None
            else build_sources(config.sources, base_dir=config.base_dir)
        )
        self.sinks = (
            list(sinks)
            if sinks is not None
            else build_sinks(config.delivery, base_dir=config.base_dir)
        )
        report = config.report
        self.generator = generator or MorningReportGenerator(
            temperature=report.temperature,
            max_chars=report.max_chars,
            max_items=report.max_items,
            model=report.model,
        )
        self._now_fn = now_fn or (lambda: datetime.now(self.tz))

    def build_window(self, moment: datetime) -> TimeWindow:
        report = self.config.report
        return TimeWindow(
            start=moment - timedelta(hours=report.lookback_hours),
            end=moment + timedelta(hours=report.lookahead_hours),
        )

    def collect(self, window: TimeWindow) -> tuple[ContextItem, ...]:
        return collect_items(
            self.sources, window, max_items=self.config.report.max_items
        )

    def run(
        self, *, now: Optional[datetime] = None, dry_run: bool = False
    ) -> ReportRun:
        """运行一次晨报。

        ``dry_run=True`` 时只收集上下文并返回渲染好的提示词，不调用模型，
        也不投递，方便在没有 API Key 的环境里检查输入。
        """
        moment = self._resolve_now(now)
        report_date = moment.date()
        window = self.build_window(moment)
        items = self.collect(window)

        messages = self.generator.build_messages(
            items, report_date, window, user_name=self.config.report.user_name
        )
        if dry_run:
            return ReportRun(
                report_date=report_date,
                window=window,
                items=items,
                content="",
                prompt=messages_to_text(messages),
                generated=False,
            )

        if items:
            body = self.generator.generate(
                items,
                report_date,
                window,
                user_name=self.config.report.user_name,
                messages=messages,
            )
            generated = True
        else:
            body = EMPTY_REPORT_BODY
            generated = False

        content = render_document(report_date, body)
        delivered = deliver_to_all(self.sinks, content, report_date)
        log.info(
            "晨报完成：%s，%d 条上下文，投递通道 %s",
            report_date.isoformat(),
            len(items),
            ", ".join(delivered) or "无",
        )
        return ReportRun(
            report_date=report_date,
            window=window,
            items=items,
            content=content,
            delivered=delivered,
            generated=generated,
        )

    def _resolve_now(self, now: Optional[datetime]) -> datetime:
        moment = now if now is not None else self._now_fn()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=self.tz)
        return moment.astimezone(self.tz)


__all__ = ["MorningReportApp"]
