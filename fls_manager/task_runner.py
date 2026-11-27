import os
import time
import signal
import random
import threading
import subprocess
import uuid

from .state import RUNNING
from .models import (
    get_task,
    mutate_tasks,
    TASKS_MUTATION_LOCK,
    load_global_env,
    add_task_history,
    update_task_history,
)
from .command import build_command
from .logs import log_file_for_task, tail_file
from .utils import now_str, safe_name
from .constants import TASK_PROCESS_PREFIX
from .config import load_config
from .proxy import apply_proxy_env
from .notify import task_notify_ids, extract_user_log_content, send_by_ids
from .directory_jobs import directory_job
from .task_execution_coordination import (
    TaskExecution, TaskProcess, TaskRetryTimer, execution_snapshot, request_task_stop,
)


STOPPED_MANUALLY = set()
PENDING_RETRIES = {}
_RUNNING_LOCK = threading.RLock()


def running_history_ids():
    with _RUNNING_LOCK:
        local = {
            str(info.get("history_id"))
            for info in RUNNING.values()
            if info.get("history_id")
        }
    return local | {row["history_id"] for row in execution_snapshot().values() if row["active"]}


def _pop_running(task_id, history_id=None):
    with _RUNNING_LOCK:
        info = RUNNING.get(task_id)
        if not info:
            return False
        if history_id and info.get("history_id") != history_id:
            return False
        RUNNING.pop(task_id, None)
        return True


def _has_running_attempt(task_id, history_id):
    with _RUNNING_LOCK:
        info = RUNNING.get(task_id)
        present = bool(info and info.get("history_id") == history_id)
        execution = info.get("execution") if present else None
    if execution is not None and execution.stopping():
        with _RUNNING_LOCK:
            STOPPED_MANUALLY.add((task_id, history_id))
        return False
    return present


def _join_task_watcher(info, timeout=5):
    """Wait briefly for a terminal watcher before releasing a stopped task."""
    watcher = (info or {}).get("watcher_thread")
    if not watcher or watcher is threading.current_thread():
        return
    try:
        if watcher.is_alive():
            watcher.join(timeout=max(0, timeout))
    except Exception:
        # The stop result is still governed by the durable history state.
        pass


def _running_history_record(task_id, history_id):
    with _RUNNING_LOCK:
        info = RUNNING.get(task_id)
        record = info.get("history_record") if info and info.get("history_id") == history_id else None
        return dict(record) if isinstance(record, dict) else None


def _update_running_attempt(task_id, history_id, updates):
    with _RUNNING_LOCK:
        info = RUNNING.get(task_id)
        if not info or info.get("history_id") != history_id:
            return False
        info.update(updates)
        return True


def _cancel_task_retry_entry(pending, reason):
    if not pending:
        return False

    pending["timer"].cancel()
    append_task_log(
        pending["log_file"],
        f"\n===== 已取消计划重试：{reason} =====\n",
    )
    return True


def _clear_stopped_marker(task_id, history_id):
    with _RUNNING_LOCK:
        STOPPED_MANUALLY.discard((task_id, history_id))


def cancel_task_retry(task_id, reason="收到新的任务操作"):
    requested = request_task_stop(task_id, retry_only=True)
    with _RUNNING_LOCK:
        pending = PENDING_RETRIES.pop(task_id, None)

    cancelled = _cancel_task_retry_entry(pending, reason)
    if requested:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not execution_snapshot([task_id]).get(task_id, {}).get("active"):
                break
            time.sleep(0.02)
    return cancelled or requested


def safe_process_name(name):
    name = safe_name(name)
    return (TASK_PROCESS_PREFIX + name)[:120]


def _is_running_local(task_id):
    with _RUNNING_LOCK:
        info = RUNNING.get(task_id)

        if not info:
            return False

        status = info.get("status")
        if status in ("starting", "delaying"):
            return True
        if info.get("watcher_managed"):
            return True
        if info.get("watcher_done") and info.get("stop_error"):
            return True

        proc = info.get("process")

        if not proc:
            RUNNING.pop(task_id, None)
            return False

        if proc.poll() is None:
            return True

        return False


def is_running(task_id):
    if _is_running_local(task_id):
        return True
    return execution_snapshot([task_id]).get(task_id, {}).get("active", False)


