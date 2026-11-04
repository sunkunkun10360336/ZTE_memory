"""配置加载与校验。

配置使用 JSON 文件，字符串里支持 ``${ENV_VAR}`` / ``${ENV_VAR:-默认值}``
两种环境变量写法，便于把 API Key、Webhook 地址等密钥留在环境变量里。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .scheduler import parse_hhmm

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """配置文件缺失、格式错误或字段非法。"""


@dataclass(frozen=True)
class ScheduleConfig:
    """每天的执行时间与时区。"""

    at: time
    timezone: str = "Asia/Shanghai"


@dataclass(frozen=True)
class ReportConfig:
    """晨报内容与模型参数。"""

    user_name: str = ""
    lookback_hours: float = 24.0
    lookahead_hours: float = 12.0
    max_items: int = 30
    max_chars: int = 500
    temperature: float = 0.4
    model: Optional[str] = None


@dataclass(frozen=True)
class AppConfig:
    """一次晨报运行的完整配置。"""

    base_dir: Path
    schedule: ScheduleConfig
    report: ReportConfig
    sources: Tuple[Mapping[str, Any], ...]
    delivery: Tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if not self.sources:
            raise ConfigError("至少需要配置一个上下文来源（sources）")
        if not self.delivery:
            raise ConfigError("至少需要配置一个投递通道（delivery）")


def load_config(path: Path | str) -> AppConfig:
    """读取并校验 JSON 配置。"""
    config_path = Path(path).expanduser()
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {config_path}：{exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件 {config_path} 不是合法 JSON：{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是 JSON 对象")

    raw = _expand_env(raw)

    timezone_name = raw.get("timezone", "Asia/Shanghai")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ConfigError("timezone 必须是非空字符串")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError(f"未知时区：{timezone_name}") from exc

    schedule_raw = raw.get("schedule", {})
    if not isinstance(schedule_raw, dict):
        raise ConfigError("schedule 必须是对象")
    try:
        at = parse_hhmm(str(schedule_raw.get("at", "08:00")))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    report_raw = raw.get("report", {})
    if not isinstance(report_raw, dict):
        raise ConfigError("report 必须是对象")
    report = _parse_report_config(report_raw)

    sources = raw.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ConfigError("sources 必须是非空数组")
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ConfigError(f"sources[{index}] 必须是对象")
        if not source.get("type"):
            raise ConfigError(f"sources[{index}] 缺少 type")

    delivery = raw.get("delivery")
    if delivery is None:
        delivery = [{"type": "console"}]
    if not isinstance(delivery, list) or not delivery:
        raise ConfigError("delivery 必须是非空数组")
    for index, sink in enumerate(delivery):
        if not isinstance(sink, dict):
            raise ConfigError(f"delivery[{index}] 必须是对象")
        if not sink.get("type"):
            raise ConfigError(f"delivery[{index}] 缺少 type")

    return AppConfig(
        base_dir=config_path.resolve().parent,
        schedule=ScheduleConfig(at=at, timezone=timezone_name),
        report=report,
        sources=tuple(dict(source) for source in sources),
        delivery=tuple(dict(sink) for sink in delivery),
    )


def _parse_report_config(raw: Mapping[str, Any]) -> ReportConfig:
    user_name = raw.get("user_name", "")
    if not isinstance(user_name, str):
        raise ConfigError("report.user_name 必须是字符串")

    model = raw.get("model")
    if model is not None and not isinstance(model, str):
        raise ConfigError("report.model 必须是字符串")
    if isinstance(model, str) and not model.strip():
        model = None

    lookback = _number(raw, "lookback_hours", 24.0, minimum=0.0)
    lookahead = _number(raw, "lookahead_hours", 12.0, minimum=0.0)
    max_items = _integer(raw, "max_items", 30, minimum=1)
    max_chars = _integer(raw, "max_chars", 500, minimum=1)
    temperature = _number(raw, "temperature", 0.4, minimum=0.0, maximum=2.0)

    return ReportConfig(
        user_name=user_name,
        lookback_hours=lookback,
        lookahead_hours=lookahead,
        max_items=max_items,
        max_chars=max_chars,
        temperature=temperature,
        model=model,
    )


def _number(
    raw: Mapping[str, Any],
    key: str,
    default: float,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"report.{key} 必须是数字")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ConfigError(f"report.{key} 不能小于 {minimum}")
    if maximum is not None and number > maximum:
        raise ConfigError(f"report.{key} 不能大于 {maximum}")
    return number


def _integer(
    raw: Mapping[str, Any], key: str, default: int, *, minimum: int
) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"report.{key} 必须是整数")
    if value < minimum:
        raise ConfigError(f"report.{key} 不能小于 {minimum}")
    return value


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(_replace_env, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def _replace_env(match: re.Match[str]) -> str:
    name, default = match.group(1), match.group(2)
    if name in os.environ:
        return os.environ[name]
    if default is not None:
        return default
    raise ConfigError(f"环境变量 {name} 未设置（可在配置中使用 ${{{name}:-默认值}}）")


__all__ = [
    "AppConfig",
    "ConfigError",
    "ReportConfig",
    "ScheduleConfig",
    "load_config",
]
