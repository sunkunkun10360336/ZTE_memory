"""晨报应用服务：串起收集、生成、渲染和投递。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .config import AppConfig
from .delivery import Sink, build_sinks, deliver_to_all
from .generator import GROUP_SUMMARY_SYSTEM_PROMPT, MorningReportGenerator
from .models import ContextItem, ReportRun, TimeWindow
from .pipeline import collect_items
from .render import messages_to_text, render_document
from .sources import ContextSource, build_sources

log = logging.getLogger(__name__)

#: 带状态运行时，下一窗口向前多覆盖一点，避免分钟级时间戳造成边界漏读；
#: 重复条目由条目指纹过滤掉。
STATE_OVERLAP = timedelta(minutes=2)

#: 状态文件里最多保留的已投递条目指纹数量。
MAX_SEEN = 500


@dataclass(frozen=True)
class _RunState:
    last_end: Optional[datetime] = None
    seen: Tuple[str, ...] = ()


class MorningReportApp:
    """一次“收集 → 生成 → 投递”的晨报应用。

    依赖都可以注入（``sources``、``sinks``、``generator``、``now_fn``），
    便于单元测试、替换模型和嵌入其他调度系统。

    配置了 ``state_file`` 时，每次成功投递后都会记录窗口终点和已投递条目
    指纹：下一次只处理新增内容，因此可以安全地按分钟级间隔运行。
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
        system_prompt = report.system_prompt
        if system_prompt is None and report.profile == "group":
            system_prompt = GROUP_SUMMARY_SYSTEM_PROMPT
        self.generator = generator or MorningReportGenerator(
            temperature=report.temperature,
            max_chars=report.max_chars,
            max_items=report.max_items,
            model=report.model,
            system_prompt=system_prompt,
        )
        self._now_fn = now_fn or (lambda: datetime.now(self.tz))

    def build_window(
        self, moment: datetime, last_end: Optional[datetime] = None
    ) -> TimeWindow:
        report = self.config.report
        if last_end is not None and last_end <= moment:
            start = last_end - STATE_OVERLAP
        else:
            start = moment - timedelta(hours=report.lookback_hours)
        end = moment + timedelta(hours=report.lookahead_hours)
        if end < start:
            end = moment
        return TimeWindow(start=start, end=end)

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
        state = self._load_state()
        window = self.build_window(moment, state.last_end)
        items = self._drop_seen(self.collect(window), state.seen)

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
                new_items=len(items),
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
            body = self.config.report.empty_body
            generated = False

        content = render_document(
            report_date,
            body,
            title=self.config.report.title,
            footer=self.config.report.footer,
        )
        delivered = deliver_to_all(self.sinks, content, report_date)
        self._save_state(moment, state.seen, items)
        log.info(
            "晨报完成：%s，%d 条新上下文，投递通道 %s",
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
            new_items=len(items),
        )

    def _resolve_now(self, now: Optional[datetime]) -> datetime:
        moment = now if now is not None else self._now_fn()
        if moment.tzinfo is None:
            return moment.replace(tzinfo=self.tz)
        return moment.astimezone(self.tz)

    @property
    def state_path(self) -> Optional[Path]:
        return self.config.state_path

    def _load_state(self) -> _RunState:
        path = self.state_path
        if path is None or not path.exists():
            return _RunState()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("无法读取晨报状态文件 %s：%s", path, exc)
            return _RunState()
        if not isinstance(raw, dict):
            log.warning("晨报状态文件 %s 格式错误，忽略", path)
            return _RunState()

        last_end = _parse_state_time(raw.get("last_end"), self.tz)
        seen_raw = raw.get("seen")
        seen: list[str] = []
        if isinstance(seen_raw, list):
            seen = [item for item in seen_raw if isinstance(item, str)]
        return _RunState(last_end=last_end, seen=tuple(seen))

    def _save_state(
        self, moment: datetime, previous_seen: Sequence[str], items: Sequence[ContextItem]
    ) -> None:
        path = self.state_path
        if path is None:
            return
        fingerprints = [self._fingerprint(item) for item in items]
        seen = list(dict.fromkeys([*fingerprints, *previous_seen]))[:MAX_SEEN]
        payload = {
            "last_end": moment.isoformat(),
            "seen": seen,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            temporary.replace(path)
        except OSError as exc:
            log.warning("无法写入晨报状态文件 %s：%s", path, exc)

    def _drop_seen(
        self, items: Sequence[ContextItem], seen: Sequence[str]
    ) -> tuple[ContextItem, ...]:
        if not seen:
            return tuple(items)
        known = set(seen)
        return tuple(item for item in items if self._fingerprint(item) not in known)

    @staticmethod
    def _fingerprint(item: ContextItem) -> str:
        moment = item.happened_at.isoformat() if item.happened_at else ""
        raw = "\x1f".join((item.source, item.title, moment, item.url))
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _parse_state_time(value: object, tz: ZoneInfo) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


__all__ = ["MorningReportApp"]