def running_task_snapshot(task_ids=None):
    """Return one consistent running-state snapshot for a batch read.

    The status and task-list pages need the same process state for many tasks.
    Keeping the lock across the batch avoids reacquiring it and looking up the
    same runtime map once per row, while preserving ``is_running`` semantics.
    """
    requested = None
    if task_ids is not None:
        requested = {str(task_id or "") for task_id in task_ids}

    persisted = execution_snapshot(requested)
    with _RUNNING_LOCK:
        task_keys = list(RUNNING) if requested is None else requested
        snapshot = {
            task_id: {"history_id": row["history_id"], "status": row["status"], "pid": "-", "active": True,
                      "stop_requested": row["stop_requested"], "log_name": row.get("log_name", "")}
            for task_id, row in persisted.items() if row["active"]
        }
        for task_id in task_keys:
            info = RUNNING.get(task_id)
            if not info:
                continue

            status = info.get("status")
            if status in ("starting", "delaying"):
                snapshot[task_id] = dict(info)
                continue
            if info.get("watcher_managed"):
                snapshot[task_id] = dict(info)
                continue
            if info.get("watcher_done") and info.get("stop_error"):
                snapshot[task_id] = dict(info)
                continue

            proc = info.get("process")
            if not proc:
                RUNNING.pop(task_id, None)
                continue

            if proc.poll() is None:
                snapshot[task_id] = dict(info)

        for task_id, info in snapshot.items():
            row = persisted.get(task_id, {})
            if row.get("active"):
                info.update(status=row["status"], stop_requested=row["stop_requested"],
                            history_id=row["history_id"], active=True)
        return snapshot


def increase_run_count(task_id):
    def increment(tasks):
        for item in tasks:
            if item.get("id") == task_id:
                item["run_count"] = int(item.get("run_count", 0)) + 1
                item["last_run_at"] = now_str()
                item["updated_at"] = now_str()
                return True
        return False

    mutate_tasks(increment)


def append_task_log(log_file, text):
    try:
        with open(log_file, "ab") as f:
            f.write(str(text).encode("utf-8", errors="replace"))

            if not str(text).endswith("\n"):
                f.write(b"\n")

    except Exception as e:
        print(f"[TaskLog] 写任务日志失败: {e}")


def finish_task_history(history_id, start_ts, status, return_code=None, log_file="", message="", fallback_record=None):
    if not history_id:
        return False

    try:
        duration = max(0, int(time.time() - float(start_ts or time.time())))
    except Exception:
        duration = 0

    updates = {
        "status": status,
        "end_at": now_str(),
        "duration_seconds": duration,
        "return_code": return_code,
        "log_file": str(log_file or ""),
    }

    if message:
        updates["message"] = str(message)

    saved = update_task_history(
        history_id,
        updates,
        fallback_record=fallback_record,
        strict=fallback_record is not None,
    )
    if saved:
        with _RUNNING_LOCK:
            for info in RUNNING.values():
                if info.get("history_id") == history_id and info.get("execution") is not None:
                    info["execution"].terminal = status
    return saved


def task_retry_config(task):
    retry = (task or {}).get("retry") or {}

    if not isinstance(retry, dict):
        retry = {}

    try:
        attempts = int(retry.get("attempts", 0) or 0)
    except Exception:
        attempts = 0

    try:
        interval_seconds = int(retry.get("interval_seconds", 60) or 60)
    except Exception:
        interval_seconds = 60

    return {
        "attempts": max(0, min(5, attempts)),
        "interval_seconds": max(5, min(3600, interval_seconds)),
    }


def schedule_task_retry(task_id, task_snapshot, retry_attempt, reason, log_file, history_id=None, execution=None):
    cfg = task_retry_config(task_snapshot)
    max_retries = int(cfg.get("attempts", 0) or 0)

    if retry_attempt >= max_retries:
        return False, ""

    next_attempt = retry_attempt + 1
    interval_seconds = int(cfg.get("interval_seconds", 60) or 60)

    pending = {"timer": None, "log_file": str(log_file)}
    with _RUNNING_LOCK:
        execution = execution or RUNNING.get(task_id, {}).get("execution")
    if execution is not None and execution.stopping():
        return False, "任务已停止，取消重试"
    pending["execution"] = execution

    def retry_worker():
        try:
            with _RUNNING_LOCK:
                if PENDING_RETRIES.get(task_id) is not pending:
                    return

            latest_task = get_task(task_id)

            if not latest_task:
                append_task_log(log_file, "\n===== 重试取消：任务已不存在 =====\n")
                return

            if not latest_task.get("enabled", True):
                append_task_log(log_file, "\n===== 重试取消：任务已禁用 =====\n")
                return

            ok, msg = run_task_now(
                task_id,
                source="retry",
                retry_attempt=next_attempt,
                retry_token=pending,
            )

            if not ok and msg != "重试已取消":
                append_task_log(
                    log_file,
                    f"\n===== 重试启动失败: {now_str()}，{msg} =====\n",
                )

        except Exception as e:
            append_task_log(log_file, f"\n===== 重试调度失败: {e} =====\n")
        finally:
            with _RUNNING_LOCK:
                if PENDING_RETRIES.get(task_id) is pending:
                    PENDING_RETRIES.pop(task_id, None)

    try:
        if execution is None:
            timer = threading.Timer(interval_seconds, retry_worker)
        else:
            def cancelled():
                with _RUNNING_LOCK:
                    if PENDING_RETRIES.get(task_id) is pending:
                        PENDING_RETRIES.pop(task_id, None)
                append_task_log(log_file, "\n===== 已取消计划重试 =====\n")
            timer = TaskRetryTimer(interval_seconds, retry_worker, execution, cancelled, threading.Timer)
        timer.daemon = True
    except Exception as exc:
        retry_error = f"重试计划失败：{exc}"
        append_task_log(log_file, f"\n===== {retry_error} =====\n")
        return False, retry_error
    pending["timer"] = timer
    # Task mutations cancel retries under this lock order: tasks before running state.
    with TASKS_MUTATION_LOCK:
        if history_id:
            latest_task = get_task(task_id)
            if not latest_task or not latest_task.get("enabled", True):
                return False, "任务已删除或禁用，取消重试"

        with _RUNNING_LOCK:
            if history_id:
                current = RUNNING.get(task_id)
                if (
                    not current
                    or current.get("history_id") != history_id
                    or (task_id, history_id) in STOPPED_MANUALLY
                ):
                    return False, "任务已停止，取消重试"

            previous = PENDING_RETRIES.get(task_id)
            if previous:
                previous["timer"].cancel()
            PENDING_RETRIES[task_id] = pending
            append_task_log(
                log_file,
                (
                    f"\n===== 任务失败，计划第 {next_attempt}/{max_retries} 次重试 =====\n"
                    f"原因: {reason}\n"
                    f"间隔: {interval_seconds} 秒\n"
                    f"时间: {now_str()}\n"
                ),
            )
            try:
                if execution is not None:
                    execution.phase("retry_wait")
                timer.start()
            except Exception as exc:
                if PENDING_RETRIES.get(task_id) is pending:
                    PENDING_RETRIES.pop(task_id, None)
                try:
                    timer.cancel()
                except Exception:
                    pass
                retry_error = f"重试计划失败：{exc}"
                append_task_log(log_file, f"\n===== {retry_error} =====\n")
                return False, retry_error

    return True, f"{reason}，已计划第 {next_attempt}/{max_retries} 次重试"


