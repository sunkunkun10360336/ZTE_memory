"""晨报生成：构造提示词并调用 DeepSeek。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Mapping, Optional, Sequence

from .models import ContextItem, TimeWindow
from .render import format_date_zh

#: 没有上下文时使用的兜底正文，避免为一条空晨报调用模型。
EMPTY_REPORT_BODY = "今日暂无需要特别关注的信息，祝你一天顺利。"

SYSTEM_PROMPT = """\
你是「PCE 个人上下文引擎」的晨报编辑。请根据提供的上下文条目，为使用者撰写一份简洁的今日晨报。

硬性要求：
1. 只使用上下文条目中的事实，不得编造、不得补充外部信息；信息不足时就省略。
2. 使用简体中文，语气具体、克制，不写空泛的套话。
3. 只输出 Markdown 正文，按需要选用以下二级标题，没有内容的章节直接省略：
   ## 今日概览
   ## 重点事项
   ## 待办与提醒
   ## 关注动态
   ## 一句话建议
4. 全文不超过 {max_chars} 字，最多展开 {max_items} 条上下文。
5. 不要输出一级标题、日期标题、代码块或“以下是晨报”之类的开场白。"""

#: 群聊速报使用的提示词：总结群内讨论与双方工作。
GROUP_SUMMARY_SYSTEM_PROMPT = """\
你是「PCE 个人上下文引擎」的群内速报编辑。请根据提供的上下文条目，为群成员撰写一份简短的中文速报。

硬性要求：
1. 只使用上下文条目中的事实，不得编造、不得补充外部信息；信息不足时就省略。
2. 使用简体中文，语气具体、克制，不写空泛的套话。
3. 优先总结群内讨论：谁提出了什么、讨论了什么、结论或待办是什么。省略寒暄、无意义的测试消息（如单个数字、表情）和与工作无关的闲谈。
4. 再总结工作动态，区分「人工成员」和「Agent」各自完成或推进了什么；没有工作动态时省略这一部分。
5. 不要复述机票、票号、证件号、手机号、住址等敏感个人信息；遇到时用一句概括或直接省略。
6. 按需要选用以下二级标题，没有内容的章节直接省略：
   ## 群内讨论
   ## 工作动态
   内容很少时可以合并成一段，不强制使用标题。
7. 全文不超过 {max_chars} 字，最多展开 {max_items} 条上下文。
8. 只输出 Markdown 正文，不要输出一级标题、日期标题、代码块或“以下是速报”之类的开场白。"""


class GenerationError(RuntimeError):
    """调用模型或解析模型输出失败。"""


ChatFunction = Callable[..., str]


class MorningReportGenerator:
    """把上下文整理成提示词，调用 DeepSeek 并返回晨报正文。

    ``chat_fn`` 默认使用仓库根目录的 :func:`LLM.chat`，也可以在测试或
    其他模型中注入自定义实现（签名与 ``LLM.chat`` 一致）。
    """

    def __init__(
        self,
        chat_fn: Optional[ChatFunction] = None,
        *,
        temperature: float = 0.4,
        max_chars: int = 500,
        max_items: int = 30,
        max_tokens: int = 1500,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
    ) -> None:
        if not 0 <= temperature <= 2:
            raise ValueError("temperature 必须在 0 到 2 之间")
        if max_chars <= 0 or max_items <= 0 or max_tokens <= 0:
            raise ValueError("max_chars、max_items、max_tokens 必须大于 0")
        if system_prompt is not None and not system_prompt.strip():
            raise ValueError("system_prompt 不能为空")
        self._chat_fn = chat_fn
        self.temperature = temperature
        self.max_chars = max_chars
        self.max_items = max_items
        self.max_tokens = max_tokens
        self.model = model
        self.system_prompt = system_prompt

    def build_messages(
        self,
        items: Sequence[ContextItem],
        report_date: date,
        window: TimeWindow,
        *,
        user_name: str = "",
    ) -> list[dict[str, str]]:
        """构造 DeepSeek 的 ``messages``，独立出来便于 dry-run 和测试。"""
        system = (self.system_prompt or SYSTEM_PROMPT)
        system = system.replace("{max_chars}", str(self.max_chars)).replace(
            "{max_items}", str(self.max_items)
        )
        lines = [
            f"日期：{format_date_zh(report_date)}",
            f"上下文时间范围：{_format_moment(window.start)} 至 {_format_moment(window.end)}",
        ]
        if user_name:
            lines.append(f"使用者：{user_name}")
        lines.append(f"上下文条目（共 {len(items)} 条）：")
        if items:
            for index, item in enumerate(items, 1):
                lines.extend(self._format_item(index, item))
        else:
            lines.append("（无）")
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(lines)},
        ]

    def generate(
        self,
        items: Sequence[ContextItem],
        report_date: date,
        window: TimeWindow,
        *,
        user_name: str = "",
        messages: Optional[Sequence[Mapping[str, str]]] = None,
    ) -> str:
        """生成晨报正文；``messages`` 不为空时直接复用，避免重复构造。"""
        prompt = (
            list(messages)
            if messages is not None
            else self.build_messages(items, report_date, window, user_name=user_name)
        )
        try:
            raw = self._complete(prompt)
        except Exception as exc:  # noqa: BLE001 - 统一包装为生成失败
            raise GenerationError(f"晨报生成失败：{exc}") from exc

        if not isinstance(raw, str):
            raise GenerationError("模型返回内容不是文本")
        body = _strip_code_fence(raw).strip()
        if not body:
            raise GenerationError("模型返回了空晨报")
        return body

    def _complete(self, messages: Sequence[Mapping[str, str]]) -> str:
        if self._chat_fn is not None:
            return self._chat_fn(
                list(messages),
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                model=self.model,
            )
        from LLM import chat  # 延迟导入，避免无 API Key 时影响包导入

        return chat(
            list(messages),
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            model=self.model,
        )

    def _format_item(self, index: int, item: ContextItem) -> list[str]:
        header = f"{index}. "
        if item.source:
            header += f"[{item.source}] "
        header += item.title
        if item.happened_at is not None:
            header += f"（{_format_moment(item.happened_at)}）"
        lines = [header]
        if item.detail:
            lines.append(f"   说明：{item.detail}")
        if item.tags:
            lines.append(f"   标签：{'、'.join(item.tags)}")
        if item.url:
            lines.append(f"   链接：{item.url}")
        return lines


def _format_moment(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def _strip_code_fence(text: str) -> str:
    """去掉模型偶尔加上的三重反引号代码块。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


__all__ = [
    "EMPTY_REPORT_BODY",
    "GROUP_SUMMARY_SYSTEM_PROMPT",
    "GenerationError",
    "MorningReportGenerator",
    "SYSTEM_PROMPT",
]
