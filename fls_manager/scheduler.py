from datetime import timedelta
import threading

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from .state import scheduler
from .models import load_tasks, get_task
from .task_runner import run_task_now
from .logs import cleanup_logs
from .paths import SCHEDULER_STATE_FILE
from .storage import read_json, write_json
from .config import (
    load_config,
    get_panel_timezone,
    get_panel_time_offset_seconds,
    panel_now,
)
from .utils import now_str


SCHEDULER_RELOAD_LOCK = threading.RLock()
SCHEDULER_STATE_LOCK = threading.RLock()
SCHEDULER_RELOAD_ACTIVE = False
SCHEDULER_STATE_LABELS = {
    "running": "重载中",
    "succeeded": "已完成",
    "failed": "失败",
    "unknown": "结果未知",
}


def _nonnegative_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


class SchedulerReloadError(RuntimeError):
    """Raised when a scheduler plan cannot be applied safely."""


def _scheduler_state_payload(
    status="unknown",
    *,
    task_count=0,
    scheduled_count=0,
    failed_tasks=None,
    error="",
    recovery="",
):
    status = str(status or "unknown")
    if status not in SCHEDULER_STATE_LABELS:
        status = "unknown"

    failures = []
    for item in failed_tasks or []:
        if not isinstance(item, dict):
            continue
        failures.append({
            "task_id": str(item.get("task_id") or ""),
            "task_name": str(item.get("task_name") or ""),
            "error": str(item.get("error") or ""),
        })

    return {
        "status": status,
        "status_label": SCHEDULER_STATE_LABELS[status],
        "task_count": _nonnegative_int(task_count),
        "scheduled_count": _nonnegative_int(scheduled_count),
        "failed_tasks": failures[:20],
        "error": str(error or ""),
        "recovery": str(recovery or ""),
        "updated_at": now_str(),
    }


def _persist_scheduler_state(state):
    state = state if isinstance(state, dict) else {}
    payload = _scheduler_state_payload(
        state.get("status", "unknown"),
        task_count=state.get("task_count", 0),
        scheduled_count=state.get("scheduled_count", 0),
        failed_tasks=state.get("failed_tasks", []),
        error=state.get("error", ""),
        recovery=state.get("recovery", ""),
    )
    if state.get("updated_at"):
        payload["updated_at"] = str(state["updated_at"])
    with SCHEDULER_STATE_LOCK:
        write_json(SCHEDULER_STATE_FILE, payload)


def _record_scheduler_state(status, **kwargs):
    state = _scheduler_state_payload(status, **kwargs)
    _persist_scheduler_state(state)
    return state


def get_scheduler_state():
    """Return the last reload result and resolve an old in-flight snapshot."""
    with SCHEDULER_STATE_LOCK:
        raw = read_json(SCHEDULER_STATE_FILE, {})

    state = _scheduler_state_payload(
        raw.get("status", "unknown") if isinstance(raw, dict) else "unknown",
        task_count=raw.get("task_count", 0) if isinstance(raw, dict) else 0,
        scheduled_count=raw.get("scheduled_count", 0) if isinstance(raw, dict) else 0,
        failed_tasks=raw.get("failed_tasks", []) if isinstance(raw, dict) else [],
        error=raw.get("error", "") if isinstance(raw, dict) else "",
        recovery=raw.get("recovery", "") if isinstance(raw, dict) else "",
    )
    if isinstance(raw, dict) and raw.get("updated_at"):
        state["updated_at"] = str(raw.get("updated_at"))

    if state["status"] == "running" and not SCHEDULER_RELOAD_ACTIVE:
        state = _scheduler_state_payload(
            "unknown",
            task_count=state["task_count"],
            scheduled_count=state["scheduled_count"],
            failed_tasks=state["failed_tasks"],
            error=(
                "面板重启后无法确认调度器重载是否完成；请检查任务列表和下次执行时间，"
                "确认后重新加载调度器。"
            ),
            recovery="检查任务调度状态后提交“重新加载调度器”。",
        )
        try:
            _persist_scheduler_state(state)
        except Exception as exc:
            state["error"] += f"；结果未知状态未能持久化：{exc}"

    return state


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


def _build_task_spec(task):
    """Validate one task and return an immutable job specification."""
    task = task if isinstance(task, dict) else {}
    task_id = str(task.get("id") or "").strip()
    cron_expr = str(task.get("cron") or "").strip()

    if not task_id or not task.get("enabled", True) or not cron_expr:
        return None

    trigger = cron_to_trigger(cron_expr)
    next_virtual = trigger.get_next_fire_time(None, panel_now())
    if not next_virtual:
        raise ValueError("Cron 没有可计算的下一次执行时间")

    next_real = virtual_to_real_time(next_virtual)
    return {
        "id": f"task_{task_id}",
        "task_id": task_id,
        "task_name": str(task.get("name") or task_id),
        "trigger": DateTrigger(run_date=next_real, timezone=get_panel_timezone()),
        "next_virtual": next_virtual,
        "next_real": next_real,
    }


