"""孤独日报：新闻聚合卡片（记者小孤独）。

数据源全部为公开接口，单源失败不影响整报；卡片走 SVG 渲染基建。
"""

from .service import (
    KIND_DAILY_REPORT,
    DailyReportService,
    ReportSchedules,
    build_report_commands,
    register_report_job,
)

__all__ = [
    "KIND_DAILY_REPORT",
    "DailyReportService",
    "ReportSchedules",
    "build_report_commands",
    "register_report_job",
]
