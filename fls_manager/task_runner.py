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
    load_tasks,
    save_tasks,
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


STOPPED_MANUALLY = set()


def safe_process_name(name):
    name = safe_name(name)
    return (TASK_PROCESS_PREFIX + name)[:120]


def is_running(task_id):
    info = RUNNING.get(task_id)

    if not info:
        return False

    status = info.get("status")
    if status in ("starting", "delaying"):
        return True

    proc = info.get("process")

    if not proc:
        RUNNING.pop(task_id, None)
        return False

    if proc.poll() is None:
        return True

    return False


def increase_run_count(task_id):
    tasks = load_tasks()

    for item in tasks:
        if item.get("id") == task_id:
            item["run_count"] = int(item.get("run_count", 0)) + 1
            item["last_run_at"] = now_str()
            item["updated_at"] = now_str()
            break

    save_tasks(tasks)


def append_task_log(log_file, text):
    try:
        with open(log_file, "ab") as f:
            f.write(str(text).encode("utf-8", errors="replace"))

            if not str(text).endswith("\n"):
                f.write(b"\n")

    except Exception as e:
        print(f"[TaskLog] 写任务日志失败: {e}")


def finish_task_history(history_id, start_ts, status, return_code=None, log_file="", message=""):
    if not history_id:
        return

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

    update_task_history(history_id, updates)


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


def schedule_task_retry(task_id, task_snapshot, retry_attempt, reason, log_file):
    cfg = task_retry_config(task_snapshot)
    max_retries = int(cfg.get("attempts", 0) or 0)

    if retry_attempt >= max_retries:
        return False, ""

    next_attempt = retry_attempt + 1
    interval_seconds = int(cfg.get("interval_seconds", 60) or 60)

    append_task_log(
        log_file,
        (
            f"\n===== 任务失败，计划第 {next_attempt}/{max_retries} 次重试 =====\n"
            f"原因: {reason}\n"
            f"间隔: {interval_seconds} 秒\n"
            f"时间: {now_str()}\n"
        ),
    )

    def retry_worker():
        try:
            latest_task = get_task(task_id)

            if not latest_task:
                append_task_log(log_file, "\n===== 重试取消：任务已不存在 =====\n")
                return

            if not latest_task.get("enabled", True):
                append_task_log(log_file, "\n===== 重试取消：任务已禁用 =====\n")
                return

            ok, msg = run_task_now(task_id, source="retry", retry_attempt=next_attempt)

            if not ok:
                append_task_log(
                    log_file,
                    f"\n===== 重试启动失败: {now_str()}，{msg} =====\n",
                )

        except Exception as e:
            append_task_log(log_file, f"\n===== 重试调度失败: {e} =====\n")

    timer = threading.Timer(interval_seconds, retry_worker)
    timer.daemon = True
    timer.start()

    return True, f"{reason}，已计划第 {next_attempt}/{max_retries} 次重试"


def force_kill_process(proc):
    if not proc:
        return

    try:
        if proc.poll() is not None:
            return

        if os.name == "nt":
            try:
                proc.terminate()
            except Exception:
                pass

            time.sleep(1)

            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass

        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass

            time.sleep(1)

            if proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    except Exception:
        pass


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

            force_kill_process(proc)
            return_code = proc.wait()

        manual_stopped = task_id in STOPPED_MANUALLY

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

        retry_scheduled = False

        if not manual_stopped and history_status in ("failed", "timeout"):
            retry_scheduled, retry_msg = schedule_task_retry(
                task_id,
                task_snapshot,
                retry_attempt,
                history_msg,
                log_file,
            )

            if retry_scheduled:
                history_msg = retry_msg

        finish_task_history(
            history_id,
            history_start_ts,
            history_status,
            return_code=return_code,
            log_file=log_file,
            message=history_msg,
        )

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

        try:
            RUNNING.pop(task_id, None)
        except Exception:
            pass

        if manual_stopped:
            STOPPED_MANUALLY.discard(task_id)
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


