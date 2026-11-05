import json
import unittest
from datetime import datetime
from typing import Sequence
from zoneinfo import ZoneInfo

from morning_report.github import GhCli, GhCliError, GitHubActivitySource
from morning_report.models import TimeWindow
from morning_report.sources import SourceError

TZ = ZoneInfo("Asia/Shanghai")


def window():
    return TimeWindow(
        start=datetime(2026, 11, 4, 11, 0, tzinfo=TZ),
        end=datetime(2026, 11, 5, 9, 0, tzinfo=TZ),
    )


def pr(
    number: int,
    *,
    title: str,
    author: str,
    created: str,
    merged: str = "",
    merged_by: str = "",
    state: str = "MERGED",
):
    return {
        "number": number,
        "title": title,
        "state": state,
        "url": f"https://github.com/o/r/pull/{number}",
        "author": {"login": author},
        "createdAt": created,
        "updatedAt": created,
        "mergedAt": merged,
        "mergedBy": {"login": merged_by} if merged_by else None,
    }


def commit(sha: str, *, author: str, name: str, date: str, message: str, parents=1):
    return {
        "sha": sha,
        "author": {"login": author} if author else None,
        "parents": [{"sha": f"p{i}"} for i in range(parents)],
        "commit": {
            "author": {"name": name, "date": date},
            "message": message,
        },
    }


class FakeGh:
    def __init__(self, prs=None, commits=None):
        self.prs = prs or []
        self.commits = commits or []
        self.calls = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(list(args))
        if args[0] == "pr":
            return json.dumps(self.prs, ensure_ascii=False)
        if args[0] == "api":
            return json.dumps(self.commits, ensure_ascii=False)
        raise AssertionError(f"unexpected args: {args}")


class GhCliTest(unittest.TestCase):
    def test_non_json_is_reported(self):
        cli = GhCli(runner=lambda args: "not json")
        with self.assertRaises(GhCliError):
            cli.run_json(["pr", "list"])


class GitHubActivitySourceTest(unittest.TestCase):
    def test_collects_prs_and_commits_with_author_labels(self):
        runner = FakeGh(
            prs=[
                pr(
                    3,
                    title="清理 README",
                    author="sunkunkun10360336-agent",
                    created="2026-11-04T08:34:35Z",
                    merged="2026-11-04T08:43:31Z",
                    merged_by="sunkunkun10360336",
                ),
                pr(
                    9,
                    title="窗口外的 PR",
                    author="someone",
                    created="2026-11-01T00:00:00Z",
                    merged="2026-11-01T00:00:00Z",
                ),
            ],
            commits=[
                commit(
                    "abc",
                    author="sunkunkun10360336-agent",
                    name="Agent",
                    date="2026-11-04T08:35:01Z",
                    message="实现晨报架构\n\n详细说明",
                ),
                commit(
                    "def",
                    author="",
                    name="本地作者",
                    date="2026-11-04T20:00:00Z",
                    message="人工提交",
                ),
                commit(
                    "merge",
                    author="sunkunkun10360336",
                    name="Human",
                    date="2026-11-04T20:30:00Z",
                    message="Merge pull request #9",
                    parents=2,
                ),
            ],
        )
        source = GitHubActivitySource(
            repo="o/r", agent_account="sunkunkun10360336-agent", runner=runner
        )

        items = source.collect(window())

        titles = [item.title for item in items]
        self.assertIn("PR #3 已合并：清理 README", titles)
        self.assertNotIn("PR #9 已合并：窗口外的 PR", titles)
        self.assertIn("提交：实现晨报架构", titles)
        self.assertIn("提交：人工提交", titles)
        self.assertNotIn("提交：Merge pull request #9", titles)

        by_title = {item.title: item for item in items}
        self.assertEqual(by_title["PR #3 已合并：清理 README"].source, "Agent 工作")
        self.assertEqual(by_title["提交：人工提交"].source, "人工工作")
        self.assertIn("合并人：sunkunkun10360336", by_title["PR #3 已合并：清理 README"].detail)

    def test_new_pr_is_reported(self):
        runner = FakeGh(
            prs=[
                pr(
                    4,
                    title="新功能",
                    author="sunkunkun10360336-agent",
                    created="2026-11-04T19:00:00Z",
                    state="OPEN",
                )
            ]
        )
        source = GitHubActivitySource(
            repo="o/r", agent_account="sunkunkun10360336-agent", runner=runner
        )
        items = source.collect(window())
        self.assertEqual([item.title for item in items], ["PR #4 有新提交：新功能"])

    def test_commit_query_uses_window(self):
        runner = FakeGh()
        GitHubActivitySource(repo="o/r", runner=runner).collect(window())
        api_call = runner.calls[1]
        self.assertEqual(api_call[0], "api")
        self.assertIn("since=2026-11-04T11%3A00%3A00%2B08%3A00", api_call)
        self.assertIn("until=2026-11-05T09%3A00%3A00%2B08%3A00", api_call)

    def test_gh_failure_becomes_source_error(self):
        source = GitHubActivitySource(
            repo="o/r", runner=lambda args: (_ for _ in ()).throw(RuntimeError("断网"))
        )
        with self.assertRaises(SourceError):
            source.collect(window())

    def test_missing_repo_is_rejected(self):
        source = GitHubActivitySource(repo="")
        with self.assertRaises(SourceError):
            source.collect(window())


if __name__ == "__main__":
    unittest.main()
