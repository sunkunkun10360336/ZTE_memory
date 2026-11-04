"""DeepSeek 大模型调用封装（非流式输出）。

本模块只对接 DeepSeek 官方 API（OpenAI 兼容协议）的 ``/chat/completions`` 接口，
一次请求返回完整结果，不做流式输出。

API Key 从环境变量 ``DEEPSEEK_API_KEY`` 读取，也可以在创建 ``DeepSeekClient``
时显式传入。基本用法：

    from LLM import chat

    print(chat("你好，请用一句话介绍你自己。"))

    # 多轮消息
    print(chat([
        {"role": "system", "content": "你是一个简洁的助手。"},
        {"role": "user", "content": "什么是 PCE？"},
    ]))

需要复用配置或读取 token 用量时，可以直接使用 ``DeepSeekClient``：

    from LLM import DeepSeekClient

    client = DeepSeekClient(model="deepseek-reasoner")
    result = client.complete("帮我总结这段文字……")
    print(result.content, result.usage)
"""

from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

#: DeepSeek 官方 API 地址（OpenAI 兼容），也可通过环境变量覆盖。
DEFAULT_BASE_URL = "https://api.deepseek.com"
#: 默认使用 DeepSeek 的通用对话模型。
DEFAULT_MODEL = "deepseek-chat"
#: 单次请求的默认超时时间（秒）。
DEFAULT_TIMEOUT = 60.0

#: 读取 API Key 的环境变量名。
API_KEY_ENV = "DEEPSEEK_API_KEY"
#: 读取 API 地址的环境变量名。
BASE_URL_ENV = "DEEPSEEK_BASE_URL"
#: 读取模型名的环境变量名。
MODEL_ENV = "DEEPSEEK_MODEL"

_ALLOWED_ROLES = frozenset({"system", "user", "assistant"})

#: 传入的消息列表类型。
Messages = Union[str, Sequence[Mapping[str, Any]]]


class DeepSeekError(RuntimeError):
    """DeepSeek 调用失败时抛出的异常。"""


