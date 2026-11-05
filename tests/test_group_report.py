import json
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from morning_report.app import MorningReportApp
from morning_report.config import (
    AppConfig,
    ConfigError,
    ReportConfig,
    ScheduleConfig,
    load_config,
)
from morning_report.generator import GROUP_SUMMARY_SYSTEM_PROMPT, MorningReportGenerator
from morning_report.models import ContextItem
from morning_report.render import render_document
from morning_report.scheduler import IntervalScheduler

TZ = ZoneInfo("Asia/Shanghai")


def moment(hour, minute=0):
    return datetime(2026, 11, 5, hour, minute, tzinfo=TZ)


class FixedSource:
    name = "fixed"

    def __init__(self, items):
        self.items = items
        self.windows = []

    def collect(self, window):
        self.windows.append(window)
        return list(self.items)


class RecordingSink:
    name = "recording"

    def __init__(self):
        self.deliveries = []

    def deliver(self, report, *, report_date):
        self.deliveries.append((report_date, report))


class FakeChat:
    response = "## 群内讨论\n大家确认了晨报方案。"

    def __init__(self):
        self.calls = []

    def __call__(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.response


def make_config(tmp: Path, **report_overrides):
    report = ReportConfig(
        lookback_hours=24,
        lookahead_hours=0,
        title="群内速报",
        footer="本速报自动生成。",
        empty_body="暂无新动态。",
        **report_overrides,
    )
    return AppConfig(
        base_dir=tmp,
        schedule=ScheduleConfig(interval_minutes=5, timezone="Asia/Shanghai"),
        report=report,
        sources=({"type": "console"},),
        delivery=({"type": "console"},),
        state_path=tmp / "state.json",
    )


def make_app(tmp, items, **report_overrides):
    sink = RecordingSink()
    chat = FakeChat()
    app = MorningReportApp(
        make_config(tmp, **report_overrides),
        sources=[FixedSource(items)],
        sinks=[sink],
        generator=MorningReportGenerator(chat_fn=chat),
        now_fn=lambda: moment(10, 5),
    )
    return app, sink, chat


class IncrementalAppTest(unittest.TestCase):
    def test_first_run_uses_lookback_then_state_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = [
                ContextItem(title="A", happened_at=moment(10, 0)),
                ContextItem(title="B", happened_at=moment(10, 1)),
            ]
            app, sink, chat = make_app(Path(tmp), items)

            first = app.run(now=moment(10, 5))
            self.assertEqual(len(first.items), 2)
            self.assertEqual(len(sink.deliveries), 1)
            self.assertIn("群内速报", sink.deliveries[0][1])
            self.assertIn("本速报自动生成。", sink.deliveries[0][1])
            self.assertEqual(first.window.start, datetime(2026, 11, 4, 10, 5, tzinfo=TZ))

            state = json.loads((Path(tmp) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["last_end"], moment(10, 5).isoformat())
            self.assertEqual(len(state["seen"]), 2)

            second = app.run(now=moment(10, 10))
            self.assertEqual(second.window.start, moment(10, 3))
            self.assertEqual(second.items, ())
            self.assertEqual(len(state["seen"]), 2)

    def test_only_new_items_are_reported_and_repeated_empty_run_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            items = [
                ContextItem(title="A", happened_at=moment(10, 0)),
                ContextItem(title="B", happened_at=moment(10, 1)),
            ]
            app, sink, chat = make_app(Path(tmp), items)
            app.run(now=moment(10, 5))

            items.append(ContextItem(title="C", happened_at=moment(10, 7)))
            second = app.run(now=moment(10, 10))
            self.assertEqual([item.title for item in second.items], ["C"])
            self.assertTrue(second.generated)

            third = app.run(now=moment(10, 15))
            self.assertEqual(third.items, ())
            self.assertFalse(third.generated)
            self.assertIn("暂无新动态。", third.content)
            self.assertEqual(len(chat.calls), 2)

    def test_dry_run_does_not_advance_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            app, sink, _ = make_app(
                Path(tmp), [ContextItem(title="A", happened_at=moment(10, 0))]
            )
            app.run(now=moment(10, 5))
            before = (Path(tmp) / "state.json").read_text(encoding="utf-8")

            app.run(now=moment(10, 10), dry_run=True)
            after = (Path(tmp) / "state.json").read_text(encoding="utf-8")
            self.assertEqual(before, after)

    def test_no_state_file_keeps_legacy_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp))
            config = AppConfig(
                base_dir=config.base_dir,
                schedule=config.schedule,
                report=config.report,
                sources=config.sources,
                delivery=config.delivery,
                state_path=None,
            )
            app = MorningReportApp(
                config,
                sources=[FixedSource([])],
                sinks=[RecordingSink()],
                generator=MorningReportGenerator(chat_fn=FakeChat()),
            )
            window = app.build_window(moment(10, 5))
            self.assertEqual(window.start, datetime(2026, 11, 4, 10, 5, tzinfo=TZ))
            state_file = Path(tmp) / "state.json"
            app.run(now=moment(10, 5))
            self.assertFalse(state_file.exists())

    def test_group_profile_uses_group_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = make_config(Path(tmp), profile="group")
            app = MorningReportApp(config, sources=[FixedSource([])], sinks=[RecordingSink()])
            self.assertEqual(app.generator.system_prompt, GROUP_SUMMARY_SYSTEM_PROMPT)


