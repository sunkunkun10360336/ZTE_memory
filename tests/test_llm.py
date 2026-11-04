"""LLM.py 的单元测试。

使用本地 HTTP 服务器模拟 DeepSeek 的 /chat/completions 接口，测试过程不访问外网，
也不需要真实的 DeepSeek API Key。
"""

import copy
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import LLM

_SUCCESS_RESPONSE = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "deepseek-chat",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "你好，我是 DeepSeek。"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
}


class _MockHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        self.server.requests.append(
            {
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(raw),
            }
        )
        status, payload = self.server.response
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 保持测试输出干净
        pass


class _MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _MockHandler)
        self.requests = []
        self.response = (200, _SUCCESS_RESPONSE)


class DeepSeekClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _MockServer()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.requests.clear()
        self.server.response = (200, copy.deepcopy(_SUCCESS_RESPONSE))

    def make_client(self, **overrides):
        kwargs = {"api_key": "sk-test", "base_url": self.base_url}
        kwargs.update(overrides)
        return LLM.DeepSeekClient(**kwargs)

    def test_chat_with_string_prompt(self):
        client = self.make_client()

        answer = client.chat("你好")

        self.assertEqual(answer, "你好，我是 DeepSeek。")
        self.assertEqual(len(self.server.requests), 1)
        request = self.server.requests[0]
        self.assertEqual(request["path"], "/chat/completions")
        self.assertEqual(request["headers"]["Authorization"], "Bearer sk-test")
        self.assertEqual(request["body"]["model"], "deepseek-chat")
        self.assertFalse(request["body"]["stream"])
        self.assertEqual(
            request["body"]["messages"], [{"role": "user", "content": "你好"}]
        )

    def test_module_chat_uses_environment_and_system_prompt(self):
        env = {
            "DEEPSEEK_API_KEY": "sk-env",
            "DEEPSEEK_BASE_URL": self.base_url + "/",
            "DEEPSEEK_MODEL": "deepseek-reasoner",
        }
        # 保留 no_proxy 等环境变量，避免本地测试请求被系统代理接管。
        with mock.patch.dict(os.environ, env):
            answer = LLM.chat(
                "什么是 PCE？",
                system="你是一个简洁的助手。",
                temperature=0.2,
                max_tokens=128,
            )

        self.assertEqual(answer, "你好，我是 DeepSeek。")
        payload = self.server.requests[0]["body"]
        self.assertEqual(payload["model"], "deepseek-reasoner")
        self.assertEqual(payload["temperature"], 0.2)
        self.assertEqual(payload["max_tokens"], 128)
        self.assertEqual(
            payload["messages"],
            [
                {"role": "system", "content": "你是一个简洁的助手。"},
                {"role": "user", "content": "什么是 PCE？"},
            ],
        )
        self.assertEqual(
            self.server.requests[0]["headers"]["Authorization"], "Bearer sk-env"
        )

    def test_complete_returns_metadata(self):
        result = self.make_client().complete(
            [{"role": "user", "content": "你好"}]
        )

        self.assertEqual(result.content, "你好，我是 DeepSeek。")
        self.assertEqual(result.model, "deepseek-chat")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.usage["total_tokens"], 7)

    def test_http_error_raises_api_error_with_detail(self):
        self.server.response = (401, {"error": {"message": "Authentication Fails"}})

        with self.assertRaises(LLM.DeepSeekAPIError) as context:
            self.make_client().chat("你好")

        self.assertEqual(context.exception.status_code, 401)
        self.assertIn("Authentication Fails", str(context.exception))

    def test_missing_api_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(LLM.DeepSeekError):
                LLM.DeepSeekClient()

    def test_malformed_response(self):
        self.server.response = (200, {"choices": []})

        with self.assertRaises(LLM.DeepSeekError):
            self.make_client().chat("你好")

    def test_invalid_arguments_do_not_send_request(self):
        client = self.make_client()

        with self.assertRaises(ValueError):
            client.chat("   ")
        with self.assertRaises(ValueError):
            client.chat("你好", temperature=3)
        with self.assertRaises(ValueError):
            client.chat("你好", max_tokens=0)
        with self.assertRaises(ValueError):
            client.chat([{"role": "tool", "content": "结果"}])
        with self.assertRaises(ValueError):
            client.chat([{"role": [], "content": "结果"}])

        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