def _start_task_worker(task_id, task_snapshot, cmd_info, process_name, log_file, log_fp, source, history_id, history_start_ts, retry_attempt):
    try:
        env = os.environ.copy()
        env.update(load_global_env())
        env.update(task_snapshot.get("env", {}) or {})
        env = apply_proxy_env(env, task_snapshot.get("proxy_id", ""))

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
            if task_id not in RUNNING:
                log_fp.write("===== 任务在随机延迟前被取消 =====\n".encode("utf-8"))
                log_fp.close()
                finish_task_history(history_id, history_start_ts, "stopped", log_file=log_file, message="随机延迟前被取消")
                return

            RUNNING[task_id]["status"] = "delaying"
            update_task_history(history_id, {"status": "delaying"})

            log_fp.write(
                f"===== 随机延迟: {delay_seconds} 秒，开始等待 =====\n".encode("utf-8")
            )

            time.sleep(delay_seconds)

            if task_id not in RUNNING:
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

        if task_id not in RUNNING:
            try:
                log_fp.write("===== 任务在启动前被取消 =====\n".encode("utf-8"))
                log_fp.close()
            except Exception:
                pass
            finish_task_history(history_id, history_start_ts, "stopped", log_file=log_file, message="启动前被取消")
            return

        RUNNING[task_id]["status"] = "starting"
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

        proc = subprocess.Popen(
            cmd_info["cmd"],
            **popen_kwargs
        )

        if task_id not in RUNNING:
            force_kill_process(proc)
            try:
                log_fp.write("===== 任务启动后立即被取消 =====\n".encode("utf-8"))
                log_fp.close()
            except Exception:
                pass
            finish_task_history(history_id, history_start_ts, "stopped", return_code=proc.poll(), log_file=log_file, message="启动后立即被取消")
            return

        RUNNING[task_id].update({
            "process": proc,
            "pid": proc.pid,
            "process_name": process_name,
            "log_file": str(log_file),
            "log_fp": log_fp,
            "start_time": time.time(),
            "status": "running",
        })
        update_task_history(history_id, {
            "status": "running",
            "pid": proc.pid,
        })

        increase_run_count(task_id)

        th = threading.Thread(
            target=task_finish_watcher,
            args=(task_id, dict(task_snapshot), proc, str(log_file), log_fp, history_id, history_start_ts, retry_attempt),
            daemon=True,
            name=f"fls-task-watch-{task_id[:8]}",
        )
        th.start()

    except Exception as e:
        try:
            log_fp.write(f"启动失败: {e}\n".encode("utf-8"))
            log_fp.close()
        except Exception:
            pass

        retry_scheduled, retry_msg = schedule_task_retry(
            task_id,
            task_snapshot,
            retry_attempt,
            f"启动失败：{e}",
            log_file,
        )

        finish_task_history(
            history_id,
            history_start_ts,
            "start_failed",
            log_file=log_file,
            message=retry_msg if retry_scheduled else f"启动失败：{e}",
        )

        try:
            RUNNING.pop(task_id, None)
        except Exception:
            pass


def run_task_now(task_id, source="manual", retry_attempt=0):
    task = get_task(task_id)

    if not task:
        return False, "任务不存在"

    if is_running(task_id):
        return False, "任务已在运行中"

    try:
        cmd_info = build_command(task)
    except Exception as e:
        return False, f"命令解析失败：{e}"

    STOPPED_MANUALLY.discard(task_id)
    retry_attempt = max(0, int(retry_attempt or 0))

    task_display_name = task.get("name") or task.get("command") or task_id
    process_name = safe_process_name(task_display_name)
    retry_cfg = task_retry_config(task)

    log_file = log_file_for_task(task)
    log_fp = open(log_file, "ab", buffering=0)
    history_id = uuid.uuid4().hex
    history_start_ts = time.time()

    add_task_history({
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
    })

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
    }

    th = threading.Thread(
        target=_start_task_worker,
        args=(task_id, dict(task), cmd_info, process_name, str(log_file), log_fp, source, history_id, history_start_ts, retry_attempt),
        daemon=True,
        name=f"fls-task-start-{task_id[:8]}",
    )
    th.start()

    return True, "已提交启动"


def stop_task_now(task_id):
    info = RUNNING.get(task_id)

    if not info:
        return False, "任务未运行"

    STOPPED_MANUALLY.add(task_id)

    proc = info.get("process")

    if proc:
        try:
            force_kill_process(proc)
        except Exception as e:
            return False, f"结束失败：{e}"

    try:
        log_file = info.get("log_file")
        if log_file:
            append_task_log(log_file, f"\n===== 手动结束任务: {now_str()} =====\n")
    except Exception:
        pass

    if not proc:
        STOPPED_MANUALLY.discard(task_id)

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

    try:
        RUNNING.pop(task_id, None)
    except Exception:
        pass

    return True, "已结束"