def terminate_task_process(proc):
    """Request termination and return a verified result with the real error."""
    if proc is None:
        return False, "任务进程不存在"

    if isinstance(proc, TaskProcess):
        try:
            proc.stop(timeout=10)
            return proc.poll() is not None, ""
        except Exception:
            return False, "尚未确认任务进程树和日志已退出"

    try:
        if proc.poll() is not None:
            return False, f"任务已结束，退出码 {proc.poll()}"
    except Exception as e:
        return False, f"读取任务进程状态失败：{e}"

    errors = []

    if os.name == "nt":
        try:
            proc.terminate()
        except Exception as e:
            errors.append(f"请求正常终止失败：{e}")
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception as e:
            errors.append(f"发送进程组终止信号失败：{e}")
            try:
                proc.terminate()
            except Exception as fallback_error:
                errors.append(f"请求进程终止失败：{fallback_error}")

    try:
        proc.wait(timeout=1)
        if proc.poll() is not None:
            return True, ""
    except subprocess.TimeoutExpired:
        pass
    except Exception as e:
        errors.append(f"等待正常终止失败：{e}")

    if os.name == "nt":
        try:
            proc.kill()
        except Exception as e:
            errors.append(f"强制终止失败：{e}")
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception as e:
            errors.append(f"发送进程组强制终止信号失败：{e}")
            try:
                proc.kill()
            except Exception as fallback_error:
                errors.append(f"强制终止进程失败：{fallback_error}")

    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    except Exception as e:
        errors.append(f"等待强制终止失败：{e}")

    try:
        if proc.poll() is not None:
            return True, ""
    except Exception as e:
        errors.append(f"确认任务进程状态失败：{e}")

    detail = "；".join(errors)
    if detail:
        detail += "；无法确认任务进程已退出"
    return False, detail or "无法确认任务进程已退出"


def force_kill_process(proc):
    """Compatibility wrapper for timeout and stale-worker cleanup paths."""
    ok, detail = terminate_task_process(proc)
    return ok or detail.startswith("任务已结束")


def task_random_delay_seconds(task):
    delay = task.get("random_delay") or {}

    if not isinstance(delay, dict):
        return 0

    mode = delay.get("mode", "none")

    if mode == "none":
        return 0

    if mode == "default":
        try:
            seconds = int(load_config().get("random_delay_seconds", 0) or 0)
        except Exception:
            seconds = 0

    elif mode == "custom":
        try:
            seconds = int(delay.get("seconds", 0) or 0)
        except Exception:
            seconds = 0

    else:
        seconds = 0

    if seconds <= 0:
        return 0

    seconds = max(1, min(120, seconds))
    return random.randint(1, seconds)


