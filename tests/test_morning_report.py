import io
import json
import os
import tempfile
import unittest
from datetime import date, datetime, time, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from morning_report.app import MorningReportApp
from morning_report.config import (
    AppConfig,
    ConfigError,
    ReportConfig,
    ScheduleConfig,
    load_config,
)
from morning_report.delivery import (
    ConsoleSink,
    DeliveryError,
    FeishuWebhookSink,
    FileSink,
    MultiSink,
    build_sink,
    feishu_sign,
)
from morning_report.generator import (
    EMPTY_REPORT_BODY,
    GenerationError,
    MorningReportGenerator,
)
from morning_report.models import ContextItem, TimeWindow
from morning_report.pipeline import collect_items
from morning_report.render import format_date_zh, render_document
from morning_report.scheduler import next_run_at, parse_hhmm
from morning_report.sources import JsonFileSource, SourceError, StaticSource


TZ = ZoneInfo("Asia/Shanghai")


def moment(day=4, hour=8, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def make_window():
    return TimeWindow(start=moment(4, 8), end=moment(4, 20))


class FakeChat:
    def __init__(self, response="## 今日概览\n今天有一场项目周会。"):
        self.response = response
        self.calls = []

    def __call__(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class RecordingSink:
    name = "recording"

    def __init__(self):
        self.deliveries = []

    def deliver(self, report, *, report_date):
        self.deliveries.append((report_date, report))


class BrokenSink:
    name = "broken"

    def deliver(self, report, *, report_date):
        raise DeliveryError("通道不可用")


class BrokenSource:
    name = "broken-source"

    def collect(self, window):
        raise RuntimeError("来源不可用")


def make_app(items, *, chat=None, sink=None, now=None, config=None):
    config = config or AppConfig(
        base_dir=Path("."),
        schedule=ScheduleConfig(at=time(8, 0), timezone="Asia/Shanghai"),
        report=ReportConfig(user_name="测试用户"),
        sources=({"type": "json_file", "path": "unused.json"},),
        delivery=({"type": "console"},),
    )
    generator = MorningReportGenerator(
        chat_fn=chat or FakeChat(), temperature=0.4, max_chars=500, max_items=30
    )
    return MorningReportApp(
        config,
        sources=[StaticSource(items)],
        sinks=[sink or RecordingSink()],
        generator=generator,
        now_fn=lambda: now or moment(4, 8),
    )


class ContextSourceTest(unittest.TestCase):
    def test_json_source_filters_window_and_parses_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.json"
            path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "title": "窗口内的事项",
                                "source": "日历",
                                "detail": "带上材料",
                                "happened_at": "2026-11-04T09:30:00+08:00",
                                "priority": 3,
                                "tags": ["会议", "重要"],
                            },
                            {
                                "title": "窗口外的事项",
                                "happened_at": "2026-11-02T09:00:00+08:00",
                            },
                            {"title": "没有时间的事项"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            items = JsonFileSource(path=path).collect(make_window())

        self.assertEqual([item.title for item in items], ["窗口内的事项", "没有时间的事项"])
        first = items[0]
        self.assertEqual(first.source, "日历")
        self.assertEqual(first.priority, 3)
        self.assertEqual(first.tags, ("会议", "重要"))
        self.assertIsNotNone(first.happened_at)

    def test_json_source_accepts_top_level_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.json"
            path.write_text(json.dumps([{"title": "直接数组"}]), encoding="utf-8")
            items = JsonFileSource(path=path).collect(make_window())
        self.assertEqual(items[0].title, "直接数组")

    def test_json_source_reports_invalid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.json"
            path.write_text("not json", encoding="utf-8")
            with self.assertRaises(SourceError):
                JsonFileSource(path=path).collect(make_window())

    def test_json_source_requires_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.json"
            path.write_text(json.dumps([{"detail": "没有标题"}]), encoding="utf-8")
            with self.assertRaises(SourceError):
                JsonFileSource(path=path).collect(make_window())

    def test_build_source_resolves_relative_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context.json"
            path.write_text(json.dumps([]), encoding="utf-8")
            source = build_source_for_test(path)
        self.assertEqual(source.path, path.resolve())

    def test_static_source_filters_window(self):
        items = [
            ContextItem(title="进窗口", happened_at=moment(4, 9)),
            ContextItem(title="出窗口", happened_at=moment(4, 21)),
        ]
        collected = StaticSource(items).collect(make_window())
        self.assertEqual([item.title for item in collected], ["进窗口"])


def build_source_for_test(path):
    from morning_report.sources import build_source

    return build_source(
        {"type": "json_file", "path": path.name}, base_dir=path.parent
    )


class PipelineTest(unittest.TestCase):
    def test_dedupes_sort_and_truncates(self):
        items = [
            ContextItem(title="低优先级", priority=0, happened_at=moment(4, 9)),
            ContextItem(title="高优先级-早", priority=3, happened_at=moment(4, 10)),
            ContextItem(title="高优先级-晚", priority=3, happened_at=moment(4, 12)),
            ContextItem(title="高优先级-晚", priority=3, happened_at=moment(4, 12)),
        ]
        collected = collect_items([StaticSource(items)], make_window(), max_items=2)
        self.assertEqual([item.title for item in collected], ["高优先级-晚", "高优先级-早"])

    def test_single_source_failure_is_tolerated(self):
        items = [ContextItem(title="仍然可用", happened_at=moment(4, 9))]
        collected = collect_items(
            [BrokenSource(), StaticSource(items)], make_window()
        )
        self.assertEqual([item.title for item in collected], ["仍然可用"])

    def test_all_sources_failing_raises(self):
        with self.assertRaises(SourceError):
            collect_items([BrokenSource()], make_window())


class SchedulerTest(unittest.TestCase):
    def test_parse_hhmm(self):
        self.assertEqual(parse_hhmm("08:05"), time(8, 5))
        self.assertEqual(parse_hhmm("23:59"), time(23, 59))

    def test_parse_hhmm_rejects_invalid(self):
        for value in ("8", "24:00", "08:60", "abc"):
            with self.assertRaises(ValueError):
                parse_hhmm(value)

    def test_next_run_same_day(self):
        target = next_run_at(moment(4, 7), time(8, 0), TZ)
        self.assertEqual(target, moment(4, 8))

    def test_next_run_rolls_to_tomorrow(self):
        target = next_run_at(moment(4, 9), time(8, 0), TZ)
        self.assertEqual(target, moment(5, 8))

    def test_next_run_converts_timezone(self):
        now_utc = datetime(2026, 11, 3, 23, 0, tzinfo=timezone.utc)  # 上海 11-04 07:00
        target = next_run_at(now_utc, time(8, 0), TZ)
        self.assertEqual(target, moment(4, 8))


class GeneratorTest(unittest.TestCase):
    def setUp(self):
        self.items = [
            ContextItem(
                title="项目周会",
                source="日历",
                detail="讨论晨报方案",
                happened_at=moment(4, 10),
                priority=3,
                tags=("会议",),
            )
        ]
        self.window = make_window()

    def test_build_messages_contains_context(self):
        generator = MorningReportGenerator(chat_fn=FakeChat())
        messages = generator.build_messages(
            self.items, date(2026, 11, 4), self.window, user_name="张三"
        )
        self.assertEqual(messages[0]["role"], "system")
        user = messages[1]["content"]
        self.assertIn("项目周会", user)
        self.assertIn("讨论晨报方案", user)
        self.assertIn("张三", user)
        self.assertIn("2026年11月4日 星期三", user)

    def test_generate_calls_chat_and_strips_code_fence(self):
        chat = FakeChat("```markdown\n## 今日概览\n示例内容\n```")
        generator = MorningReportGenerator(chat_fn=chat, temperature=0.2)
        body = generator.generate(self.items, date(2026, 11, 4), self.window)
        self.assertEqual(body, "## 今日概览\n示例内容")
        self.assertEqual(chat.calls[0][1]["temperature"], 0.2)

    def test_generate_wraps_chat_error(self):
        generator = MorningReportGenerator(chat_fn=FakeChat(RuntimeError("网络异常")))
        with self.assertRaises(GenerationError):
            generator.generate(self.items, date(2026, 11, 4), self.window)

    def test_generate_rejects_empty_response(self):
        generator = MorningReportGenerator(chat_fn=FakeChat("   "))
        with self.assertRaises(GenerationError):
            generator.generate(self.items, date(2026, 11, 4), self.window)


class DeliveryTest(unittest.TestCase):
    def test_file_sink_writes_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = FileSink(directory=Path(tmp))
            sink.deliver("# 晨报", report_date=date(2026, 11, 4))
            target = Path(tmp) / "morning-report-2026-11-04.md"
            self.assertEqual(target.read_text(encoding="utf-8"), "# 晨报")

    def test_console_sink_writes_to_stream(self):
        stream = io.StringIO()
        ConsoleSink(stream=stream).deliver("内容", report_date=date(2026, 11, 4))
        self.assertEqual(stream.getvalue(), "内容\n")

    def test_feishu_payload_with_signature(self):
        sink = FeishuWebhookSink(url="https://example.test/hook", secret="s3cret")
        payload = sink.build_payload("晨报内容", timestamp=1700000000)
        self.assertEqual(payload["msg_type"], "text")
        self.assertEqual(payload["content"]["text"], "晨报内容")
        self.assertEqual(payload["timestamp"], "1700000000")
        self.assertEqual(payload["sign"], feishu_sign(1700000000, "s3cret"))

    def test_feishu_deliver_posts_json(self):
        calls = []

        def transport(url, body, timeout):
            calls.append((url, json.loads(body), timeout))
            return {"code": 0, "msg": "success"}

        sink = FeishuWebhookSink(
            url="https://example.test/hook", transport=transport, timeout=5
        )
        sink.deliver("晨报", report_date=date(2026, 11, 4))
        self.assertEqual(calls[0][0], "https://example.test/hook")
        self.assertEqual(calls[0][1]["msg_type"], "text")
        self.assertEqual(calls[0][2], 5)

    def test_feishu_deliver_raises_on_error_code(self):
        sink = FeishuWebhookSink(
            url="https://example.test/hook",
            transport=lambda url, body, timeout: {"code": 9499, "msg": "bad"},
        )
        with self.assertRaises(DeliveryError):
            sink.deliver("晨报", report_date=date(2026, 11, 4))

    def test_multi_sink_tolerates_partial_failure(self):
        recording = RecordingSink()
        delivered = MultiSink([BrokenSink(), recording]).deliver(
            "晨报", report_date=date(2026, 11, 4)
        )
        self.assertEqual(delivered, ("recording",))
        self.assertEqual(len(recording.deliveries), 1)

    def test_multi_sink_raises_when_all_fail(self):
        with self.assertRaises(DeliveryError):
            MultiSink([BrokenSink(), BrokenSink()]).deliver(
                "晨报", report_date=date(2026, 11, 4)
            )

    def test_build_sink_resolves_file_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            sink = build_sink({"type": "file", "directory": "reports"}, base_dir=Path(tmp))
        self.assertEqual(sink.directory, (Path(tmp) / "reports").resolve())


class ConfigTest(unittest.TestCase):
    def _write(self, tmp, data):
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_load_minimal_config_with_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                {
                    "sources": [{"type": "json_file", "path": "context.json"}],
                },
            )
            config = load_config(path)
        self.assertEqual(config.schedule.at, time(8, 0))
        self.assertEqual(config.schedule.timezone, "Asia/Shanghai")
        self.assertEqual(config.report.lookback_hours, 24.0)
        self.assertEqual(config.delivery, ({"type": "console"},))

    def test_load_config_expands_environment_variables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                {
                    "sources": [{"type": "json_file", "path": "context.json"}],
                    "report": {"model": "${TEST_REPORT_MODEL}"},
                },
            )
            with mock.patch.dict(os.environ, {"TEST_REPORT_MODEL": "deepseek-reasoner"}):
                config = load_config(path)
        self.assertEqual(config.report.model, "deepseek-reasoner")

    def test_missing_environment_variable_reports_config_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                {
                    "sources": [{"type": "json_file", "path": "context.json"}],
                    "report": {"model": "${TEST_MISSING_VAR}"},
                },
            )
            os.environ.pop("TEST_MISSING_VAR", None)
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_invalid_timezone_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                {
                    "timezone": "Mars/Base",
                    "sources": [{"type": "json_file", "path": "context.json"}],
                },
            )
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_sources_cannot_be_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, {"sources": []})
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_invalid_schedule_time_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp,
                {
                    "schedule": {"at": "25:00"},
                    "sources": [{"type": "json_file", "path": "context.json"}],
                },
            )
            with self.assertRaises(ConfigError):
                load_config(path)


