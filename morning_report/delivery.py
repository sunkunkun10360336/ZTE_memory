"""晨报投递通道（Sink）。

内置控制台、Markdown 文件和飞书自定义机器人三种通道；也可以通过
``{"type": "python", "class": "包.模块:类"}`` 注册自定义通道。所有通道都
遵循 :class:`Sink` 协议，便于扩展和测试。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, TextIO, Tuple

log = logging.getLogger(__name__)


class DeliveryError(RuntimeError):
    """晨报投递失败。"""


class Sink(Protocol):
    """投递通道协议。"""

    name: str

    def deliver(self, report: str, *, report_date: date) -> None:
        """投递一份晨报。"""
        ...


@dataclass
class ConsoleSink:
    """把晨报打印到标准输出。"""

    name: str = "console"
    stream: Optional[TextIO] = None

    def deliver(self, report: str, *, report_date: date) -> None:
        stream = self.stream or sys.stdout
        print(report, file=stream, flush=True)


@dataclass
class FileSink:
    """把晨报写成 Markdown 文件，默认按日期命名并覆盖当天旧文件。"""

    directory: Path
    name: str = "file"
    filename_pattern: str = "morning-report-{date}.md"

    def __post_init__(self) -> None:
        self.directory = Path(self.directory)

    def path_for(self, report_date: date) -> Path:
        filename = self.filename_pattern.format(date=report_date.isoformat())
        return self.directory / filename

    def deliver(self, report: str, *, report_date: date) -> None:
        target = self.path_for(report_date)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(report, encoding="utf-8")
        except OSError as exc:
            raise DeliveryError(f"无法写入晨报文件 {target}：{exc}") from exc
        log.info("晨报已写入 %s", target)


@dataclass
class FeishuWebhookSink:
    """通过飞书群自定义机器人 Webhook 投递纯文本晨报。

    ``secret`` 用于开启“签名校验”；也可以在配置里用环境变量注入，避免把
    密钥写进仓库。``transport`` 仅用于测试注入，默认使用 urllib。
    """

    url: str
    secret: str = ""
    timeout: float = 10.0
    name: str = "feishu_webhook"
    transport: Optional[Callable[[str, bytes, float], Mapping[str, Any]]] = field(
        default=None, repr=False, compare=False
    )

    def build_payload(
        self, report: str, *, timestamp: Optional[int] = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "msg_type": "text",
            "content": {"text": report},
        }
        if self.secret:
            moment = int(time.time()) if timestamp is None else int(timestamp)
            payload["timestamp"] = str(moment)
            payload["sign"] = feishu_sign(moment, self.secret)
        return payload

    def deliver(self, report: str, *, report_date: date) -> None:
        payload = self.build_payload(report)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        transport = self.transport or _post_json
        try:
            response = transport(self.url, body, self.timeout)
        except DeliveryError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络异常统一包装
            raise DeliveryError(f"飞书 Webhook 请求失败：{exc}") from exc

        if isinstance(response, Mapping):
            code = response.get("code", response.get("StatusCode"))
            if code not in (None, 0):
                message = response.get("msg") or response.get("StatusMessage") or response
                raise DeliveryError(f"飞书机器人返回错误：{code} {message}")


def feishu_sign(timestamp: int, secret: str) -> str:
    """计算飞书自定义机器人的签名（HMAC-SHA256 + Base64）。"""
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


@dataclass
class MultiSink:
    """把晨报依次投递到多个通道；部分失败时继续投递其余通道。"""

    sinks: Sequence[Sink]
    name: str = "multi"

    def deliver(self, report: str, *, report_date: date) -> Tuple[str, ...]:
        delivered: list[str] = []
        errors: list[str] = []
        for sink in self.sinks:
            sink_name = getattr(sink, "name", type(sink).__name__)
            try:
                sink.deliver(report, report_date=report_date)
                delivered.append(sink_name)
            except Exception as exc:  # noqa: BLE001 - 单个通道失败不影响其他通道
                errors.append(f"{sink_name}: {exc}")
                log.warning("晨报投递通道 %s 失败：%s", sink_name, exc)
        if errors and not delivered:
            raise DeliveryError("所有投递通道均失败：" + "；".join(errors))
        return tuple(delivered)


def deliver_to_all(
    sinks: Sequence[Sink], report: str, report_date: date
) -> Tuple[str, ...]:
    """投递并返回成功的通道名；没有配置通道时返回空元组。"""
    return MultiSink(sinks).deliver(report, report_date=report_date)


def _construct_sink(cls: type, data: Mapping[str, Any], kind: str) -> Any:
    """构造内置通道，把字段错误转换成可读的配置错误。"""
    try:
        return cls(**dict(data))
    except TypeError as exc:
        raise DeliveryError(f"{kind} 通道配置错误：{exc}") from exc


def build_sink(spec: Mapping[str, Any], *, base_dir: Path) -> Sink:
    """根据配置构建一个投递通道。"""
    data = dict(spec)
    kind = data.pop("type", None)

    if kind == "console":
        return _construct_sink(ConsoleSink, data, kind)

    if kind == "file":
        raw_directory = data.pop("directory", "reports")
        if not isinstance(raw_directory, str) or not raw_directory.strip():
            raise DeliveryError("file 通道的 directory 必须是非空字符串")
        directory = Path(raw_directory).expanduser()
        if not directory.is_absolute():
            directory = (base_dir / directory).resolve()
        return _construct_sink(FileSink, {"directory": directory, **data}, kind)

    if kind == "feishu_webhook":
        url = data.get("url")
        if not isinstance(url, str) or not url.strip():
            raise DeliveryError("feishu_webhook 通道需要非空 url")
        return _construct_sink(FeishuWebhookSink, data, kind)

    if kind == "python":
        target = data.pop("class", None)
        if not isinstance(target, str) or ":" not in target:
            raise DeliveryError("python 通道需要形如 '包.模块:类' 的 class")
        options = data.pop("options", {})
        if not isinstance(options, Mapping):
            raise DeliveryError("python 通道的 options 必须是对象")
        if data:
            raise DeliveryError(f"python 通道包含未知字段：{', '.join(sorted(data))}")
        try:
            sink = _import_class(target)(**dict(options))
        except Exception as exc:  # noqa: BLE001 - 动态加载失败需要带上下文报告
            raise DeliveryError(f"无法加载投递通道 {target}：{exc}") from exc
        if not callable(getattr(sink, "deliver", None)):
            raise DeliveryError(f"投递通道 {target} 没有可调用的 deliver() 方法")
        return sink

    raise DeliveryError(f"未知的投递通道类型：{kind!r}")


def build_sinks(
    specs: Sequence[Mapping[str, Any]], *, base_dir: Path
) -> list[Sink]:
    sinks: list[Sink] = []
    for spec in specs:
        if not isinstance(spec, Mapping):
            raise DeliveryError("delivery 中的每一项都必须是对象")
        sinks.append(build_sink(spec, base_dir=base_dir))
    return sinks


def _import_class(target: str) -> type:
    module_name, _, attr = target.partition(":")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise DeliveryError(f"模块 {module_name} 中不存在 {attr}") from exc


def _post_json(url: str, body: bytes, timeout: float) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise DeliveryError(f"飞书 Webhook 返回 HTTP {exc.code}：{detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise DeliveryError(f"无法连接飞书 Webhook {url}：{reason}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeliveryError(f"飞书 Webhook 返回了非 JSON 响应：{raw[:200]}") from exc
    if not isinstance(payload, Mapping):
        raise DeliveryError("飞书 Webhook 返回格式错误")
    return payload


__all__ = [
    "ConsoleSink",
    "DeliveryError",
    "FeishuWebhookSink",
    "FileSink",
    "MultiSink",
    "Sink",
    "build_sink",
    "build_sinks",
    "deliver_to_all",
    "feishu_sign",
]