def task_finish_watcher(task_id, task_snapshot, proc, log_file, log_fp, history_id, history_start_ts, retry_attempt):
    try:
        timeout_seconds = 0

        try:
            timeout_seconds = int(load_config().get("task_timeout_seconds", 1800) or 0)
        except Exception:
            timeout_seconds = 1800

        timed_out = False

        try:
            if timeout_seconds > 0:
                return_code = proc.wait(timeout=timeout_seconds)
            else:
                return_code = proc.wait()

        except subprocess.TimeoutExpired:
            timed_out = True

            append_task_log(
                log_file,
                f"\n===== 任务超时: {now_str()}，超过 {timeout_seconds} 秒，开始强制结束 =====\n"
            )

            terminated = force_kill_process(proc)
            try:
                return_code = proc.poll()
            except Exception:
                return_code = None

            if return_code is None and terminated:
                try:
                    return_code = proc.wait(timeout=1)
                except Exception:
                    return_code = None

            if return_code is None:
                append_task_log(
                    log_file,
                    "===== 超时后停止失败：无法确认任务进程已退出 =====\n",
                )
                _update_running_attempt(
                    task_id,
                    history_id,
                    {
                        "status": "running",
                        "stop_error": "超时后停止失败：无法确认任务进程已退出",
                        "watcher_done": True,
                        "terminal_status": "timeout",
                        "terminal_message": (
                            f"任务超时，超过 {timeout_seconds} 秒；"
                            "超时后停止失败：无法确认任务进程已退出"
                        ),
                    },
                )
                update_task_history(
                    history_id,
                    {
                        "status": "running",
                        "message": "超时后停止失败：无法确认任务进程已退出",
                    },
                )
                return

        stopped_key = (task_id, history_id)
        manual_stopped = stopped_key in STOPPED_MANUALLY

        if timed_out:
            history_status = "timeout"
            history_msg = f"任务超时，超过 {timeout_seconds} 秒"
        elif manual_stopped:
            history_status = "stopped"
            history_msg = "手动结束"
        elif return_code == 0:
            history_status = "success"
            history_msg = "运行成功"
        else:
            history_status = "failed"
            history_msg = f"退出码 {return_code}"

        if history_id:
            _update_running_attempt(task_id, history_id, {
                "status": "running",
                "watcher_done": True,
                "stop_error": "任务进程已退出，正在保存终态历史",
                "terminal_status": history_status,
                "terminal_message": history_msg,
                "return_code": return_code,
            })

        retry_scheduled = False

        if not manual_stopped and history_status in ("failed", "timeout"):
            retry_scheduled, retry_msg = schedule_task_retry(
                task_id,
                task_snapshot,
                retry_attempt,
                history_msg,
                log_file,
                history_id=history_id,
            )

            if retry_scheduled:
                history_msg = retry_msg
            elif retry_msg:
                history_msg = f"{history_msg}；{retry_msg}"

        try:
            if history_id:
                history_saved = finish_task_history(
                    history_id,
                    history_start_ts,
                    history_status,
                    return_code=return_code,
                    log_file=log_file,
                    message=history_msg,
                    fallback_record=_running_history_record(task_id, history_id),
                )
                if not history_saved:
                    raise RuntimeError("找不到可更新的任务历史记录")
        except Exception as exc:
            with _RUNNING_LOCK:
                execution = RUNNING.get(task_id, {}).get("execution")
            if execution is not None:
                execution.failed = True
            recovery_message = f"{history_msg}；终态历史保存失败：{exc}"
            marker = {
                "status": "running",
                "watcher_done": True,
                "stop_error": "终态历史保存失败，尚未确认任务完成状态",
                "terminal_status": history_status,
                "terminal_message": recovery_message,
                "return_code": return_code,
            }
            if not _update_running_attempt(task_id, history_id, marker):
                return

            if retry_scheduled:
                try:
                    cancelled = cancel_task_retry(task_id, "任务终态历史未确认")
                    retry_message = "终态确认前已取消自动重试" if cancelled else "终态确认前自动重试将被阻止"
                except Exception as retry_error:
                    retry_message = f"自动重试取消失败：{retry_error}；任务运行位仍保持占用"
                recovery_message += f"；{retry_message}，确认后可从历史页重新运行"
                _update_running_attempt(task_id, history_id, {"terminal_message": recovery_message})

            append_task_log(log_file, f"\n===== {recovery_message} =====\n")
            try:
                if log_fp:
                    log_fp.close()
            except Exception:
                pass
            return

        try:
            if log_fp:
                if timed_out:
                    log_fp.write(
                        f"\n===== 任务已结束: {now_str()}，退出码: {return_code}，原因: 超时强制结束 =====\n".encode("utf-8")
                    )
                elif manual_stopped:
                    log_fp.write(
                        f"\n===== 任务已结束: {now_str()}，退出码: {return_code}，原因: 手动结束 =====\n".encode("utf-8")
                    )
                else:
                    log_fp.write(
                        f"\n===== 任务已结束: {now_str()}，退出码: {return_code} =====\n".encode("utf-8")
                    )

                log_fp.close()

        except Exception:
            pass

        _pop_running(task_id, history_id=history_id)

        with _RUNNING_LOCK:
            manual_stopped = manual_stopped or stopped_key in STOPPED_MANUALLY
            STOPPED_MANUALLY.discard(stopped_key)

        if manual_stopped:
            append_task_log(log_file, "\n===== 手动结束任务，不发送任务完成通知 =====\n")
            return

        if retry_scheduled:
            append_task_log(log_file, "\n===== 已计划失败重试，暂不发送任务完成通知 =====\n")
            return

        notify_ids = task_notify_ids(task_snapshot)

        if "__none__" in notify_ids:
            append_task_log(log_file, "\n===== 通知结果 =====\n任务设置为不通知\n")
            return

        task_name = task_snapshot.get("name") or task_snapshot.get("command") or task_id
        log_text = tail_file(str(log_file), 5000)
        content = extract_user_log_content(log_text)

        if not content.strip():
            content = f"任务无输出日志。\n退出码：{return_code}"

        title = str(task_name or "FLS 任务").strip()
        results = send_by_ids(title, content, notify_ids)

        lines = ["\n===== 通知结果，不包含在本次通知内容中 ====="]

        if not results:
            lines.append("未配置可用通知或全局默认通知为空")
        else:
            for result in results:
                lines.append(
                    f"{result.get('name')}: {'成功' if result.get('ok') else '失败'} - {result.get('msg')}"
                )

        lines.append("============================================================\n")
        append_task_log(log_file, "\n".join(lines))

    except Exception as e:
        msg = f"[TaskWatcher] 任务结束监听失败: {e}"
        print(msg)

        try:
            append_task_log(log_file, msg)
        except Exception:
            pass


