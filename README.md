# ZTE_memory
创建一个ZTE memory 系统, 服务于PCE, 个人上下文引擎

## LLM 调用（DeepSeek，非流式）

`LLM.py` 只对接 DeepSeek 官方 API（OpenAI 兼容的 `/chat/completions` 接口），
一次调用返回完整结果，不做流式输出，也不依赖第三方 SDK。

先设置 API Key：

```bash
export DEEPSEEK_API_KEY="sk-..."
```

最简单的用法：

```python
from LLM import chat

answer = chat("你好，请用一句话介绍你自己。")
print(answer)
```

多轮消息和可选参数：

```python
from LLM import chat

answer = chat(
    [
        {"role": "system", "content": "你是一个简洁的助手。"},
        {"role": "user", "content": "什么是 PCE？"},
    ],
    temperature=0.3,
    max_tokens=512,
)
print(answer)
```

需要复用配置或读取 token 用量时，使用 `DeepSeekClient`：

```python
from LLM import DeepSeekClient

client = DeepSeekClient(model="deepseek-reasoner", timeout=120)
result = client.complete("帮我总结这段文字……")
print(result.content)
print(result.usage)
```

支持的环境变量：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | DeepSeek API Key（必填） | 无 |
| `DEEPSEEK_BASE_URL` | API 地址 | `https://api.deepseek.com` |
| `DEEPSEEK_MODEL` | 模型名 | `deepseek-chat` |

运行测试（不需要网络和真实 API Key）：

```bash
python -m unittest discover
```

## 每日晨报

把个人上下文（日历、待办、关注动态……）整理成每天早晨的一份 Markdown 简报，
由 DeepSeek 生成，可投递到控制台、文件和飞书群。完整设计见
[每日晨报架构设计](docs/morning-report-architecture.md)。

### 快速开始

```bash
export DEEPSEEK_API_KEY="sk-..."

# 只看收集到的上下文和提示词，不调用模型、不投递
python -m morning_report --dry-run --config examples/config.example.json

# 立即生成并投递一次
python -m morning_report --once --config examples/config.example.json
```

示例配置使用 `examples/context.example.json` 作为上下文来源，并把晨报写入
`reports/`（已在 `.gitignore` 中忽略）。

### 定时运行

常驻进程模式（每天 08:00 生成）：

```bash
python -m morning_report --config config.json
```

也可以交给 cron / systemd timer，每次执行 `--once`：

```cron
0 8 * * * cd /path/to/ZTE_memory && DEEPSEEK_API_KEY=sk-... \
  python -m morning_report --once --config config.json
```

### 上下文数据格式

`json_file` 来源读取下面的结构（也可以直接是一个数组）：

```json
{
  "items": [
    {
      "title": "上午 10:00 项目周会",
      "source": "日历",
      "detail": "同步晨报架构进展",
      "happened_at": "2026-11-04T10:00:00+08:00",
      "priority": 3,
      "tags": ["会议"]
    }
  ]
}
```

- `title` 必填；`priority` 越大越靠前；
- `happened_at` 为 ISO 8601 时间，缺省表示“常驻事项”；
- 晨报默认回看 24 小时、前瞻 12 小时，可在 `report.lookback_hours` /
  `report.lookahead_hours` 中调整。

### 接入新的数据源和投递通道

来源实现 `collect(window)`，通道实现 `deliver(report, *, report_date)`，
在配置里用 `{"type": "python", "class": "包.模块:类"}` 加载即可，无需改动核心代码。
具体示例见[架构文档的扩展点章节](docs/morning-report-architecture.md#7-扩展点)。
