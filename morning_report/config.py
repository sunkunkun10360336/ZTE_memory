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

from .generator import EMPTY_REPORT_BODY
from .scheduler import parse_hhmm

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

#: 内置提示词配置：``default`` 面向个人日程，``group`` 面向群聊速报。
PROMPT_PROFILES = frozenset({"default", "group"})


class ConfigError(ValueError):
    """配置文件缺失、格式错误或字段非法。"""


@dataclass(frozen=True)
class ScheduleConfig:
    """执行节奏：每天固定时刻，或按固定分钟间隔。

    ``at`` 与 ``interval_minutes`` 二选一；两个都不写时默认每天 08:00。
    """

    at: Optional[time] = None
    timezone: str = "Asia/Shanghai"
    interval_minutes: Optional[int] = None


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
    profile: str = "default"
    title: str = "每日晨报"
    footer: str = "本晨报由 PCE 根据个人上下文自动生成。"
    empty_body: str = EMPTY_REPORT_BODY
    system_prompt: Optional[str] = None


@dataclass(frozen=True)
class AppConfig:
    """一次晨报运行的完整配置。"""

    base_dir: Path
    schedule: ScheduleConfig
    report: ReportConfig
    sources: Tuple[Mapping[str, Any], ...]
    delivery: Tuple[Mapping[str, Any], ...]
    state_path: Optional[Path] = None

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
    at_raw = schedule_raw.get("at")
    interval_raw = schedule_raw.get("interval_minutes")
    if at_raw is not None and interval_raw is not None:
        raise ConfigError("schedule.at 与 schedule.interval_minutes 只能二选一")
    if interval_raw is not None:
        if isinstance(interval_raw, bool) or not isinstance(interval_raw, int):
            raise ConfigError("schedule.interval_minutes 必须是整数")
        if interval_raw < 1:
            raise ConfigError("schedule.interval_minutes 不能小于 1")
        at = None
        interval_minutes: Optional[int] = interval_raw
    else:
        try:
            at = parse_hhmm(str(at_raw if at_raw is not None else "08:00"))
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        interval_minutes = None

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

    state_path = _parse_state_path(raw.get("state_file"), config_path.resolve().parent)

    return AppConfig(
        base_dir=config_path.resolve().parent,
        schedule=ScheduleConfig(
            at=at, timezone=timezone_name, interval_minutes=interval_minutes
        ),
        report=report,
        sources=tuple(dict(source) for source in sources),
        delivery=tuple(dict(sink) for sink in delivery),
        state_path=state_path,
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

    profile = raw.get("profile", "default")
    if not isinstance(profile, str) or profile not in PROMPT_PROFILES:
        allowed = "、".join(sorted(PROMPT_PROFILES))
        raise ConfigError(f"report.profile 必须是以下之一：{allowed}")

    title = raw.get("title", "每日晨报")
    if not isinstance(title, str) or not title.strip():
        raise ConfigError("report.title 必须是非空字符串")

    footer = raw.get("footer", "本晨报由 PCE 根据个人上下文自动生成。")
    if not isinstance(footer, str):
        raise ConfigError("report.footer 必须是字符串")

    empty_body = raw.get("empty_body", EMPTY_REPORT_BODY)
    if not isinstance(empty_body, str) or not empty_body.strip():
        raise ConfigError("report.empty_body 必须是非空字符串")

    system_prompt = raw.get("system_prompt")
    if system_prompt is not None and (
        not isinstance(system_prompt, str) or not system_prompt.strip()
    ):
        raise ConfigError("report.system_prompt 必须是非空字符串")

    return ReportConfig(
        user_name=user_name,
        lookback_hours=lookback,
        lookahead_hours=lookahead,
        max_items=max_items,
        max_chars=max_chars,
        temperature=temperature,
        model=model,
        profile=profile,
        title=title.strip(),
        footer=footer,
        empty_body=empty_body,
        system_prompt=system_prompt,
    )


def _parse_state_path(value: Any, base_dir: Path) -> Optional[Path]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("state_file 必须是非空字符串")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


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
    "PROMPT_PROFILES",
    "ReportConfig",
    "ScheduleConfig",
    "load_config",
]