def _build_scheduler_plan(tasks):
    tasks = list(tasks or [])
    failures = []
    specs = []

    for task in tasks:
        if not isinstance(task, dict):
            continue
        try:
            spec = _build_task_spec(task)
            if spec:
                specs.append(spec)
        except Exception as exc:
            failures.append({
                "task_id": str(task.get("id") or ""),
                "task_name": str(task.get("name") or task.get("id") or ""),
                "error": str(exc),
            })

    try:
        minutes = int(load_config().get("log_cleanup_minutes", 30))
        if minutes < 1 or minutes > 1440:
            raise ValueError("日志清理间隔必须在 1-1440 分钟之间")
    except Exception as exc:
        failures.append({
            "task_id": "log_cleanup",
            "task_name": "日志清理",
            "error": str(exc),
        })
        minutes = 30

    if failures:
        detail = "；".join(
            f"{item['task_id'] or item['task_name']}: {item['error']}"
            for item in failures[:5]
        )
        error = SchedulerReloadError(f"调度计划校验失败：{detail}")
        error.failed_tasks = failures
        raise error

    return {
        "task_count": len(tasks),
        "scheduled_count": len(specs),
        "task_specs": specs,
        "cleanup_minutes": minutes,
    }


def _job_snapshot(job):
    return {
        "id": str(getattr(job, "id", "")),
        "func": getattr(job, "func", None),
        "trigger": getattr(job, "trigger", None),
        "args": list(getattr(job, "args", ()) or ()),
        "kwargs": dict(getattr(job, "kwargs", {}) or {}),
        "name": getattr(job, "name", None),
        "misfire_grace_time": getattr(job, "misfire_grace_time", None),
        "coalesce": getattr(job, "coalesce", True),
        "max_instances": getattr(job, "max_instances", 1),
    }


def _restore_job_snapshots(snapshots):
    scheduler.remove_all_jobs()
    for item in snapshots:
        if not item.get("id") or item.get("func") is None or item.get("trigger") is None:
            continue
        scheduler.add_job(
            item["func"],
            trigger=item["trigger"],
            args=item["args"],
            kwargs=item["kwargs"],
            id=item["id"],
            name=item.get("name"),
            misfire_grace_time=item.get("misfire_grace_time"),
            coalesce=item.get("coalesce", True),
            max_instances=item.get("max_instances", 1),
            replace_existing=True,
        )


def _apply_scheduler_plan(plan):
    snapshots = [_job_snapshot(job) for job in scheduler.get_jobs()]
    try:
        scheduler.remove_all_jobs()
        for spec in plan["task_specs"]:
            scheduler.add_job(
                scheduler_run_once,
                trigger=spec["trigger"],
                args=[spec["task_id"]],
                id=spec["id"],
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
        scheduler.add_job(
            cleanup_logs,
            trigger="interval",
            minutes=plan["cleanup_minutes"],
            id="log_cleanup",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
    except Exception as exc:
        try:
            _restore_job_snapshots(snapshots)
        except Exception as rollback_exc:
            raise SchedulerReloadError(
                f"调度器重载失败：{exc}；回滚失败：{rollback_exc}"
            ) from exc
        raise SchedulerReloadError(
            f"调度器重载失败：{exc}；已恢复上一组调度"
        ) from exc


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

    spec = _build_task_spec(task)
    if spec is None:
        return

    scheduler.add_job(
        scheduler_run_once,
        trigger=spec["trigger"],
        args=[task_id],
        id=spec["id"],
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    print(
        "[Scheduler] 已加载任务: "
        f"{task.get('name') or task_id} "
        f"cron={cron_expr} "
        f"next_virtual={spec['next_virtual'].strftime('%Y-%m-%d %H:%M:%S')} "
        f"next_real={spec['next_real'].strftime('%Y-%m-%d %H:%M:%S')}"
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


def _reload_scheduler_locked():
    print(f"[Scheduler] 开始重载 time={now_str()}")
    tasks = load_tasks()
    try:
        _record_scheduler_state(
            "running",
            task_count=len(tasks),
            recovery="重载完成前请勿重复提交调度变更。",
        )
    except Exception as exc:
        raise SchedulerReloadError(f"无法保存调度器重载状态：{exc}") from exc

    try:
        plan = _build_scheduler_plan(tasks)
        _apply_scheduler_plan(plan)
    except Exception as exc:
        error = str(exc)
        try:
            _record_scheduler_state(
                "failed",
                task_count=len(tasks),
                scheduled_count=0,
                failed_tasks=getattr(exc, "failed_tasks", []),
                error=error,
                recovery="任务数据已保留；修正调度配置后重新加载调度器。",
            )
        except Exception as state_exc:
            error += f"；保存失败状态失败：{state_exc}"
        if isinstance(exc, SchedulerReloadError):
            raise SchedulerReloadError(error) from exc
        raise SchedulerReloadError(error) from exc

    try:
        state = _record_scheduler_state(
            "succeeded",
            task_count=plan["task_count"],
            scheduled_count=plan["scheduled_count"],
            recovery="调度器已按当前任务配置加载。",
        )
    except Exception as exc:
        raise SchedulerReloadError(
            f"调度器已更新，但重载结果保存失败：{exc}"
        ) from exc

    return state


def reload_scheduler():
    global SCHEDULER_RELOAD_ACTIVE
    with SCHEDULER_RELOAD_LOCK:
        SCHEDULER_RELOAD_ACTIVE = True
        try:
            return _reload_scheduler_locked()
        finally:
            SCHEDULER_RELOAD_ACTIVE = False


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
