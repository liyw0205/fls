from apscheduler.triggers.cron import CronTrigger
from .state import scheduler, FLS_TIMEZONE
from .models import load_tasks, get_task
from .task_runner import run_task_now
from .logs import cleanup_logs
from .config import load_config
from .utils import now_str

def cron_to_trigger(cron_expr):
    cron_expr = str(cron_expr or "").strip()
    if not cron_expr:
        return None

    fields = cron_expr.split()

    if len(fields) == 5:
        return CronTrigger.from_crontab(cron_expr, timezone=FLS_TIMEZONE)

    if len(fields) == 6:
        sec, minute, hour, day, month, dow = fields
        return CronTrigger(
            second=sec,
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=dow,
            timezone=FLS_TIMEZONE,
        )

    raise ValueError("Cron 格式错误，仅支持 5 位或 6 位")

def scheduler_run(task_id):
    run_task_now(task_id, source="cron")

def reload_scheduler():
    print(f"[Scheduler] 开始重载 time={now_str()}")

    try:
        scheduler.remove_all_jobs()
    except Exception as e:
        print(f"[Scheduler] 清空任务失败: {e}")

    for task in load_tasks():
        task_id = task.get("id")
        enabled = task.get("enabled", True)
        cron_expr = str(task.get("cron", "")).strip()

        if not enabled or not cron_expr:
            continue

        try:
            trigger = cron_to_trigger(cron_expr)
            scheduler.add_job(
                scheduler_run,
                trigger=trigger,
                args=[task_id],
                id=f"task_{task_id}",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            print(f"[Scheduler] 已加载任务: {task.get('name') or task_id} cron={cron_expr}")
        except Exception as e:
            print(f"[Scheduler] 任务 {task.get('name') or task_id} cron 加载失败: {e}")

    try:
        minutes = int(load_config().get("log_cleanup_minutes", 30))
        minutes = max(1, min(1440, minutes))
        scheduler.add_job(
            cleanup_logs,
            trigger="interval",
            minutes=minutes,
            id="log_cleanup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    except Exception as e:
        print(f"[Scheduler] 日志清理任务加载失败: {e}")

def get_task_next_run_time_text(task):
    try:
        cron_expr = str((task or {}).get("cron", "") or "").strip()
        if not cron_expr:
            return "-"
        if not (task or {}).get("enabled", True):
            return "已禁用"
        task_id = (task or {}).get("id")
        job = scheduler.get_job(f"task_{task_id}")
        if not job or not job.next_run_time:
            return "未加载"
        return job.next_run_time.astimezone(FLS_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "未加载"
