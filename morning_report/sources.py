"""上下文来源（Context Source）适配器。

一个来源只需要实现::

    class MySource:
        name = "my-source"

        def collect(self, window: TimeWindow) -> Iterable[ContextItem]:
            ...

内置 ``json_file`` 来源用于从本地 JSON 文件读取上下文；其他来源可以通过
配置里的 ``{"type": "python", "class": "包.模块:类"}`` 动态加载，从而把日历、
待办、飞书消息、GitHub 动态等接入晨报，而不需要改动本模块。
"""

from __future__ import annotations

import importlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from .models import ContextItem, TimeWindow

log = logging.getLogger(__name__)


class SourceError(RuntimeError):
    """读取或构建上下文来源失败。"""


@runtime_checkable
class ContextSource(Protocol):
    """上下文来源协议，内置来源与自定义来源都遵循它。"""

    name: str

    def collect(self, window: TimeWindow) -> Iterable[ContextItem]:
        """返回落在 ``window`` 内的上下文条目。"""
        ...


@dataclass
class StaticSource:
    """内存中的固定来源，便于测试或嵌入其他程序。"""

    items: Sequence[ContextItem]
    name: str = "static"

    def collect(self, window: TimeWindow) -> list[ContextItem]:
        return [item for item in self.items if _matches(item, window)]


@dataclass
class JsonFileSource:
    """从 JSON 文件读取上下文。

    文件内容可以是条目数组，也可以是 ``{"items": [...]}``。每条记录支持：

    - ``title``（必填）
    - ``source``
    - ``detail``
    - ``happened_at``（ISO 8601 字符串）
    - ``priority``（整数）
    - ``tags``（字符串数组）
    - ``url``
    """

    path: Path
    name: str = "json_file"
    include_undated: bool = True

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def collect(self, window: TimeWindow) -> list[ContextItem]:
        entries = self._load_entries()
        items: list[ContextItem] = []
        for index, entry in enumerate(entries):
            item = _parse_entry(entry, index=index, window=window, default_source=self.name)
            if _matches(item, window, include_undated=self.include_undated):
                items.append(item)
        return items

    def _load_entries(self) -> list[Mapping[str, Any]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SourceError(f"无法读取上下文文件 {self.path}：{exc}") from exc
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SourceError(f"上下文文件 {self.path} 不是合法 JSON：{exc}") from exc

        if isinstance(raw, dict):
            raw = raw.get("items")
        if not isinstance(raw, list):
            raise SourceError(f"上下文文件 {self.path} 必须是条目数组或包含 items 数组的对象")
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise SourceError(f"上下文文件 {self.path} 第 {index + 1} 条不是对象")
        return raw


def build_source(spec: Mapping[str, Any], *, base_dir: Path) -> ContextSource:
    """根据配置构建一个来源，``path`` 等相对路径按配置文件所在目录解析。"""
    data = dict(spec)
    kind = data.pop("type", None)

    if kind == "json_file":
        raw_path = data.pop("path", None)
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise SourceError("json_file 来源需要非空的 path")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        try:
            return JsonFileSource(path=path, **data)
        except TypeError as exc:
            raise SourceError(f"json_file 来源配置错误：{exc}") from exc

    if kind == "python":
        target = data.pop("class", None)
        if not isinstance(target, str) or ":" not in target:
            raise SourceError("python 来源需要形如 '包.模块:类' 的 class")
        options = data.pop("options", {})
        if not isinstance(options, Mapping):
            raise SourceError("python 来源的 options 必须是对象")
        if data:
            raise SourceError(f"python 来源包含未知字段：{', '.join(sorted(data))}")
        try:
            source = _import_class(target)(**dict(options))
        except Exception as exc:  # noqa: BLE001 - 动态加载失败需要带上下文报告
            raise SourceError(f"无法加载来源 {target}：{exc}") from exc
        if not callable(getattr(source, "collect", None)):
            raise SourceError(f"来源 {target} 没有可调用的 collect() 方法")
        return source

    raise SourceError(f"未知的上下文来源类型：{kind!r}")


def build_sources(
    specs: Sequence[Mapping[str, Any]], *, base_dir: Path
) -> list[ContextSource]:
    """构建全部来源；单个来源配置错误会直接报错，避免静默漏报。"""
    sources: list[ContextSource] = []
    for spec in specs:
        if not isinstance(spec, Mapping):
            raise SourceError("sources 中的每一项都必须是对象")
        sources.append(build_source(spec, base_dir=base_dir))
    return sources


def _import_class(target: str) -> type:
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise SourceError(f"模块 {module_name} 中不存在 {attr}") from exc


def _parse_entry(
    entry: Mapping[str, Any],
    *,
    index: int,
    window: TimeWindow,
    default_source: str,
) -> ContextItem:
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        raise SourceError(f"第 {index + 1} 条上下文缺少非空的 title")

    happened_at = entry.get("happened_at")
    if happened_at is not None:
        if not isinstance(happened_at, str):
            raise SourceError(f"第 {index + 1} 条上下文的 happened_at 必须是 ISO 时间字符串")
        try:
            parsed = datetime.fromisoformat(happened_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SourceError(
                f"第 {index + 1} 条上下文的 happened_at 不是合法 ISO 时间：{happened_at!r}"
            ) from exc
        if parsed.tzinfo is None and window.start.tzinfo is not None:
            parsed = parsed.replace(tzinfo=window.start.tzinfo)
    else:
        parsed = None

    priority = entry.get("priority", 0)
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise SourceError(f"第 {index + 1} 条上下文的 priority 必须是整数")

    tags = entry.get("tags", ())
    if isinstance(tags, str):
        tags = (tags,)
    elif not isinstance(tags, (list, tuple)):
        raise SourceError(f"第 {index + 1} 条上下文的 tags 必须是字符串数组")

    return ContextItem(
        title=title,
        source=str(entry.get("source") or default_source),
        detail=str(entry.get("detail") or ""),
        happened_at=parsed,
        priority=priority,
        tags=tuple(str(tag) for tag in tags),
        url=str(entry.get("url") or ""),
    )


def _matches(
    item: ContextItem, window: TimeWindow, *, include_undated: bool = True
) -> bool:
    if item.happened_at is None:
        return include_undated
    return window.contains(item.happened_at)


__all__ = [
    "ContextSource",
    "JsonFileSource",
    "SourceError",
    "StaticSource",
    "build_source",
    "build_sources",
]
