"""上下文收集与聚合流水线：去重、排序、截断。"""

from __future__ import annotations

import logging
from typing import Iterable, Sequence, Tuple

from .models import ContextItem, TimeWindow
from .sources import ContextSource, SourceError

log = logging.getLogger(__name__)


def collect_items(
    sources: Sequence[ContextSource],
    window: TimeWindow,
    *,
    max_items: int = 30,
) -> Tuple[ContextItem, ...]:
    """从所有来源收集上下文，按优先级与时间排序后返回前 ``max_items`` 条。

    单个来源失败只会记录告警并继续，保证晨报不会因为一个外部依赖不可用而
    整体失败；但如果所有来源都失败，则抛出 :class:`SourceError`，避免产出
    一份“看起来正常、其实什么都没读到”的晨报。
    """
    if max_items <= 0:
        raise ValueError("max_items 必须大于 0")

    collected: list[ContextItem] = []
    failures: list[str] = []
    for source in sources:
        name = getattr(source, "name", type(source).__name__)
        try:
            for item in source.collect(window):
                if not isinstance(item, ContextItem):
                    raise TypeError(f"来源返回了非 ContextItem 对象：{type(item).__name__}")
                collected.append(item)
        except Exception as exc:  # noqa: BLE001 - 单个来源失败不应拖垮整份晨报
            failures.append(f"{name}: {exc}")
            log.warning("上下文来源 %s 收集失败：%s", name, exc)

    if sources and len(failures) == len(sources):
        raise SourceError("所有上下文来源均收集失败：" + "；".join(failures))

    unique = _dedupe(collected)
    unique.sort(key=_sort_key)
    return tuple(unique[:max_items])


def _dedupe(items: Iterable[ContextItem]) -> list[ContextItem]:
    seen: set[tuple[str, str]] = set()
    unique: list[ContextItem] = []
    for item in items:
        timestamp = item.happened_at.isoformat() if item.happened_at else ""
        key = (item.title.casefold(), timestamp)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _sort_key(item: ContextItem) -> tuple[int, float]:
    timestamp = item.happened_at.timestamp() if item.happened_at else float("-inf")
    # 优先级从高到低；同一优先级内时间从近到远。
    return (-item.priority, -timestamp)


__all__ = ["collect_items"]