class AppTest(unittest.TestCase):
    def test_run_generates_and_delivers(self):
        items = [
            ContextItem(title="项目周会", happened_at=moment(4, 9), priority=3),
            ContextItem(title="晨跑", happened_at=moment(4, 7)),
        ]
        sink = RecordingSink()
        chat = FakeChat()
        app = make_app(items, chat=chat, sink=sink)

        run = app.run(now=moment(4, 8))

        self.assertEqual(run.report_date, date(2026, 11, 4))
        self.assertEqual(len(run.items), 2)
        self.assertEqual(run.items[0].title, "项目周会")
        self.assertTrue(run.generated)
        self.assertIn("今天有一场项目周会", run.content)
        self.assertTrue(run.content.startswith("# 每日晨报 · 2026年11月4日 星期三"))
        self.assertEqual(run.delivered, ("recording",))
        self.assertEqual(len(sink.deliveries), 1)

    def test_dry_run_does_not_call_model_or_deliver(self):
        items = [ContextItem(title="项目周会", happened_at=moment(4, 9))]
        sink = RecordingSink()
        chat = FakeChat()
        run = make_app(items, chat=chat, sink=sink).run(
            now=moment(4, 8), dry_run=True
        )
        self.assertEqual(chat.calls, [])
        self.assertEqual(sink.deliveries, [])
        self.assertIn("项目周会", run.prompt)
        self.assertEqual(run.content, "")

    def test_empty_context_uses_fallback_without_calling_model(self):
        sink = RecordingSink()
        chat = FakeChat()
        run = make_app([], chat=chat, sink=sink).run(now=moment(4, 8))
        self.assertFalse(run.generated)
        self.assertIn(EMPTY_REPORT_BODY, run.content)
        self.assertEqual(chat.calls, [])
        self.assertEqual(run.delivered, ("recording",))

    def test_window_respects_lookback_and_lookahead(self):
        items = [
            ContextItem(title="昨天上午", happened_at=moment(3, 9)),
            ContextItem(title="今天上午", happened_at=moment(4, 9)),
            ContextItem(title="明天凌晨", happened_at=moment(5, 1)),
            ContextItem(title="明天晚上", happened_at=moment(5, 21)),
        ]
        run = make_app(items).run(now=moment(4, 8))
        titles = [item.title for item in run.items]
        self.assertIn("昨天上午", titles)
        self.assertIn("今天上午", titles)
        self.assertNotIn("明天凌晨", titles)
        self.assertNotIn("明天晚上", titles)


class RenderTest(unittest.TestCase):
    def test_format_date_zh(self):
        self.assertEqual(format_date_zh(date(2026, 11, 4)), "2026年11月4日 星期三")

    def test_render_document_has_title_and_footer(self):
        document = render_document(date(2026, 11, 4), "## 今日概览\n内容")
        self.assertTrue(document.startswith("# 每日晨报 · 2026年11月4日 星期三"))
        self.assertIn("## 今日概览\n内容", document)
        self.assertIn("本晨报由 PCE 根据个人上下文自动生成。", document)


class CliTest(unittest.TestCase):
    def test_invalid_config_returns_config_error(self):
        from morning_report.cli import main

        code = main(["--once", "--config", "/nonexistent/morning-report.json"])
        self.assertEqual(code, 2)

    def test_dry_run_survives_missing_source_file(self):
        from morning_report.cli import main

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "sources": [
                            {"type": "json_file", "path": "missing-context.json"}
                        ],
                        "delivery": [{"type": "console"}],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                code = main(["--dry-run", "--config", str(config_path)])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