class RenderCustomizationTest(unittest.TestCase):
    def test_custom_title_and_empty_footer(self):
        document = render_document(
            date(2026, 11, 5), "内容", title="群内速报", footer=""
        )
        self.assertTrue(document.startswith("# 群内速报 · 2026年11月5日 星期四"))
        self.assertNotIn("---", document)


class IntervalSchedulerTest(unittest.TestCase):
    def test_rejects_invalid_interval(self):
        with self.assertRaises(ValueError):
            IntervalScheduler(0)
        with self.assertRaises(ValueError):
            IntervalScheduler(-1)
        with self.assertRaises(ValueError):
            IntervalScheduler(True)


class ConfigGroupTest(unittest.TestCase):
    def _load(self, tmp, data):
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return load_config(path)

    def test_interval_schedule_and_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._load(
                tmp,
                {
                    "schedule": {"interval_minutes": 5},
                    "state_file": "state/run.json",
                    "sources": [{"type": "console"}],
                },
            )
        self.assertEqual(config.schedule.interval_minutes, 5)
        self.assertIsNone(config.schedule.at)
        self.assertEqual(config.state_path, (Path(tmp) / "state/run.json").resolve())

    def test_at_and_interval_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                self._load(
                    tmp,
                    {
                        "schedule": {"at": "09:00", "interval_minutes": 5},
                        "sources": [{"type": "console"}],
                    },
                )

    def test_invalid_interval_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                self._load(
                    tmp,
                    {
                        "schedule": {"interval_minutes": 0},
                        "sources": [{"type": "console"}],
                    },
                )

    def test_group_profile_and_text_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._load(
                tmp,
                {
                    "report": {
                        "profile": "group",
                        "title": "群内速报",
                        "footer": "自动生成",
                        "empty_body": "暂无",
                    },
                    "sources": [{"type": "console"}],
                },
            )
        self.assertEqual(config.report.profile, "group")
        self.assertEqual(config.report.title, "群内速报")
        self.assertEqual(config.report.footer, "自动生成")
        self.assertEqual(config.report.empty_body, "暂无")

    def test_invalid_profile_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ConfigError):
                self._load(
                    tmp,
                    {
                        "report": {"profile": "unknown"},
                        "sources": [{"type": "console"}],
                    },
                )

    def test_build_sources_accepts_feishu_and_github(self):
        from morning_report.feishu import FeishuChatSource
        from morning_report.github import GitHubActivitySource
        from morning_report.sources import build_source

        with tempfile.TemporaryDirectory() as tmp:
            feishu = build_source(
                {"type": "feishu_chat", "chat_id": "oc_test"}, base_dir=Path(tmp)
            )
            github = build_source(
                {"type": "github_activity", "repo": "o/r"}, base_dir=Path(tmp)
            )
        self.assertIsInstance(feishu, FeishuChatSource)
        self.assertIsInstance(github, GitHubActivitySource)


if __name__ == "__main__":
    unittest.main()
