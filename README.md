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

