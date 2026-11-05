"""飞书群聊来源与投递通道。

本模块通过 ``lark-cli`` 读取群消息、发送晨报，机器人身份由 lark-cli 的
配置决定；仓库里不保存任何 App Secret 或 Token。

添加来源::

    {"type": "feishu_chat", "chat_id": "oc_xxx"}

添加投递通道::

    {"type": "feishu_chat", "chat_id": "oc_xxx"}

``runner`` 参数用于测试注入，签名是 ``(args, stdin) -> stdout``；默认通过
子进程调用 lark-cli。
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence

from .delivery import DeliveryError
from .models import ContextItem, TimeWindow
from .sources import SourceError

#: 连续 8 位以上的数字按号码处理，避免把票号、证件号、手机号带进晨报。
_LONG_NUMBER = re.compile(r"\d{8,}")

#: 直接执行 lark-cli 的默认 runner。
CliRunner = Callable[[Sequence[str], Optional[str]], str]


class LarkCliError(RuntimeError):
    """调用 lark-cli 失败或返回了错误结果。"""


@dataclass
class LarkCli:
    """lark-cli 的轻量封装，只做 JSON 调用与错误包装。"""

    executable: str = "lark-cli"
    timeout: float = 30.0
    runner: Optional[CliRunner] = field(default=None, repr=False, compare=False)

    def run_json(
        self, args: Sequence[str], *, stdin: Optional[str] = None
    ) -> Mapping[str, Any]:
        raw = self._run(list(args), stdin)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LarkCliError(f"lark-cli 返回了非 JSON 响应：{raw[:200]}") from exc
        if not isinstance(payload, Mapping):
            raise LarkCliError("lark-cli 返回格式错误")
        if payload.get("ok") is False:
            error = payload.get("error") or {}
            if isinstance(error, Mapping):
                detail = error.get("message") or error.get("type") or error
            else:
                detail = error
            raise LarkCliError(f"lark-cli 调用失败：{detail}")
        return payload

    def _run(self, args: Sequence[str], stdin: Optional[str]) -> str:
        if self.runner is not None:
            try:
                return self.runner(args, stdin)
            except LarkCliError:
                raise
            except Exception as exc:  # noqa: BLE001 - 注入的 runner 失败统一包装
                raise LarkCliError(f"lark-cli 调用失败：{exc}") from exc
        command = [self.executable, *args]
        try:
            completed = subprocess.run(
                command,
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise LarkCliError(f"找不到 lark-cli 可执行文件：{self.executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise LarkCliError(f"lark-cli 执行超时（{self.timeout:.0f} 秒）") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:500]
            raise LarkCliError(f"lark-cli 执行失败（退出码 {completed.returncode}）：{detail}")
        return completed.stdout


@dataclass
class FeishuChatSource:
    """从飞书群读取指定时间窗口内的消息。

    机器人消息和系统消息默认跳过，避免晨报把自己刚发的总结再次总结一遍；
    长数字会被替换成 ``[号码已省略]``，减少敏感信息进入提示词的概率。
    """

    chat_id: str
    name: str = "feishu_chat"
    source_label: str = "群聊"
    cli_path: str = "lark-cli"
    timeout: float = 30.0
    max_messages: int = 100
    max_message_chars: int = 400
    include_apps: bool = False
    redact_long_numbers: bool = True
    runner: Optional[CliRunner] = field(default=None, repr=False, compare=False)

    def collect(self, window: TimeWindow) -> list[ContextItem]:
        if not self.chat_id.strip():
            raise SourceError("飞书来源需要非空 chat_id")
        cli = LarkCli(self.cli_path, self.timeout, self.runner)
        try:
            payload = cli.run_json(self._list_args(window))
        except LarkCliError as exc:
            raise SourceError(str(exc)) from exc
        data = payload.get("data") or {}
        messages = data.get("messages") if isinstance(data, Mapping) else None
        if not isinstance(messages, list):
            raise SourceError("lark-cli 返回的消息列表格式错误")

        items: list[ContextItem] = []
        for raw in messages:
            if not isinstance(raw, Mapping):
                continue
            item = self._to_item(raw, window)
            if item is not None:
                items.append(item)
        items.sort(key=lambda item: item.happened_at or window.start)
        return items[: self.max_messages]

    def _list_args(self, window: TimeWindow) -> list[str]:
        return [
            "im",
            "+chat-messages-list",
            "--chat-id",
            self.chat_id,
            "--start",
            window.start.isoformat(),
            "--end",
            window.end.isoformat(),
            "--order",
            "asc",
            "--page-all",
            "--page-size",
            "50",
            "--no-reactions",
            "--format",
            "json",
        ]

    def _to_item(self, raw: Mapping[str, Any], window: TimeWindow) -> Optional[ContextItem]:
        if raw.get("deleted"):
            return None
        sender = raw.get("sender")
        if not isinstance(sender, Mapping):
            return None
        if not self.include_apps and sender.get("sender_type") == "app":
            return None
        sender_name = str(sender.get("name") or sender.get("id") or "").strip()
        if not sender_name:
            return None

        content = _message_text(raw)
        if not content:
            return None
        if self.redact_long_numbers:
            content = _LONG_NUMBER.sub("[号码已省略]", content)
        content = content.strip()
        if len(content) > self.max_message_chars:
            content = content[: self.max_message_chars] + "…"

        happened_at = _parse_message_time(raw.get("create_time"), window.start.tzinfo)
        if happened_at is None or not window.contains(happened_at):
            return None

        snippet = _one_line(content)[:60]
        detail = content if len(content) > len(snippet) else ""
        return ContextItem(
            title=f"{sender_name}：{snippet}",
            source=self.source_label,
            detail=detail,
            happened_at=happened_at,
            priority=3,
            tags=("群聊",),
            url=str(raw.get("message_app_link") or ""),
        )


@dataclass
class FeishuChatSink:
    """把晨报以 Markdown 富文本消息发送到飞书群。"""

    chat_id: str
    name: str = "feishu_chat"
    cli_path: str = "lark-cli"
    timeout: float = 60.0
    runner: Optional[CliRunner] = field(default=None, repr=False, compare=False)

    def deliver(self, report: str, *, report_date) -> None:
        del report_date  # 消息正文已经带日期，这里不需要额外使用
        if not self.chat_id.strip():
            raise DeliveryError("飞书通道需要非空 chat_id")
        cli = LarkCli(self.cli_path, self.timeout, self.runner)
        args = [
            "im",
            "+messages-send",
            "--chat-id",
            self.chat_id,
            "--markdown",
            "-",
        ]
        try:
            cli.run_json(args, stdin=report)
        except LarkCliError as exc:
            raise DeliveryError(f"飞书消息发送失败：{exc}") from exc


def _message_text(raw: Mapping[str, Any]) -> str:
    content = raw.get("content")
    msg_type = raw.get("msg_type") or "text"
    if isinstance(content, str):
        text = content
    else:
        text = ""

    if msg_type in ("text", "post"):
        if not text:
            return ""
        if msg_type == "post":
            # lark-cli 对 post 消息通常已经返回纯文本；只有拿到 JSON 时才解析。
            return _post_text(text) or text
        return text

    parsed = _post_text(text)
    if parsed:
        return parsed
    return f"[{_MESSAGE_TYPES.get(msg_type, msg_type)}]"


def _post_text(content: str) -> str:
    """从飞书 ``post`` 消息的 JSON 中提取纯文本。"""
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return ""
    paragraphs: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, str):
            paragraphs.append(node)
        elif isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, Mapping):
            if isinstance(node.get("text"), str):
                paragraphs.append(node["text"])
            for key in ("content", "title"):
                if key in node:
                    walk(node[key])

    walk(payload)
    return " ".join(part for part in paragraphs if part.strip())


_MESSAGE_TYPES = {
    "image": "图片",
    "file": "文件",
    "audio": "语音",
    "media": "视频",
    "sticker": "表情",
    "share_chat": "群名片",
    "share_user": "个人名片",
    "merge_forward": "合并转发",
}


def _parse_message_time(value: Any, tz) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=tz) if tz is not None else parsed
    return None


def _one_line(text: str) -> str:
    return " ".join(text.split())


__all__ = [
    "FeishuChatSink",
    "FeishuChatSource",
    "LarkCli",
    "LarkCliError",
]
