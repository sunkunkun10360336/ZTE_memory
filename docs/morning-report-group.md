# 群内速报部署指南

> 目标：每隔固定时间读取群聊与仓库动态，生成一份中文速报并发送回同一个
> 飞书群。默认每 5 分钟一次，也可以改成每天固定时刻。

## 1. 它长什么样

一次运行会：

1. 通过 `lark-cli` 读取群聊在时间窗口内的人类消息（跳过机器人和系统消息）；
2. 通过 `gh` 读取仓库在该窗口内新建/合并的 PR 和默认分支提交，按作者区分
   「人工工作」与「Agent 工作」；
3. 调用 DeepSeek 生成一份简短的中文速报；
4. 通过 `lark-cli` 以 Markdown 富文本发送回群；
5. 把本次窗口终点和已投递条目指纹写进状态文件，下一次只处理新增内容。

示例配置见 [`examples/group-report/config.example.json`](../examples/group-report/config.example.json)。

## 2. 配置

```json
{
  "timezone": "Asia/Shanghai",
  "schedule": {"interval_minutes": 5},
  "state_file": "state.json",
  "report": {
    "profile": "group",
    "title": "群内速报",
    "footer": "本速报由 PCE 根据群聊与仓库动态自动生成。",
    "lookback_hours": 24,
    "lookahead_hours": 0,
    "max_items": 40,
    "max_chars": 600,
    "empty_body": "过去几分钟没有新的群内讨论或工作动态。"
  },
  "sources": [
    {"type": "feishu_chat", "chat_id": "${FEISHU_CHAT_ID}", "source_label": "群聊"},
    {
      "type": "github_activity",
      "repo": "${GITHUB_REPOSITORY}",
      "agent_account": "${GITHUB_AGENT_ACCOUNT}"
    }
  ],
  "delivery": [
    {"type": "feishu_chat", "chat_id": "${FEISHU_CHAT_ID}"}
  ]
}
```

- `schedule.interval_minutes` 与 `schedule.at` 二选一；`at` 是每天固定时刻
  （如 `"09:00"`），`interval_minutes` 是按分钟间隔（如 `5`）。
- `state_file` 让每次运行只处理新增内容；相对路径按配置文件所在目录解析。
  第一次运行用 `lookback_hours` 回看，之后从上次成功运行的终点继续。
- `report.profile` 为 `group` 时使用群聊速报提示词（总结群内讨论并区分人工与
  Agent 工作）；也可以设置 `report.system_prompt` 完全自定义。
- `report.empty_body` 是窗口内没有任何新内容时发送的兜底正文。

`${VAR}` 在加载配置时展开，密钥不要写进仓库。

## 3. 环境准备

需要：

- Python 3.9+；
- `lark-cli` 已配置好机器人身份，并且机器人在目标群里；
- `gh` 已登录一个有仓库读权限的账号；
- 环境变量 `DEEPSEEK_API_KEY`。

把环境变量放进一个只有当前用户可读的文件（不要提交）：

```sh
mkdir -p ~/.config/zte-morning-report
cat > ~/.config/zte-morning-report/env <<'EOF'
export DEEPSEEK_API_KEY="sk-..."
export FEISHU_CHAT_ID="oc_xxx"
export GITHUB_REPOSITORY="owner/repo"
export GITHUB_AGENT_ACCOUNT="agent-account"
export MORNING_REPORT_CONFIG="$HOME/.config/zte-morning-report/config.json"
EOF
chmod 600 ~/.config/zte-morning-report/env
```

把配置示例复制到 `~/.config/zte-morning-report/config.json` 并按需修改。

## 4. 手动验证

```sh
set -a; . ~/.config/zte-morning-report/env; set +a
cd /path/to/ZTE_memory
python3 -m morning_report --dry-run --config "$MORNING_REPORT_CONFIG"
```

`--dry-run` 只收集上下文并打印提示词，不调用模型、不发送消息。确认输入正确后
再跑一次真实发送：

```sh
./deploy/run-morning-report.sh
```

## 5. 定时运行

### cron（推荐）

```cron
*/5 * * * * flock -n /tmp/zte-morning-report.lock /path/to/ZTE_memory/deploy/run-morning-report.sh >> ~/.local/state/zte-morning-report/cron.log 2>&1
```

`flock` 防止上一次还没跑完时重复发送。cron 不会加载登录 shell 的环境，所以
变量统一放在环境文件里，由 `deploy/run-morning-report.sh` 读取。

### systemd user timer

`~/.config/systemd/user/zte-morning-report.service`：

```ini
[Unit]
Description=ZTE memory 群内速报

[Service]
Type=oneshot
Environment=MORNING_REPORT_ENV_FILE=%h/.config/zte-morning-report/env
ExecStart=%h/path/to/ZTE_memory/deploy/run-morning-report.sh
```

`~/.config/systemd/user/zte-morning-report.timer`：

```ini
[Unit]
Description=每 5 分钟运行群内速报

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now zte-morning-report.timer
```

### 常驻进程

也可以不用系统定时器，直接让进程常驻：

```sh
python3 -m morning_report --config "$MORNING_REPORT_CONFIG"
```

## 6. 隐私与日志

- 群聊内容只在本机与 DeepSeek 请求之间流动；日志只记录日期、条目数和通道名，
  不打印速报正文。
- 连续 8 位以上的数字会被替换成 `[号码已省略]`，提示词也要求模型不复述机票、
  票号、证件号、手机号、住址等敏感信息。
- 状态文件、日志和密钥都在仓库之外，不要提交到 GitHub。

## 7. 排查

| 现象 | 检查 |
| --- | --- |
| 没有消息 | `cron.log` 里是否有配置错误或来源全部失败 |
| 机器人读不到群消息 | 机器人是否在群里，`lark-cli im +chat-list` 是否能看到该群 |
| GitHub 动态为空 | `gh auth status`、`GITHUB_REPOSITORY` 是否正确 |
| 模型调用失败 | `DEEPSEEK_API_KEY` 是否有效、网络是否可达 |
| 重复发送 | 检查 `flock` 是否生效，`state_file` 是否可写 |
