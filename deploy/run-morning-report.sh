#!/bin/sh
# 由 cron / systemd timer 调用的单次晨报入口。
#
# 依赖一个不提交到仓库的环境文件（默认 ~/.config/zte-morning-report/env），
# 里面放 DEEPSEEK_API_KEY、FEISHU_CHAT_ID 等变量；也可以用
# MORNING_REPORT_ENV_FILE 指定其他路径。
#
# crontab 示例：
#   */5 * * * * /path/to/repo/deploy/run-morning-report.sh >> ~/.local/state/zte-morning-report/cron.log 2>&1
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ENV_FILE="${MORNING_REPORT_ENV_FILE:-$HOME/.config/zte-morning-report/env}"

if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi

: "${MORNING_REPORT_CONFIG:?请设置 MORNING_REPORT_CONFIG，或在环境文件里指定配置文件路径}"

cd "$ROOT"
exec python3 -m morning_report --once --config "$MORNING_REPORT_CONFIG" "$@"
