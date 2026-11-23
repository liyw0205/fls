import os
import hashlib
import json
import time
import signal
import shutil
import stat
import threading
import subprocess
import uuid
from contextlib import closing

from ..paths import SCRIPT_DIR
from ..models import TaskSchedulerReloadError, load_tasks
from ..utils import now_str, LazyModule, prune_completed_records
from ..proxy import (
    requests_proxy_dict,
    apply_proxy_env,
    github_proxy_url,
    build_git_command_with_github_proxy,
    validate_proxy_id,
)
from .constants import (
    ONLINE_INSTALL_RUNNING,
    ONLINE_INSTALL_STOPPING,
    ONLINE_INSTALL_LOCK,
    ONLINE_INSTALL_TIMEOUT_SECONDS,
    active_online_install_for_script,
    get_online_install_job,
    persist_online_install,
)
from .logs import append_log
from .tasks import import_task_if_needed, validate_online_task_import

requests = LazyModule("requests")


class InstallCommandError(RuntimeError):
    def __init__(self, message, returncode=None):
        super().__init__(message)
        self.returncode = returncode


class InstallTimeout(InstallCommandError):
    pass


class InstallStopped(InstallCommandError):
    pass


def _persist_install_state(info):
    try:
        persist_online_install(info)
        return True
    except Exception as e:
        detail = f"保存安装状态失败：{e}"
        info["persistence_error"] = detail
        info["error"] = "；".join(filter(None, (info.get("error"), detail)))
        print(detail, flush=True)
        return False


def _set_install_phase(info, phase, *, persist=True):
    """Persist a coarse stage before the next irreversible install action."""
    info["phase"] = phase
    info["updated_at"] = now_str()
    return _persist_install_state(info) if persist else True


def _require_install_phase(info, phase):
    if not _set_install_phase(info, phase):
        raise RuntimeError(f"安装阶段 {phase} 回执未能确认保存，未开始后续操作")


def _fingerprint_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 128), b""):
            digest.update(chunk)
    stat_result = os.stat(path)
    return {"kind": "file", "size": stat_result.st_size, "sha256": digest.hexdigest()}