class DeepSeekAPIError(DeepSeekError):
    """DeepSeek 接口返回非 2xx 状态码时抛出的异常。"""

    def __init__(self, message: str, status_code: int, response_body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


@dataclass(frozen=True)
class ChatResult:
    """一次非流式对话的完整结果。"""

    content: str
    model: str
    finish_reason: Optional[str] = None
    usage: Mapping[str, int] = field(default_factory=dict)


class DeepSeekClient:
    """DeepSeek 非流式对话客户端。

    参数优先于环境变量，环境变量优先于默认值：

    - ``api_key``：``DEEPSEEK_API_KEY``，必填；
    - ``base_url``：``DEEPSEEK_BASE_URL``，默认 ``https://api.deepseek.com``；
    - ``model``：``DEEPSEEK_MODEL``，默认 ``deepseek-chat``；
    - ``timeout``：请求超时秒数，默认 60。
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        resolved_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV)
        if not resolved_key or not resolved_key.strip():
            raise DeepSeekError(
                "缺少 DeepSeek API Key：请设置环境变量 "
                f"{API_KEY_ENV}，或通过 DeepSeekClient(api_key=...) 传入。"
            )
        self.api_key = resolved_key.strip()
        self.base_url = (
            base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
        ).rstrip("/")
        self.model = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
        self.timeout = DEFAULT_TIMEOUT if timeout is None else float(timeout)
        if self.timeout <= 0:
            raise ValueError("timeout 必须大于 0")

    def chat(
        self,
        messages: Messages,
        *,
        system: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """发起一次非流式对话，返回助手回复的文本内容。"""
        return self.complete(
            messages,
            system=system,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        ).content

    def complete(
        self,
        messages: Messages,
        *,
        system: Optional[str] = None,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> ChatResult:
        """发起一次非流式对话，返回文本、模型、结束原因和 token 用量。

        ``messages`` 可以是字符串（作为单条 user 消息），也可以是
        ``{"role": "system"|"user"|"assistant", "content": "..."}`` 的列表。
        ``system`` 不为空时会作为系统消息插入到最前面。
        """
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": _normalize_messages(messages, system=system),
            "stream": False,
        }
        if temperature is not None:
            if not 0 <= temperature <= 2:
                raise ValueError("temperature 必须在 0 到 2 之间")
            payload["temperature"] = temperature
        if max_tokens is not None:
            if max_tokens <= 0:
                raise ValueError("max_tokens 必须大于 0")
            payload["max_tokens"] = max_tokens

        data = self._post(payload, timeout=timeout)
        return _parse_completion(data, fallback_model=str(payload["model"]))

    def _post(self, payload: Mapping[str, Any], *, timeout: Optional[float]) -> Any:
        url = f"{self.base_url}/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        request_timeout = self.timeout if timeout is None else float(timeout)
        if request_timeout <= 0:
            raise ValueError("timeout 必须大于 0")

        try:
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = _read_error_detail(exc)
            raise DeepSeekAPIError(
                f"DeepSeek 接口返回 HTTP {exc.code}：{detail}", exc.code, detail
            ) from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise DeepSeekError(
                f"无法连接 DeepSeek 接口 {url}：{reason}"
            ) from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise DeepSeekError(
                f"DeepSeek 接口返回了非 JSON 响应：{raw[:200]}"
            ) from exc


def chat(
    messages: Messages,
    *,
    system: Optional[str] = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
) -> str:
    """使用环境变量中的配置发起一次 DeepSeek 非流式对话，返回文本。"""
    return DeepSeekClient().chat(
        messages,
        system=system,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def _normalize_messages(
    messages: Messages, *, system: Optional[str]
) -> List[Dict[str, str]]:
    """把字符串或消息列表统一成 DeepSeek 需要的消息格式。"""
    if isinstance(messages, str):
        if not messages.strip():
            raise ValueError("messages 不能为空")
        normalized: List[Dict[str, str]] = [{"role": "user", "content": messages}]
    elif isinstance(messages, Sequence):
        normalized = []
        for index, message in enumerate(messages):
            if not isinstance(message, Mapping):
                raise TypeError(f"messages[{index}] 必须是包含 role/content 的字典")
            role = message.get("role")
            content = message.get("content")
            if not isinstance(role, str) or role not in _ALLOWED_ROLES:
                raise ValueError(
                    f"messages[{index}].role 必须是 system、user 或 assistant，"
                    f"当前为 {role!r}"
                )
            if not isinstance(content, str):
                raise TypeError(f"messages[{index}].content 必须是字符串")
            normalized.append({"role": role, "content": content})
        if not normalized:
            raise ValueError("messages 不能为空")
    else:
        raise TypeError("messages 必须是字符串或消息字典列表")

    if system is not None:
        if not isinstance(system, str) or not system.strip():
            raise ValueError("system 不能为空")
        normalized.insert(0, {"role": "system", "content": system})

    return normalized


def _parse_completion(data: Any, *, fallback_model: str) -> ChatResult:
    """从 DeepSeek 的响应 JSON 中提取文本和元数据。"""
    if not isinstance(data, dict):
        raise DeepSeekError("DeepSeek 响应格式错误：顶层不是 JSON 对象")

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise DeepSeekError(
            "DeepSeek 响应中没有 choices："
            f"{json.dumps(data, ensure_ascii=False)[:300]}"
        )
    choice = choices[0]
    if not isinstance(choice, dict):
        raise DeepSeekError("DeepSeek 响应格式错误：choices[0] 不是 JSON 对象")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise DeepSeekError("DeepSeek 响应格式错误：缺少 message")

    content = message.get("content")
    if content is None:
        # 极少数情况（例如被截断）content 可能为空，按空字符串返回。
        content = ""
    if not isinstance(content, str):
        raise DeepSeekError("DeepSeek 响应格式错误：message.content 不是字符串")

    usage = data.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    finish_reason = choice.get("finish_reason")
    return ChatResult(
        content=content,
        model=str(data.get("model") or fallback_model),
        finish_reason=finish_reason if isinstance(finish_reason, str) else None,
        usage=usage,
    )


def _read_error_detail(exc: urllib.error.HTTPError) -> str:
    """尽量从错误响应中提取可读的错误信息。"""
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 读取响应体失败不应覆盖原始错误
        return str(exc.reason or "")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw[:500] or str(exc.reason or "")

    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if message:
                return str(message)
        elif isinstance(error, str) and error:
            return error
    return raw[:500] or str(exc.reason or "")


__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DEFAULT_TIMEOUT",
    "MODEL_ENV",
    "ChatResult",
    "DeepSeekAPIError",
    "DeepSeekClient",
    "DeepSeekError",
    "chat",
]
