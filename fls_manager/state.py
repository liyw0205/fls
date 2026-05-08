import time
from datetime import timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    FLS_TIMEZONE = ZoneInfo("Asia/Shanghai")
except Exception:
    FLS_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

from apscheduler.schedulers.background import BackgroundScheduler

# ============================================================
# 面板启动时间
# ============================================================
PANEL_START_TIME = time.time()
PANEL_START_STR = time.strftime(
    "%Y-%m-%d %H:%M:%S",
    time.localtime(PANEL_START_TIME),
)

scheduler = BackgroundScheduler(timezone=FLS_TIMEZONE)
scheduler.start()

RUNNING = {}
DEPS_RUNNING = {}