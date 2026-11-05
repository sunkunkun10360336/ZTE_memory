"""GitHub 动态来源。

通过 ``gh`` 读取仓库在时间窗口内产生的 Pull Request 与提交，按作者区分
「人工工作」和「Agent 工作」，作为群内晨报的上下文。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import quote

from .models import ContextItem, TimeWindow
from .sources import SourceError

GhRunner = Callable[[Sequence[str]], str]

#: 指向仓库或 PR 的链接，方便晨报里保留出处。
GITHUB_URL = "https://github.com"


class GhCliError(RuntimeError):
    """调用 gh 失败。"""


@dataclass
class GhCli:
    """``gh`` 的轻量 JSON 封装。"""

    executable: str = "gh"
    timeout: float = 30.0
    runner: Optional[GhRunner] = field(default=None, repr=False, compare=False)

    def run_json(self, args: Sequence[str]) -> Any:
        raw = self._run(list(args))
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GhCliError(f"gh 返回了非 JSON 响应：{raw[:200]}") from exc

    def _run(self, args: Sequence[str]) -> str:
        if self.runner is not None:
            try:
                return self.runner(args)
            except GhCliError:
                raise
            except Exception as exc:  # noqa: BLE001 - 注入的 runner 失败统一包装
                raise GhCliError(f"gh 调用失败：{exc}") from exc
        command = [self.executable, *args]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise GhCliError(f"找不到 gh 可执行文件：{self.executable}") from exc
        except subprocess.TimeoutExpired as exc:
            raise GhCliError(f"gh 执行超时（{self.timeout:.0f} 秒）") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:500]
            raise GhCliError(f"gh 执行失败（退出码 {completed.returncode}）：{detail}")
        return completed.stdout


@dataclass
class GitHubActivitySource:
    """收集窗口内的 PR 与默认分支提交。"""

    repo: str
    agent_account: str = ""
    name: str = "github_activity"
    source_label: str = "工作"
    agent_label: str = "Agent 工作"
    human_label: str = "人工工作"
    cli_path: str = "gh"
    timeout: float = 30.0
    max_prs: int = 30
    max_commits: int = 30
    include_commits: bool = True
    runner: Optional[GhRunner] = field(default=None, repr=False, compare=False)

    def collect(self, window: TimeWindow) -> list[ContextItem]:
        if not self.repo.strip():
            raise SourceError("GitHub 来源需要非空 repo")
        cli = GhCli(self.cli_path, self.timeout, self.runner)
        items: list[ContextItem] = []
        try:
            items.extend(self._pull_requests(cli, window))
            if self.include_commits:
                items.extend(self._commits(cli, window))
        except GhCliError as exc:
            raise SourceError(str(exc)) from exc
        items.sort(key=lambda item: item.happened_at or window.start)
        return items

    def _pull_requests(self, cli: GhCli, window: TimeWindow) -> list[ContextItem]:
        payload = cli.run_json(
            [
                "pr",
                "list",
                "--repo",
                self.repo,
                "--state",
                "all",
                "--limit",
                str(self.max_prs),
                "--json",
                "number,title,state,url,author,createdAt,updatedAt,mergedAt,mergedBy",
            ]
        )
        if not isinstance(payload, list):
            raise SourceError("gh pr list 返回格式错误")

        items: list[ContextItem] = []
        for raw in payload:
            if not isinstance(raw, Mapping):
                continue
            created = _parse_github_time(raw.get("createdAt"))
            merged = _parse_github_time(raw.get("mergedAt"))
            created_in = created is not None and window.contains(created)
            merged_in = merged is not None and window.contains(merged)
            if not created_in and not merged_in:
                continue

            number = raw.get("number")
            title = str(raw.get("title") or "").strip()
            author = _login(raw.get("author"))
            merged_by = _login(raw.get("mergedBy"))
            if merged_in:
                action = "已合并"
                happened_at = merged
                priority = 3
            else:
                action = "有新提交"
                happened_at = created
                priority = 2

            detail_parts = [f"发起人：{author or '未知'}"]
            if merged_by:
                detail_parts.append(f"合并人：{merged_by}")
            detail_parts.append(f"状态：{raw.get('state') or 'UNKNOWN'}")
            items.append(
                ContextItem(
                    title=f"PR #{number} {action}：{title}",
                    source=self._label_for(author),
                    detail="；".join(detail_parts),
                    happened_at=happened_at,
                    priority=priority,
                    tags=("GitHub", "PR"),
                    url=str(raw.get("url") or ""),
                )
            )
        return items

    def _commits(self, cli: GhCli, window: TimeWindow) -> list[ContextItem]:
        payload = cli.run_json(
            [
                "api",
                f"repos/{self.repo}/commits",
                "-X",
                "GET",
                "-f",
                f"since={quote(window.start.isoformat())}",
                "-f",
                f"until={quote(window.end.isoformat())}",
                "-f",
                f"per_page={self.max_commits}",
            ]
        )
        if not isinstance(payload, list):
            raise SourceError("gh api commits 返回格式错误")

        items: list[ContextItem] = []
        for raw in payload:
            if not isinstance(raw, Mapping):
                continue
            parents = raw.get("parents")
            if isinstance(parents, list) and len(parents) > 1:
                continue  # 合并提交由 PR 事件表达，避免重复
            commit = raw.get("commit")
            if not isinstance(commit, Mapping):
                continue
            author = _login(raw.get("author"))
            author_info = commit.get("author") if isinstance(commit.get("author"), Mapping) else {}
            author_name = str(author_info.get("name") or author or "未知")
            happened_at = _parse_github_time(author_info.get("date"))
            if happened_at is None or not window.contains(happened_at):
                continue
            message = str(commit.get("message") or "").splitlines()
            subject = message[0].strip() if message else ""
            if subject.startswith("Merge pull request"):
                continue
            sha = str(raw.get("sha") or "")
            items.append(
                ContextItem(
                    title=f"提交：{_truncate(subject, 80)}",
                    source=self._label_for(author),
                    detail=f"作者：{author_name}；提交时间：{happened_at.strftime('%Y-%m-%d %H:%M')}",
                    happened_at=happened_at,
                    priority=2,
                    tags=("GitHub", "提交"),
                    url=f"{GITHUB_URL}/{self.repo}/commit/{sha}" if sha else "",
                )
            )
        return items

    def _label_for(self, login: str) -> str:
        if self.agent_account and login == self.agent_account:
            return self.agent_label
        return self.human_label


def _login(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("login") or "")
    return ""


def _parse_github_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = [
    "GhCli",
    "GhCliError",
    "GitHubActivitySource",
]