def _task_finish_worker(execution, *args):
    try:
        task_finish_watcher(*args)
    finally:
        execution.retry_ready.set()
        execution.release()


def _start_task_worker(task_id, task_snapshot, cmd_info, process_name, log_file, log_fp, source, history_id, history_start_ts, retry_attempt, execution=None):
    try:
        _start_task_worker_body(task_id, task_snapshot, cmd_info, process_name, log_file, log_fp,
                                source, history_id, history_start_ts, retry_attempt, execution)
    finally:
        if execution is not None:
            try:
                if not execution.watcher_handoff:
                    execution.retry_ready.set()
                execution.release()
            finally:
                if execution.terminal in ("stopped", "start_failed"):
                    _pop_running(task_id, history_id=history_id)


def _start_task_worker_body(task_id, task_snapshot, cmd_info, process_name, log_file, log_fp, source, history_id, history_start_ts, retry_attempt, execution=None):
    try:
        if not _has_running_attempt(task_id, history_id):
            try:
                log_fp.close()
            except Exception:
                pass
            _clear_stopped_marker(task_id, history_id)
            finish_task_history(
                history_id,
                history_start_ts,
                "stopped",
                log_file=log_file,
                message="启动前已取消",
            )
            return

        env = os.environ.copy()
        env.update(load_global_env())
        env.update(task_snapshot.get("env", {}) or {})
        apply_proxy_env(dict(env), task_snapshot.get("proxy_id", ""))

        env["PYTHONUNBUFFERED"] = "1"
        env["FLS_TASK_ID"] = task_id
        env["FLS_TASK_NAME"] = task_snapshot.get("name") or task_snapshot.get("command") or task_id
        env["FLS_TASK_PROCESS_NAME"] = process_name

        display_cmd = cmd_info.get("display_cmd", cmd_info.get("cmd"))

        header = (
            f"===== 启动任务: {env['FLS_TASK_NAME']} =====\n"
            f"时间: {now_str()}\n"
            f"来源: {source}\n"
            f"重试次数: {retry_attempt}\n"
            f"任务标识名: {process_name}\n"
            f"命令: {task_snapshot.get('command')}\n"
            f"代理ID: {task_snapshot.get('proxy_id', '') or '不使用代理'}\n"
            f"工作目录: {cmd_info.get('cwd')}\n"
            f"实际启动命令: {display_cmd}\n"
            f"============================================================\n"
        )

        log_fp.write(header.encode("utf-8"))

        delay_seconds = task_random_delay_seconds(task_snapshot)

        if delay_seconds > 0:
            if execution is not None:
                execution.phase("delaying")
            if not _update_running_attempt(
                task_id,
                history_id,
                {"status": "delaying"},
            ):
                log_fp.write("===== 任务在随机延迟前被取消 =====\n".encode("utf-8"))
                log_fp.close()
                finish_task_history(history_id, history_start_ts, "stopped", log_file=log_file, message="随机延迟前被取消")
                return

            update_task_history(history_id, {"status": "delaying"})

            log_fp.write(
                f"===== 随机延迟: {delay_seconds} 秒，开始等待 =====\n".encode("utf-8")
            )

            if execution is None:
                time.sleep(delay_seconds)
            else:
                deadline = time.monotonic() + delay_seconds
                while time.monotonic() < deadline and _has_running_attempt(task_id, history_id):
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))

            if not _has_running_attempt(task_id, history_id):
                try:
                    log_fp.write("===== 任务在随机延迟期间被取消 =====\n".encode("utf-8"))
                    log_fp.close()
                except Exception:
                    pass
                finish_task_history(history_id, history_start_ts, "stopped", log_file=log_file, message="随机延迟期间被取消")
                return

            log_fp.write(
                f"===== 随机延迟结束: {now_str()}，开始启动任务 =====\n".encode("utf-8")
            )

        if not _update_running_attempt(
            task_id,
            history_id,
            {"status": "starting"},
        ):
            try:
                log_fp.write("===== 任务在启动前被取消 =====\n".encode("utf-8"))
                log_fp.close()
            except Exception:
                pass
            finish_task_history(history_id, history_start_ts, "stopped", log_file=log_file, message="启动前被取消")
            return

        update_task_history(history_id, {"status": "starting"})

        popen_kwargs = {
            "shell": cmd_info.get("shell", False),
            "cwd": cmd_info["cwd"],
            "stdout": log_fp,
            "stderr": subprocess.STDOUT,
            "env": env,
        }

        if os.name != "nt":
            popen_kwargs["preexec_fn"] = os.setsid

        with TASKS_MUTATION_LOCK:
            if task_snapshot.get("proxy_id"):
                popen_kwargs["env"] = apply_proxy_env(env, task_snapshot["proxy_id"])
            if execution is None:
                proc = subprocess.Popen(cmd_info["cmd"], **popen_kwargs)
            else:
                if execution.stopping():
                    raise RuntimeError("任务启动前已请求停止")
                def stopped():
                    with _RUNNING_LOCK:
                        STOPPED_MANUALLY.add((task_id, history_id))
                proc = TaskProcess(execution, cmd_info["cmd"], cwd=cmd_info["cwd"], env=popen_kwargs["env"],
                                   shell=cmd_info.get("shell", False), stdout=log_fp, on_stop=stopped)
                execution.phase("running")

        if not _update_running_attempt(
            task_id,
            history_id,
            {
                "process": proc,
                "pid": proc.pid,
                "process_name": process_name,
                "log_file": str(log_file),
                "log_fp": log_fp,
                "start_time": time.time(),
                "status": "running",
                "watcher_managed": True,
            },
        ):
            force_kill_process(proc)
            try:
                log_fp.write("===== 任务启动后立即被取消 =====\n".encode("utf-8"))
                log_fp.close()
            except Exception:
                pass
            finish_task_history(history_id, history_start_ts, "stopped", return_code=proc.poll(), log_file=log_file, message="启动后立即被取消")
            return

        update_task_history(history_id, {
            "status": "running",
            "pid": proc.pid,
        })

        increase_run_count(task_id)

        th = None
        try:
            if execution is not None:
                execution.retain()
            th = threading.Thread(
                target=task_finish_watcher if execution is None else _task_finish_worker,
                args=(() if execution is None else (execution,)) + (task_id, dict(task_snapshot), proc, str(log_file), log_fp, history_id, history_start_ts, retry_attempt),
                daemon=True,
                name=f"fls-task-watch-{task_id[:8]}",
            )
            th.start()
            if execution is not None:
                execution.watcher_handoff = True
            _update_running_attempt(task_id, history_id, {"watcher_thread": th})
        except Exception as watcher_error:
            if th is not None and getattr(th, "ident", None) is not None:
                if execution is not None:
                    execution.watcher_handoff = True
                _update_running_attempt(task_id, history_id, {"watcher_thread": th})
                return
            # A process is already owned by RUNNING at this point. If the
            # watcher cannot get a thread, finish it from the current worker
            # instead of closing the log and losing the child process.
            append_task_log(
                log_file,
                f"\n===== 任务监听线程启动失败，改为当前 worker 监听: {watcher_error} =====\n",
            )
            watcher_args = (task_id, dict(task_snapshot), proc, str(log_file), log_fp, history_id, history_start_ts, retry_attempt)
            if execution is None:
                task_finish_watcher(*watcher_args)
            else:
                _task_finish_worker(execution, *watcher_args)

    except Exception as e:
        if execution is not None and execution.process is not None:
            while True:
                try:
                    execution.process.stop(timeout=10)
                    break
                except subprocess.TimeoutExpired:
                    continue
                except Exception:
                    execution.failed = True
                    if execution.process._process.poll() is not None:
                        break
                    time.sleep(0.05)
        try:
            log_fp.write(f"启动失败: {e}\n".encode("utf-8"))
            log_fp.close()
        except Exception:
            pass

        if not _has_running_attempt(task_id, history_id):
            finish_task_history(
                history_id,
                history_start_ts,
                "stopped",
                log_file=log_file,
                message="启动期间已取消",
            )
            return

        retry_scheduled, retry_msg = schedule_task_retry(
            task_id,
            task_snapshot,
            retry_attempt,
            f"启动失败：{e}",
            log_file,
            history_id=history_id,
        )

        finish_task_history(
            history_id,
            history_start_ts,
            "start_failed",
            log_file=log_file,
            message=retry_msg if retry_scheduled else f"启动失败：{e}；{retry_msg}" if retry_msg else f"启动失败：{e}",
        )

        _pop_running(task_id, history_id=history_id)


