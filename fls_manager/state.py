from datetime import timezone, timedelta

try:
    from zoneinfo import ZoneInfo
    FLS_TIMEZONE = ZoneInfo("Asia/Shanghai")
except Exception:
    FLS_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")

from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler(timezone=FLS_TIMEZONE)
scheduler.start()

RUNNING = {}
DEPS_RUNNING = {}
