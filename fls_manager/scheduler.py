from datetime import timedelta

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .state import scheduler
from .models import load_tasks, get_task
from .task_runner import run_task_now
from .logs import cleanup_logs
from .config import (
    load_config,
    get_panel_timezone,
    get_panel_time_offset_seconds,
    panel_now,
)
from .utils import now_str


def cron_to_trigger(cron_expr):
    cron_expr = str(cron_expr or "").strip()
    if not cron_expr:
        return None

    fields = cron_expr.split()
    tz = get_panel_timezone()

    if len(fields) == 5:
        return CronTrigger.from_crontab(cron_expr, timezone=tz)

    if len(fields) == 6:
        sec, minute, hour, day, month, dow = fields
        return CronTrigger(
            second=sec,
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=dow,
            timezone=tz,
        )

    raise ValueError("Cron 格式错误，仅支持 5 位或 6 位")


def virtual_to_real_time(virtual_dt):
    """
    把面板虚拟时间转换成系统真实时间。

    面板虚拟时间 = 系统真实时间 + panel_time_offset_seconds
    所以：
        系统真实时间 = 面板虚拟时间 - panel_time_offset_seconds
    """
    offset_seconds = get_panel_time_offset_seconds()
    return virtual_dt - timedelta(seconds=offset_seconds)


def real_to_virtual_time(real_dt):
    """
    把系统真实时间转换成面板虚拟时间。
    """
    offset_seconds = get_panel_time_offset_seconds()
    return real_dt + timedelta(seconds=offset_seconds)


def calc_next_virtual_run_time(task):
    """
    根据面板虚拟时间计算任务下一次 Cron 时间。
    """
    cron_expr = str((task or {}).get("cron", "") or "").strip()

    if not cron_expr:
        return None

    trigger = cron_to_trigger(cron_expr)
    if not trigger:
        return None

    now_virtual = panel_now()
    return trigger.get_next_fire_time(None, now_virtual)


def schedule_task_job(task):
    """
    为单个任务安排下一次真实执行时间。

    这里使用 DateTrigger，而不是直接使用 CronTrigger。
    原因：
        APScheduler 本身依赖系统时间。
        我们要让 Cron 依赖 FLS 面板虚拟时间。
    """
    task_id = (task or {}).get("id")

    if not task_id:
        return

    enabled = (task or {}).get("enabled", True)
    cron_expr = str((task or {}).get("cron", "") or "").strip()

    if not enabled or not cron_expr:
        return

    try:
        next_virtual = calc_next_virtual_run_time(task)

        if not next_virtual:
            return

        next_real = virtual_to_real_time(next_virtual)

        scheduler.add_job(
            scheduler_run_once,
            trigger=DateTrigger(
                run_date=next_real,
                timezone=get_panel_timezone(),
            ),
            args=[task_id],
            id=f"task_{task_id}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

        print(
            "[Scheduler] 已加载任务: "
            f"{task.get('name') or task_id} "
            f"cron={cron_expr} "
            f"next_virtual={next_virtual.strftime('%Y-%m-%d %H:%M:%S')} "
            f"next_real={next_real.strftime('%Y-%m-%d %H:%M:%S')}"
        )

    except Exception as e:
        print(
            f"[Scheduler] 任务 {task.get('name') or task_id} cron 加载失败: {e}"
        )


def scheduler_run_once(task_id):
    """
    DateTrigger 触发一次任务后，再重新计算下一次 Cron。
    """
    try:
        run_task_now(task_id, source="cron")
    finally:
        try:
            task = get_task(task_id)

            if task and task.get("enabled", True):
                schedule_task_job(task)

        except Exception as e:
            print(f"[Scheduler] 重新安排任务失败 task_id={task_id}: {e}")


def scheduler_run(task_id):
    """
    保留旧接口兼容。
    """
    scheduler_run_once(task_id)


def reload_scheduler():
    print(f"[Scheduler] 开始重载 time={now_str()}")

    try:
        scheduler.remove_all_jobs()
    except Exception as e:
        print(f"[Scheduler] 清空任务失败: {e}")

    for task in load_tasks():
        schedule_task_job(task)

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

        # job.next_run_time 是系统真实时间，这里转成面板虚拟时间显示。
        virtual_next = real_to_virtual_time(
            job.next_run_time.astimezone(get_panel_timezone())
        )

        return virtual_next.strftime("%Y-%m-%d %H:%M:%S")

    except Exception:
        return "未加载"