def run_task_now(task_id, source="manual", retry_attempt=0, retry_token=None):
    from .script_rename import pending_script_rename

    with directory_job(), TASKS_MUTATION_LOCK:
        try:
            pending = pending_script_rename(task_id=task_id, task=get_task(task_id))
        except Exception as e:
            return False, f"任务启动失败：重命名恢复状态读取失败：{e}"
        if pending:
            return False, f"脚本重命名待恢复，请访问 /scripts/rename-recovery/{pending['id']} 完成恢复后运行"
        return _submit_task_now(task_id, source, retry_attempt, retry_token)


def _submit_task_now(task_id, source="manual", retry_attempt=0, retry_token=None):
    task = get_task(task_id)

    if not task:
        return False, "任务不存在"

    if task.get("proxy_id"):
        try:
            apply_proxy_env({}, task["proxy_id"])
        except Exception as exc:
            return False, f"任务启动失败：{exc}"

    if source != "retry":
        source_label = {"manual": "手动运行", "cron": "定时运行"}.get(source, "新运行请求")
        cancel_task_retry(task_id, source_label)

    submission_error = None

    with _RUNNING_LOCK:
        retry_execution = retry_token.get("execution") if isinstance(retry_token, dict) else None
        if is_running(task_id) and not (source == "retry" and retry_execution is not None and task_id not in RUNNING):
            return False, "任务已在运行中"

        if source == "retry":
            if retry_token is None or PENDING_RETRIES.get(task_id) is not retry_token:
                return False, "重试已取消"

    try:
        cmd_info = build_command(task)
    except Exception as e:
        return False, f"命令解析失败：{e}"

    retry_attempt = max(0, int(retry_attempt or 0))

    task_display_name = task.get("name") or task.get("command") or task_id
    process_name = safe_process_name(task_display_name)
    retry_cfg = task_retry_config(task)

    with _RUNNING_LOCK:
        if is_running(task_id) and not (source == "retry" and retry_execution is not None and task_id not in RUNNING):
            return False, "任务已在运行中"

        if source == "retry":
            if retry_token is None or PENDING_RETRIES.get(task_id) is not retry_token:
                return False, "重试已取消"
            PENDING_RETRIES.pop(task_id, None)
        else:
            cancel_task_retry(task_id, source_label)

        history_id = uuid.uuid4().hex
        execution = TaskExecution(task_id, history_id, previous=retry_execution)
        try:
            log_file = log_file_for_task(task)
            execution.bind_log(log_file)
            log_fp = open(log_file, "ab", buffering=0)
            execution.log_fp = log_fp
        except BaseException:
            execution.release()
            raise
        history_start_ts = time.time()
        history_record = {
            "id": history_id,
            "task_id": task_id,
            "task_name": task_display_name,
            "command": task.get("command", ""),
            "source": source,
            "status": "starting",
            "retry_attempt": retry_attempt,
            "max_retries": int(retry_cfg.get("attempts", 0) or 0),
            "start_at": now_str(),
            "end_at": "",
            "duration_seconds": 0,
            "return_code": None,
            "pid": "-",
            "log_file": str(log_file),
            "message": "已提交启动",
        }
        try:
            add_task_history(history_record)
        except BaseException:
            execution.release()
            raise

        RUNNING[task_id] = {
            "process": None,
            "pid": "-",
            "process_name": process_name,
            "log_file": str(log_file),
            "log_fp": log_fp,
            "start_time": time.time(),
            "status": "starting",
            "history_id": history_id,
            "history_start_ts": history_start_ts,
            "history_record": dict(history_record),
            "execution": execution,
        }
        STOPPED_MANUALLY.discard((task_id, history_id))

        th = None
        try:
            execution.retain()
            th = threading.Thread(
                target=_start_task_worker,
                args=(task_id, dict(task), cmd_info, process_name, str(log_file), log_fp, source, history_id, history_start_ts, retry_attempt, execution),
                daemon=True,
                name=f"fls-task-start-{task_id[:8]}",
            )
            th.start()
        except Exception as exc:
            if th is None or getattr(th, "ident", None) is None:
                submission_error = exc
                RUNNING.pop(task_id, None)
                STOPPED_MANUALLY.discard((task_id, history_id))
                execution.release()

    if submission_error is not None:
        error_text = f"后台线程启动失败：{submission_error}"
        append_task_log(log_file, f"\n===== 任务启动失败: {error_text} =====\n")
        try:
            log_fp.close()
        except Exception:
            pass

        try:
            retry_scheduled, retry_msg = schedule_task_retry(
                task_id,
                task,
                retry_attempt,
                error_text,
                str(log_file),
                execution=execution,
            )
        except Exception as retry_error:
            retry_scheduled = False
            retry_msg = f"{error_text}；重试计划失败：{retry_error}"
        try:
            saved = finish_task_history(
                history_id,
                history_start_ts,
                "start_failed",
                log_file=str(log_file),
                message=retry_msg if retry_scheduled else f"{error_text}；{retry_msg}" if retry_msg else error_text,
            )
            if not saved:
                raise RuntimeError("任务启动失败后的历史结果无法确认")
            execution.terminal = "start_failed"
        except BaseException:
            execution.failed = True
            raise
        finally:
            execution.retry_ready.set()
            execution.release()
        failure_message = retry_msg if retry_scheduled else f"{error_text}；{retry_msg}" if retry_msg else error_text
        return False, f"任务启动失败：{failure_message}"

    execution.release()
    return True, "已提交启动"