def _fingerprint_path(path):
    path = os.fspath(path)
    if os.path.islink(path):
        return {"kind": "symlink", "target": os.readlink(path)}
    if os.path.isfile(path):
        return _fingerprint_file(path)
    if os.path.isdir(path):
        entries = []
        root = os.path.abspath(path)
        for current, dirs, files in os.walk(root, followlinks=False):
            dirs.sort()
            files.sort()
            for name in dirs + files:
                child = os.path.join(current, name)
                relative = os.path.relpath(child, root).replace(os.sep, "/")
                stat_result = os.lstat(child)
                kind = "symlink" if os.path.islink(child) else "dir" if os.path.isdir(child) else "file"
                entries.append((relative, kind, stat_result.st_size, stat_result.st_mtime_ns))
        digest = hashlib.sha256(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        return {"kind": "directory", "entries": len(entries), "sha256": digest}
    return {"kind": "missing"}


def _task_snapshot_for_install(info):
    selected = set(str(value) for value in (info.get("selected_task_indexes") or []))
    if not info.get("import_task") or not selected:
        return []
    script_id = str(info.get("script_id") or "")
    tasks = []
    for task in load_tasks():
        if str(task.get("online_script_id") or "") != script_id:
            continue
        key = str(task.get("online_script_task_key") or "")
        if key.count(":") >= 2 and key.split(":", 2)[1] in selected:
            tasks.append(task)
    return tasks


def _task_snapshot_fingerprint(snapshot):
    payload = json.dumps(snapshot or [], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def capture_install_outputs(info, target=None):
    """Record the files/tasks created by this install for later recovery checks."""
    if target is None:
        target = info.get("target_path")
    if not target:
        return _persist_install_state(info)
    info["target_path"] = str(target)
    info["target_fingerprint"] = _fingerprint_path(target)
    if info.get("import_task"):
        snapshot = _task_snapshot_for_install(info)
        info["task_snapshot"] = snapshot
        info["tasks_fingerprint"] = _task_snapshot_fingerprint(snapshot)
    info["updated_at"] = now_str()
    return _persist_install_state(info)


def verify_install_outputs(info):
    """Compare durable install evidence with current files without mutating them."""
    result = {"checked": False, "changed": False, "target": "not_recorded", "tasks": "not_recorded"}
    expected_target = info.get("target_fingerprint")
    target_path = str(info.get("target_path") or "")
    if expected_target and target_path:
        try:
            target = os.path.abspath(target_path)
            base = os.path.abspath(SCRIPT_DIR)
            if target != base and not target.startswith(base + os.sep):
                raise RuntimeError("目标路径超出 scripts 目录")
            result["target"] = "unchanged" if _fingerprint_path(target) == expected_target else "changed"
        except Exception as exc:
            result["target"] = "unreadable"
            result["target_error"] = str(exc)
        result["checked"] = True
    expected_tasks = info.get("tasks_fingerprint")
    if expected_tasks:
        try:
            actual = _task_snapshot_for_install(info)
            result["tasks"] = "unchanged" if _task_snapshot_fingerprint(actual) == expected_tasks else "changed"
        except Exception as exc:
            result["tasks"] = "unreadable"
            result["tasks_error"] = str(exc)
        result["checked"] = True
    result["changed"] = result["target"] in ("changed", "unreadable") or result["tasks"] in ("changed", "unreadable")
    return result


def get_running_install_by_script_id(script_id):
    return active_online_install_for_script(script_id)


def online_install_should_stop(install_id):
    with ONLINE_INSTALL_LOCK:
        return install_id in ONLINE_INSTALL_STOPPING


def terminate_install_process(proc):
    if not proc:
        return True, ""

    errors = []
    for sig, method in ((signal.SIGTERM, "terminate"), (getattr(signal, "SIGKILL", signal.SIGTERM), "kill")):
        if proc.poll() is not None:
            return True, ""
        if os.name == "nt":
            try:
                getattr(proc, method)()
            except Exception as e:
                errors.append(f"{method}: {e}")
        else:
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except Exception as e:
                errors.append(f"killpg({sig}): {e}")
                try:
                    getattr(proc, method)()
                except Exception as fallback_error:
                    errors.append(f"{method}: {fallback_error}")
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            errors.append(f"wait: {e}")

    if proc.poll() is not None:
        return True, ""
    return False, "；".join(errors + ["无法确认安装进程已退出"])


def _interrupt_install_command(install_id, proc, reason, retry=False):
    with ONLINE_INSTALL_LOCK:
        info = ONLINE_INSTALL_RUNNING.get(install_id)
        if not info or info.get("process") is not proc or not info.get("running"):
            return True, ""
        stop_lock = info.setdefault("stop_lock", threading.Lock())

    # Serialize signals per job, never blocking the worker's state lock while waiting.
    with stop_lock:
        with ONLINE_INSTALL_LOCK:
            if not info.get("running") or info.get("process") is not proc or proc.poll() is not None:
                return True, ""
            if info.get("command_interruption") and not retry:
                return False, info.get("error", "")
            info.setdefault("command_interruption", reason)
            reason = info["command_interruption"]
        ok, detail = terminate_install_process(proc)
        with ONLINE_INSTALL_LOCK:
            if info.get("running") and info.get("process") is proc and proc.poll() is None:
                info["status"] = "超时（停止失败）" if reason == "timeout" else "停止失败"
                info["error"] = (
                    f"安装命令超时，超过 {ONLINE_INSTALL_TIMEOUT_SECONDS} 秒；{detail}"
                    if reason == "timeout" else detail
                )
                info["updated_at"] = now_str()
                _persist_install_state(info)
        return ok, detail


def request_stop_online_install(install_id):
    with ONLINE_INSTALL_LOCK:
        snapshot = get_online_install_job(install_id)
        if not snapshot:
            return False, "安装记录不存在"
        if snapshot.get("status") == "结果未知":
            return False, "安装任务结果未知，面板重启后无法停止，请检查日志和脚本目录"
        info = ONLINE_INSTALL_RUNNING.get(install_id)

        if not info or not info.get("running"):
            return False, "安装任务已结束"

        if install_id in ONLINE_INSTALL_STOPPING and info.get("status") == "停止中":
            return True, "已请求停止安装"

        previous_status = info.get("status")
        info["status"] = "停止中"
        info["updated_at"] = now_str()
        try:
            persist_online_install(info)
        except Exception:
            info["status"] = previous_status
            raise
        ONLINE_INSTALL_STOPPING.add(install_id)
        proc = info.get("process")
        log_file = info.get("log_file")

    log_error = ""
    if log_file:
        try:
            append_log(log_file, "")
            append_log(log_file, f"===== 用户请求停止安装: {now_str()} =====")
        except Exception as e:
            detail = str(e).replace("\x00", "")[:500]
            log_error = f"停止请求日志写入失败：{detail}"
            with ONLINE_INSTALL_LOCK:
                info["stop_warning"] = log_error
                info["updated_at"] = now_str()
                try:
                    persist_online_install(info)
                except Exception as persist_error:
                    receipt_detail = str(persist_error).replace("\x00", "")[:300]
                    log_error += f"；停止诊断回执保存失败：{receipt_detail}"
                    info["stop_warning"] = log_error[:1000]

    if proc:
        ok, detail = _interrupt_install_command(install_id, proc, "stop", retry=True)
        if not ok and proc.poll() is None:
            return False, "；".join(filter(None, (f"停止失败：{detail}", log_error)))

    return True, "；".join(filter(None, ("已请求停止安装", log_error)))


def online_script_target(item):
    link_name = str(
        item.get("link_name") or item.get("id") or "script"
    ).strip().strip("/")

    if not link_name or link_name in (".", ".."):
        raise RuntimeError("link_name 非法")

    target = (SCRIPT_DIR / link_name).resolve()
    base = SCRIPT_DIR.resolve()

    if target != base and not str(target).startswith(str(base) + os.sep):
        raise RuntimeError("目标路径非法")

    return target


def command_list_to_text(cmd):
    return " ".join(str(x) for x in cmd)


def run_logged_command(cmd, cwd, log_file, env=None, shell=False, install_id=""):
    append_log(log_file, "")
    append_log(
        log_file,
        f"$ {cmd if isinstance(cmd, str) else command_list_to_text(cmd)}"
    )
    append_log(log_file, f"cwd: {cwd}")
    append_log(log_file, "------------------------------------------------------------")

    with open(log_file, "ab", buffering=0) as log_fp:
        popen_kwargs = {
            "stdout": log_fp,
            "stderr": subprocess.STDOUT,
            "cwd": str(cwd),
            "env": env or os.environ.copy(),
            "shell": shell,
        }

        if os.name != "nt":
            popen_kwargs["start_new_session"] = True

        with ONLINE_INSTALL_LOCK:
            if install_id and online_install_should_stop(install_id):
                raise InstallStopped("安装已停止")
            proc = subprocess.Popen(cmd, **popen_kwargs)
            info = ONLINE_INSTALL_RUNNING.get(install_id)
            if info is not None:
                info["process"] = proc
                info.pop("command_interruption", None)

        deadline = time.monotonic() + ONLINE_INSTALL_TIMEOUT_SECONDS
        interruption_attempted = False
        while proc.poll() is None:
            stopping = install_id and online_install_should_stop(install_id)
            if not interruption_attempted and (stopping or time.monotonic() >= deadline):
                interruption_attempted = True
                reason = "stop" if stopping else "timeout"
                if info is not None:
                    with ONLINE_INSTALL_LOCK:
                        already_attempted = bool(info.get("command_interruption"))
                    if not already_attempted:
                        _interrupt_install_command(install_id, proc, reason)
                else:
                    terminate_install_process(proc)
            time.sleep(0.1)

        return_code = proc.returncode
        with ONLINE_INSTALL_LOCK:
            interruption = info.pop("command_interruption", "") if info is not None else ""
            if info is not None:
                info["process"] = None

    append_log(log_file, "------------------------------------------------------------")
    append_log(log_file, f"命令结束，退出码：{return_code}")

    if interruption == "timeout" or (info is None and interruption_attempted):
        raise InstallTimeout(f"安装命令超时，超过 {ONLINE_INSTALL_TIMEOUT_SECONDS} 秒", return_code)
    if interruption == "stop":
        raise InstallStopped("安装已停止", return_code)
    if return_code != 0:
        raise InstallCommandError(f"命令执行失败，退出码：{return_code}", return_code)


def download_online_script_logged(item, proxy_id, log_file, force=False, install_id=""):
    validate_proxy_id(proxy_id)
    SCRIPT_DIR.mkdir(parents=True, exist_ok=True)

    script_type = item.get("type")
    link = item.get("link")
    target = online_script_target(item)

    append_log(log_file, "")
    append_log(log_file, "===== 下载在线脚本 =====")
    append_log(log_file, f"脚本类型: {script_type}")
    append_log(log_file, f"原始链接: {link}")
    append_log(log_file, f"保存目标: {target}")
    append_log(log_file, f"代理ID: {proxy_id or '不使用代理'}")
    append_log(log_file, f"允许覆盖/更新: {'是' if force else '否'}")
    append_log(log_file, "============================================================")

    if install_id and online_install_should_stop(install_id):
        raise InstallStopped("安装已停止")

    if target.exists() and not force:
        raise FileExistsError(f"目标已存在，为避免意外覆盖已停止：{target}")

    if script_type == "raw":
        if target.exists() and target.is_dir():
            raise RuntimeError(f"目标已存在且是文件夹，无法覆盖为文件：{target}")

        target.parent.mkdir(parents=True, exist_ok=True)

        real_url = github_proxy_url(link, proxy_id, verify=True)

        if real_url == link:
            append_log(log_file, "GitHub 代理不可用或未启用，使用原始下载地址")
        else:
            append_log(log_file, f"使用 GitHub 代理下载地址：{real_url}")

        append_log(log_file, f"开始下载文件：{real_url}")

        temp_file = target.with_name(f".fls-download-{uuid.uuid4().hex}.part")
        temp_created = False
        try:
            with closing(requests.get(
                real_url,
                timeout=60,
                stream=True,
                headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
                proxies=requests_proxy_dict(proxy_id),
            )) as r:
                r.raise_for_status()
                with temp_file.open("xb") as f:
                    temp_created = True
                    for chunk in r.iter_content(chunk_size=1024 * 128):
                        if install_id and online_install_should_stop(install_id):
                            raise InstallStopped("安装已停止")
                        if chunk:
                            f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())

            if target.exists():
                temp_file.chmod(stat.S_IMODE(target.stat().st_mode))
            if target.suffix.lower() in (".sh", ".bash"):
                temp_file.chmod(temp_file.stat().st_mode | 0o755)

            # Stop and publication have one ordering; link provides atomic no-overwrite.
            with ONLINE_INSTALL_LOCK:
                if install_id and online_install_should_stop(install_id):
                    raise InstallStopped("安装已停止")
                if force:
                    os.replace(temp_file, target)
                else:
                    os.link(temp_file, target)
        finally:
            if temp_created:
                try:
                    temp_file.unlink(missing_ok=True)
                except OSError as e:
                    append_log(log_file, f"清理临时下载文件失败：{temp_file}：{e}")

        append_log(log_file, f"文件下载完成：{target}")
        return target

    if script_type == "repo":
        git_bin = shutil.which("git")
        if not git_bin:
            raise RuntimeError("未安装 git，无法下载仓库")

        env = os.environ.copy()
        env = apply_proxy_env(env, proxy_id)

        if target.exists():
            if not (target / ".git").exists():
                raise RuntimeError(f"目标目录已存在且不是 git 仓库，请手动处理后重试：{target}")

            append_log(log_file, "目标 Git 仓库已存在，执行 git pull 更新")

            git_cmd = build_git_command_with_github_proxy(
                git_bin,
                proxy_id,
                ["pull"],
                verify=True,
            )

            if len(git_cmd) > 1:
                append_log(log_file, "GitHub 代理可用，使用 git 临时配置方式更新仓库")
            else:
                append_log(log_file, "GitHub 代理不可用或未启用，直接执行原始 git pull")

            run_logged_command(
                git_cmd,
                cwd=target,
                log_file=log_file,
                env=env,
                shell=False,
                install_id=install_id,
            )
        else:
            append_log(log_file, "目标目录不存在，执行 git clone")

            git_cmd = build_git_command_with_github_proxy(
                git_bin,
                proxy_id,
                ["clone", link, str(target)],
                verify=True,
            )

            if len(git_cmd) > 1:
                append_log(log_file, "GitHub 代理可用，使用 git 临时配置方式 clone 仓库")
            else:
                append_log(log_file, "GitHub 代理不可用或未启用，直接执行原始 git clone")

            run_logged_command(
                git_cmd,
                cwd=SCRIPT_DIR,
                log_file=log_file,
                env=env,
                shell=False,
                install_id=install_id,
            )

        append_log(log_file, f"仓库下载/更新完成：{target}")
        return target

    raise RuntimeError("未知脚本类型")


def install_worker(
    install_id,
    item,
    proxy_id="",
    import_task=False,
    force=False,
    enable_task=False,
    selected_task_indexes=None,
):
    with ONLINE_INSTALL_LOCK:
        info = ONLINE_INSTALL_RUNNING.get(install_id)
        if not isinstance(info, dict):
            return
        log_file = info["log_file"]

    try:
        with ONLINE_INSTALL_LOCK:
            if online_install_should_stop(install_id):
                raise InstallStopped("安装已停止")
            info.update(running=True, status="运行中", phase="starting", error="", updated_at=now_str())
            persist_online_install(info)

        append_log(log_file, "===== 在线脚本下载安装 =====")
        append_log(log_file, f"时间: {now_str()}")
        append_log(log_file, f"安装ID: {install_id}")
        append_log(log_file, f"脚本ID: {item.get('id')}")
        append_log(log_file, f"脚本名称: {item.get('name')}")
        append_log(log_file, f"类型: {item.get('type')}")
        append_log(log_file, f"链接: {item.get('link')}")
        append_log(log_file, f"保存名: {item.get('link_name')}")
        append_log(log_file, f"脚本目录: {SCRIPT_DIR}")
        append_log(log_file, f"代理ID: {proxy_id or '不使用代理'}")
        append_log(log_file, f"导入任务: {'是' if import_task else '否'}")
        append_log(log_file, f"导入后启用任务: {'是' if enable_task else '否'}")
        append_log(log_file, f"允许覆盖/更新: {'是' if force else '否'}")
        append_log(log_file, "============================================================")

        if import_task:
            _require_install_phase(info, "validating_tasks")
            validate_online_task_import(item, selected_task_indexes)

        _require_install_phase(info, "downloading")
        target = download_online_script_logged(
            item=item,
            proxy_id=proxy_id,
            log_file=log_file,
            force=force,
            install_id=install_id,
        )
        if not capture_install_outputs(info, target):
            if import_task:
                info["phase"] = "importing_tasks"
            raise RuntimeError("下载产物回执未能确认保存，未导入任务或执行安装命令")

        if online_install_should_stop(install_id):
            raise InstallStopped("安装已停止")

        if import_task:
            _require_install_phase(info, "importing_tasks")
            append_log(log_file, "")
            append_log(log_file, "===== 导入任务 =====")
            try:
                import_task_if_needed(
                    item,
                    log_file=log_file,
                    enable_task=enable_task,
                    selected_task_indexes=selected_task_indexes,
                    raise_on_reload_error=True,
                )
                if not capture_install_outputs(info, target):
                    raise RuntimeError("任务导入产物回执未能确认保存，未执行安装命令")
            except TaskSchedulerReloadError as e:
                capture_install_outputs(info, target)
                with ONLINE_INSTALL_LOCK:
                    info.update(
                        phase="scheduler_reload_pending",
                        scheduler_reload_pending=True,
                        scheduler_reload_error=f"任务已保存，但调度器重载失败：{e}",
                        updated_at=now_str(),
                    )
                    if not _persist_install_state(info):
                        raise RuntimeError("调度器待恢复回执未能确认保存，未执行后续安装命令")

        if online_install_should_stop(install_id):
            raise InstallStopped("安装已停止")

        install_cmd = str(item.get("install") or "").strip()

        if install_cmd:
            _require_install_phase(info, "running_command")
            append_log(log_file, "")
            append_log(log_file, "===== 执行安装命令 =====")
            append_log(log_file, f"安装命令: {install_cmd}")
            append_log(log_file, f"工作目录: {SCRIPT_DIR}")

            env = os.environ.copy()
            env = apply_proxy_env(env, proxy_id)

            run_logged_command(
                ["sh", "-lc", install_cmd],
                cwd=SCRIPT_DIR,
                log_file=log_file,
                env=env,
                shell=False,
                install_id=install_id,
            )
        else:
            append_log(log_file, "")
            append_log(log_file, "该脚本未提供 install 命令，跳过安装步骤")

        capture_install_outputs(info, target)

        with ONLINE_INSTALL_LOCK:
            if online_install_should_stop(install_id):
                raise InstallStopped("安装已停止")
            info = ONLINE_INSTALL_RUNNING.get(install_id)
            if not isinstance(info, dict):
                return
            info.update({
                "running": False,
                "phase": "completed" if not info.get("scheduler_reload_pending") else "scheduler_reload_pending",
                "status": "部分完成" if info.get("scheduler_reload_pending") else "已完成",
                "returncode": 0,
                "error": "；".join(filter(None, (
                    info.get("scheduler_reload_error"), info.get("persistence_error"),
                ))),
                "process": None,
                "updated_at": now_str(),
            })
            persisted = _persist_install_state(info)
            if not persisted:
                info.update(
                    status="结果未知",
                    returncode=None,
                    error="；".join(filter(None, (
                        info.get("error"),
                        "安装副作用已执行，但终态尚未可靠保存，请查看日志后重试",
                    ))),
                )
                if not _persist_install_state(info):
                    info["recovery_persistence_error"] = info.get("persistence_error") or "安装恢复回执未能核对"

        append_log(log_file, "")
        result = (
            "结果未知"
            if info.get("status") == "结果未知"
            else "安装完成，调度器待重载"
            if info.get("scheduler_reload_pending")
            else "全部完成"
        )
        append_log(log_file, f"===== {result}: {now_str()} =====")

    except Exception as e:
        stopped = isinstance(e, InstallStopped)

        with ONLINE_INSTALL_LOCK:
            info = ONLINE_INSTALL_RUNNING.get(install_id)
            if not isinstance(info, dict):
                return
            # Logging after a committed terminal result must not overwrite that result.
            if not info.get("running"):
                return
            info.update({
                "running": False,
                "phase": (
                    info.get("phase") if not stopped and not isinstance(e, InstallTimeout)
                    and info.get("phase") == "importing_tasks"
                    else "stopped" if stopped else ("timeout" if isinstance(e, InstallTimeout) else "failed")
                ),
                "status": "已停止" if stopped else ("超时" if isinstance(e, InstallTimeout) else "失败"),
                "returncode": getattr(e, "returncode", None),
                "error": "；".join(dict.fromkeys(filter(None, (
                    str(e), info.get("error"), info.get("persistence_error"),
                )))),
                "process": None,
                "updated_at": now_str(),
            })
            persisted = _persist_install_state(info)
            if not persisted:
                info.update(
                    phase="unknown",
                    status="结果未知",
                    returncode=None,
                    error="；".join(filter(None, (
                        info.get("error"),
                        "安装结果已执行，但终态尚未可靠保存，请查看日志后重试",
                    ))),
                )
                if not _persist_install_state(info):
                    info["recovery_persistence_error"] = info.get("persistence_error") or "安装恢复回执未能核对"

        try:
            append_log(log_file, f"===== {info['status']}: {now_str()} =====")
            append_log(log_file, f"错误: {e}")
        except Exception as log_error:
            print(f"写入安装日志失败：{log_error}；原错误：{e}", flush=True)

    finally:
        with ONLINE_INSTALL_LOCK:
            ONLINE_INSTALL_STOPPING.discard(install_id)


def script_has_install(item):
    return bool(str(item.get("install") or "").strip())


def script_has_doc(item):
    return bool(str(item.get("doc_link") or "").strip())


def start_install_thread(
    install_id,
    item,
    proxy_id="",
    import_task=False,
    force=False,
    enable_task=False,
    selected_task_indexes=None,
):
    th = threading.Thread(
        target=install_worker,
        args=(
            install_id,
            dict(item),
            proxy_id,
            import_task,
            force,
            enable_task,
            selected_task_indexes,
        ),
        daemon=True,
        name=f"fls-online-install-{install_id[:8]}",
    )
    th.start()
    return th
