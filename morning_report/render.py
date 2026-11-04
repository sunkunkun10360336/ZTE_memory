"""晨报文本渲染工具。"""

from __future__ import annotations

from datetime import date
from typing import Mapping, Sequence

WEEKDAY_NAMES = "一二三四五六日"


def format_date_zh(value: date) -> str:
    """把日期格式化为中文，例如 ``2026年11月4日 星期三``。"""
    weekday = WEEKDAY_NAMES[value.weekday()]
    return f"{value.year}年{value.month}月{value.day}日 星期{weekday}"


def render_document(report_date: date, body: str) -> str:
    """在模型生成的正文外层加上固定标题和落款。"""
    content = body.strip()
    return (
        f"# 每日晨报 · {format_date_zh(report_date)}\n\n"
        f"{content}\n\n"
        "---\n\n"
        "*本晨报由 PCE 根据个人上下文自动生成。*\n"
    )


def messages_to_text(messages: Sequence[Mapping[str, str]]) -> str:
    """把提示词消息列表渲染成可读文本，供 ``--dry-run`` 查看。"""
    return "\n\n".join(f"[{message['role']}]\n{message['content']}" for message in messages)