def stop_task_now(task_id):
    with directory_job():
        if request_task_stop(task_id):
            with _RUNNING_LOCK:
                local = RUNNING.get(task_id)
            if local and isinstance(local.get("process"), TaskProcess):
                try:
                    local["process"].stop(timeout=2)
                except Exception:
                    pass
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if not execution_snapshot([task_id]).get(task_id, {}).get("active"):
                    return True, "已请求停止，任务进程树及日志已关闭"
                time.sleep(0.02)
            return True, "已请求停止，等待任务进程树及日志关闭"
    with _RUNNING_LOCK:
        pending_retry = PENDING_RETRIES.pop(task_id, None)
        info = RUNNING.get(task_id)
        if info:
            STOPPED_MANUALLY.add((task_id, info.get("history_id")))
            proc = info.get("process")
            if not proc and RUNNING.get(task_id) is info:
                RUNNING.pop(task_id, None)

    _cancel_task_retry_entry(pending_retry, "任务已停止或删除")

    if not info:
        if pending_retry:
            return True, "已取消计划重试"
        return False, "任务未运行"

    if proc:
        try:
            return_code = proc.poll()
        except Exception as e:
            return False, f"停止失败：读取任务进程状态失败：{e}"

        if return_code is not None:
            if info.get("watcher_done") and info.get("stop_error"):
                terminal_status = info.get("terminal_status") or "stopped"
                terminal_message = info.get("terminal_message") or f"任务已结束，退出码 {return_code}"
                if terminal_status in ("failed", "timeout"):
                    terminal_message += "；此前未确认退出，当前已确认进程退出"
                log_file = info.get("log_file") or ""
                log_fp = info.get("log_fp")
                if log_file:
                    append_task_log(log_file, f"\n===== 已确认任务进程退出，退出码 {return_code} =====\n")
                try:
                    history_saved = finish_task_history(
                        info.get("history_id"),
                        info.get("history_start_ts"),
                        terminal_status,
                        return_code=return_code,
                        log_file=log_file,
                        message=terminal_message,
                        fallback_record=info.get("history_record"),
                    )
                except Exception as e:
                    return False, f"停止失败：任务进程已退出，但历史保存失败：{e}"
                if not history_saved:
                    return False, "停止失败：任务进程已退出，但未找到可更新的历史记录"
                try:
                    if log_fp:
                        log_fp.close()
                except Exception:
                    pass
                _pop_running(task_id, history_id=info.get("history_id"))
                STOPPED_MANUALLY.discard((task_id, info.get("history_id")))
                return True, "已确认任务进程退出"
            if info.get("watcher_managed"):
                _join_task_watcher(info)
                if not _has_running_attempt(task_id, info.get("history_id")):
                    return True, "已确认任务进程退出"
                return True, "已确认任务进程退出，正在完成终态记录"
            _pop_running(task_id, history_id=info.get("history_id"))
            STOPPED_MANUALLY.discard((task_id, info.get("history_id")))
            return False, f"任务已结束，退出码 {return_code}"

        terminated, detail = terminate_task_process(proc)

        if not terminated:
            log_file = info.get("log_file")
            if log_file:
                append_task_log(
                    log_file,
                    f"\n===== 停止任务失败: {now_str()}，{detail} =====\n",
                )
            return False, f"停止失败：{detail}"

        watcher_done = bool(info.get("watcher_done"))
        if not watcher_done:
            _join_task_watcher(info)
            if not _has_running_attempt(task_id, info.get("history_id")):
                return True, "已确认任务进程退出"
            watcher_done = bool(info.get("watcher_done"))
    else:
        watcher_done = False

    try:
        log_file = info.get("log_file")
        if log_file:
            append_task_log(log_file, f"\n===== 手动结束任务: {now_str()} =====\n")
    except Exception:
        pass

    if not proc or watcher_done:
        STOPPED_MANUALLY.discard((task_id, info.get("history_id")))

        try:
            log_fp = info.get("log_fp")
            if log_fp:
                log_fp.close()
        except Exception:
            pass

        finish_task_history(
            info.get("history_id"),
            info.get("history_start_ts"),
            "stopped",
            log_file=info.get("log_file", ""),
            message="手动结束",
        )
        _pop_running(task_id, history_id=info.get("history_id"))
    elif not info.get("watcher_managed"):
        _pop_running(task_id, history_id=info.get("history_id"))

    return True, "已结束"
