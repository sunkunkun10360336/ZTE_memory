"""PCE 每日晨报（Daily Morning Report）。

该包把“个人上下文”整理成一份每天早晨可读的简报：

- :mod:`morning_report.sources` 负责从各类来源收集上下文；
- :mod:`morning_report.pipeline` 负责去重、排序和截断；
- :mod:`morning_report.generator` 负责构造提示词并调用 DeepSeek；
- :mod:`morning_report.delivery` 负责把晨报投递到控制台、文件或飞书机器人；
- :mod:`morning_report.scheduler` 负责按天定时触发。

架构说明见 ``docs/morning-report-architecture.md``。
"""

from .app import MorningReportApp
from .config import AppConfig, ConfigError, load_config
from .generator import GenerationError, MorningReportGenerator
from .models import ContextItem, ReportRun, TimeWindow

__all__ = [
    "AppConfig",
    "ConfigError",
    "ContextItem",
    "GenerationError",
    "MorningReportApp",
    "MorningReportGenerator",
    "ReportRun",
    "TimeWindow",
    "load_config",
]

__version__ = "0.1.0"
