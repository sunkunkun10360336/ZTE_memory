import unittest
from datetime import datetime
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from morning_report.delivery import DeliveryError
from morning_report.feishu import (
    FeishuChatSink,
    FeishuChatSource,
    LarkCli,
    LarkCliError,
)
from morning_report.models import TimeWindow
from morning_report.sources import SourceError

TZ = ZoneInfo("Asia/Shanghai")


def window():
    return TimeWindow(
        start=datetime(2026, 11, 5, 10, 0, tzinfo=TZ),
        end=datetime(2026, 11, 5, 11, 0, tzinfo=TZ),
    )


def message(
    content: str,
    *,
    sender: str = "用户444625",
    sender_type: str = "user",
    create_time: str = "2026-11-05 10:30",
    msg_type: str = "text",
    deleted: bool = False,
):
    return {
        "chat_id": "oc_test",
        "content": content,
        "create_time": create_time,
        "deleted": deleted,
        "message_app_link": "https://example.test/message/1",
        "msg_type": msg_type,
        "sender": {
            "id": "ou_test",
            "name": sender,
            "sender_type": sender_type,
        },
    }


class FakeLark:
    def __init__(self, payload=None, error: Optional[Exception] = None):
        self.payload = payload
        self.error = error
        self.calls = []

    def __call__(self, args: Sequence[str], stdin: Optional[str]) -> str:
        self.calls.append((list(args), stdin))
        if self.error is not None:
            raise self.error
        import json

        return json.dumps(self.payload, ensure_ascii=False)


class LarkCliTest(unittest.TestCase):
    def test_run_json_reports_error_payload(self):
        cli = LarkCli(
            runner=FakeLark(
                {
                    "ok": False,
                    "error": {"type": "api", "message": "无权限"},
                }
            )
        )
        with self.assertRaises(LarkCliError) as caught:
            cli.run_json(["im", "+chat-messages-list"])
        self.assertIn("无权限", str(caught.exception))

    def test_run_json_rejects_non_json(self):
        cli = LarkCli(runner=lambda args, stdin: "not json")
        with self.assertRaises(LarkCliError):
            cli.run_json(["im"])


class FeishuChatSourceTest(unittest.TestCase):
    def test_collects_only_human_messages_in_window(self):
        messages = [
            message("项目本周要合并晨报"),
            message("机器人自己的消息", sender="DSH SuperAgent", sender_type="app"),
            message("窗口外", create_time="2026-11-05 09:00"),
            {
                "create_time": "2026-11-05 10:40",
                "deleted": False,
                "msg_type": "text",
                "content": "系统消息",
                "sender": {},
            },
            {"content": "", "sender": {"name": "用户444625", "sender_type": "user"}},
        ]
        runner = FakeLark({"ok": True, "data": {"messages": messages}})
        source = FeishuChatSource(chat_id="oc_test", runner=runner)

        items = source.collect(window())

        self.assertEqual(len(items), 1)
        self.assertIn("项目本周要合并晨报", items[0].title)
        self.assertEqual(items[0].source, "群聊")
        self.assertEqual(items[0].tags, ("群聊",))

    def test_redacts_long_numbers_and_truncates(self):
        content = "票号 7842311142055 请查收" + "很长的内容" * 100
        runner = FakeLark({"ok": True, "data": {"messages": [message(content)]}})
        source = FeishuChatSource(
            chat_id="oc_test", runner=runner, max_message_chars=40
        )

        items = source.collect(window())

        combined = items[0].title + (items[0].detail or "")
        self.assertNotIn("7842311142055", combined)
        self.assertIn("[号码已省略]", combined)
        body = items[0].title.split("：", 1)[1]
        self.assertLessEqual(len(body), 41)

    def test_post_messages_keep_plain_text(self):
        messages = [
            message("开始工作吧", msg_type="post"),
            message(
                '{"title":"","content":[[{"tag":"text","text":"JSON 里的内容"}]]}',
                msg_type="post",
            ),
        ]
        runner = FakeLark({"ok": True, "data": {"messages": messages}})
        source = FeishuChatSource(chat_id="oc_test", runner=runner)

        items = source.collect(window())

        self.assertIn("开始工作吧", items[0].title)
        self.assertIn("JSON 里的内容", items[1].title)

    def test_uses_window_in_cli_arguments(self):
        runner = FakeLark({"ok": True, "data": {"messages": []}})
        FeishuChatSource(chat_id="oc_test", runner=runner).collect(window())

        args, stdin = runner.calls[0]
        self.assertIsNone(stdin)
        self.assertIn("--start", args)
        self.assertIn("2026-11-05T10:00:00+08:00", args)
        self.assertIn("--end", args)
        self.assertIn("2026-11-05T11:00:00+08:00", args)
        self.assertIn("--page-all", args)

    def test_cli_failure_becomes_source_error(self):
        source = FeishuChatSource(
            chat_id="oc_test", runner=FakeLark(error=RuntimeError("断网"))
        )
        with self.assertRaises(SourceError):
            source.collect(window())

    def test_missing_chat_id_is_rejected(self):
        source = FeishuChatSource(chat_id="")
        with self.assertRaises(SourceError):
            source.collect(window())


class FeishuChatSinkTest(unittest.TestCase):
    def test_sends_markdown_via_stdin(self):
        runner = FakeLark({"ok": True, "data": {}})
        sink = FeishuChatSink(chat_id="oc_test", runner=runner)

        sink.deliver("# 群内速报\n内容", report_date=datetime(2026, 11, 5).date())

        args, stdin = runner.calls[0]
        self.assertEqual(stdin, "# 群内速报\n内容")
        self.assertIn("+messages-send", args)
        self.assertIn("--markdown", args)
        self.assertIn("oc_test", args)

    def test_send_failure_becomes_delivery_error(self):
        sink = FeishuChatSink(
            chat_id="oc_test", runner=FakeLark(error=RuntimeError("断网"))
        )
        with self.assertRaises(DeliveryError):
            sink.deliver("内容", report_date=datetime(2026, 11, 5).date())


if __name__ == "__main__":
    unittest.main()
