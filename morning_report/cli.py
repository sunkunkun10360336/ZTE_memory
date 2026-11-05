"""``python -m morning_report`` 命令行入口。"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from .app import MorningReportApp
from .config import ConfigError, load_config
from .delivery import DeliveryError
from .scheduler import DailyScheduler, IntervalScheduler
from .sources import SourceError

log = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m morning_report",
        description="根据个人上下文生成 PCE 每日晨报。",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        help="配置文件路径（默认 ./config.json，示例见 examples/config.example.json）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只运行一次后退出，适合交给 cron / systemd timer",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只收集上下文并打印提示词，不调用模型、不投递",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="输出调试日志"
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
        app = MorningReportApp(config)
    except (ConfigError, SourceError, DeliveryError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        try:
            run = app.run(dry_run=True)
        except Exception as exc:  # noqa: BLE001 - CLI 需要给出可读的失败信息
            log.exception("晨报 dry-run 失败")
            print(f"晨报 dry-run 失败：{exc}", file=sys.stderr)
            return 1
        print(run.prompt)
        return 0

    if not args.once:
        if config.schedule.interval_minutes is not None:
            minutes = config.schedule.interval_minutes
            print(
                f"晨报已启动：每 {minutes} 分钟生成一次（{config.schedule.timezone}），"
                "Ctrl+C 退出。"
            )
            scheduler = IntervalScheduler(minutes)
        else:
            at = config.schedule.at
            if at is None:
                print(
                    "配置错误：schedule.at 与 schedule.interval_minutes 至少配置一个",
                    file=sys.stderr,
                )
                return 2
            print(
                f"每日晨报已启动：每天 {at.strftime('%H:%M')}（{config.schedule.timezone}）生成，"
                "Ctrl+C 退出。"
            )
            scheduler = DailyScheduler(at, ZoneInfo(config.schedule.timezone))
        try:
            scheduler.run_forever(lambda _target: app.run())
        except KeyboardInterrupt:
            return 0
        return 0

    try:
        run = app.run()
    except Exception as exc:  # noqa: BLE001 - CLI 需要给出可读的失败信息
        log.exception("晨报生成失败")
        print(f"晨报生成失败：{exc}", file=sys.stderr)
        return 1

    delivered = ", ".join(run.delivered) or "无"
    print(
        f"晨报已生成：{run.report_date.isoformat()}，"
        f"{len(run.items)} 条新上下文，投递通道：{delivered}"
    )
    return 0


__all__ = ["build_parser", "main"]
