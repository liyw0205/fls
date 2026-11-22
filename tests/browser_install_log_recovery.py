"""Real HTTP/Chromium recovery for online and dependency install logs."""
import os
import json
import hashlib
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace

from tools.browser_regression import TOKEN, find_chromium, find_playwright_core, seed_browser_fixture


def main():
    playwright, chromium = find_playwright_core(), find_chromium()
    if not playwright or not chromium:
        raise SystemExit("Set FLS_PLAYWRIGHT_CORE and FLS_CHROMIUM")
    with tempfile.TemporaryDirectory(prefix="fls-install-log-browser-") as temp:
        seed_browser_fixture(temp)
        os.environ.update(FLS_BASE_DIR=temp, FLS_TOKEN=TOKEN, FLS_SECRET_KEY="install-log-browser")
        from flask import Response, jsonify, request
        from werkzeug.serving import make_server
        from fls_manager.app import create_app
        import fls_manager.app as app_module
        from fls_manager import scheduler
        from fls_manager import storage
        from fls_manager.install_records import INSTALL_JOBS_FILE, persist_install_job
        from fls_manager.online_scripts import install, source, tasks as online_tasks
        from fls_manager.models import TASK_FILE, load_tasks
        from fls_manager.online_scripts.constants import (
            ONLINE_INSTALL_JOBS_FILE, ONLINE_INSTALL_RETRYING, ONLINE_INSTALL_RUNNING,
            _load_persisted_online_install, persist_online_install,
        )
        from fls_manager.online_scripts.install import capture_install_outputs
        from fls_manager.online_scripts.logs import online_install_log_file
        from fls_manager.routes.online_scripts import install as install_routes
        from fls_manager.routes import deps as deps_routes
        from fls_manager.state import DEPS_RUNNING

        base_dir = Path(temp)
        release = base_dir / "release"
        command = (
            "import pathlib,time,sys; "
            f"p=pathlib.Path({str(release)!r}); "
            "print('live installer output',flush=True); "
            "deadline=time.monotonic()+90; "
            "exec('while not p.exists() and time.monotonic()<deadline: time.sleep(0.05)'); "
            "sys.exit(int(p.read_text()) if p.exists() else 99)"
        )
        item = {
            "id": "browser-recovery", "name": "Install recovery", "type": "raw",
            "link": "https://fixture.invalid/demo.py", "link_name": "demo.py",
            "install": shlex.join([sys.executable, "-u", "-c", command]),
        }
        task_items = {
            width: {
                "id": f"browser-task-recovery-{width}", "name": f"Task recovery {width}",
                "type": "raw", "link": "https://fixture.invalid/task-recovery.sh",
                "link_name": "task-recovery.sh",
                "task_cron": [{"name": "Recovery task", "command": "task recovery.sh"}],
            }
            for width in (390, 1280)
        }
        source.save_online_script_cache([item, *task_items.values()])
        downloads = []
        task_reload_state = {"failures": 0, "calls": 0}
        receipt_fault = {"install_id": "", "remaining": 0}
        scheduler_receipt_fault = {"install_id": "", "remaining": 0, "mode": "after"}
        restart_receipt_fault = {"install_id": "", "remaining": 0, "mode": "after"}
        stop_log_failure = {"remaining": 0}
        dependency_receipt_fault = {
            "install_id": "", "remaining": 0, "status": "",
            "error": "browser terminal install receipt unavailable",
        }
        dependency_receipt_process = {"polls": 0}
        dependency_start_count = {"calls": 0}
        persist_dependency_job = deps_routes.persist_install_job

        def persist_dependency_with_failure(info):
            expected_status = dependency_receipt_fault["status"]
            if (
                info.get("id") == dependency_receipt_fault["install_id"]
                and dependency_receipt_fault["remaining"]
                and (not expected_status or info.get("status") == expected_status)
            ):
                dependency_receipt_fault["remaining"] -= 1
                raise PermissionError(dependency_receipt_fault["error"])
            return persist_dependency_job(info)

        deps_routes.persist_install_job = persist_dependency_with_failure
        write = storage._write_json_unlocked

        def write_with_receipt_failure(path, data):
            install_id = receipt_fault["install_id"]
            if (path == ONLINE_INSTALL_JOBS_FILE and receipt_fault["remaining"]
                    and data.get(install_id, {}).get("phase") == "completed"):
                receipt_fault["remaining"] -= 1
                raise PermissionError("browser completion receipt unavailable")
            install_id = scheduler_receipt_fault["install_id"]
            if (path == ONLINE_INSTALL_JOBS_FILE and scheduler_receipt_fault["remaining"]
                    and data.get(install_id, {}).get("scheduler_reload_pending") is False):
                scheduler_receipt_fault["remaining"] -= 1
                if scheduler_receipt_fault["mode"] == "after":
                    write(path, data)
                raise PermissionError("browser scheduler receipt unavailable")
            install_id = restart_receipt_fault["install_id"]
            if (path == ONLINE_INSTALL_JOBS_FILE and restart_receipt_fault["remaining"]
                    and data.get(install_id, {}).get("status") == "结果未知"):
                restart_receipt_fault["remaining"] -= 1
                if restart_receipt_fault["mode"] == "after":
                    write(path, data)
                raise PermissionError("browser restart receipt acknowledgement lost")
            return write(path, data)

        storage._write_json_unlocked = write_with_receipt_failure

        def task_import_reload():
            task_reload_state["calls"] += 1
            if task_reload_state["failures"]:
                task_reload_state["failures"] -= 1
                raise RuntimeError("browser scheduler unavailable")

        def recovered_task_reload():
            task_reload_state["calls"] += 1

        online_tasks.reload_scheduler = task_import_reload
        install_routes.reload_scheduler = recovered_task_reload

        append_log = install.append_log

        def append_log_with_stop_failure(log_file, message):
            if stop_log_failure["remaining"] and str(message).startswith("===== 用户请求停止安装:"):
                stop_log_failure["remaining"] -= 1
                raise PermissionError("browser stop log unavailable")
            return append_log(log_file, message)

        install.append_log = append_log_with_stop_failure

        def download(**kwargs):
            downloads.append(kwargs["install_id"])
            target = base_dir / "scripts" / "demo.py"
            target.write_text("print('local fixture')\n", encoding="utf-8")
            return target

        install.download_online_script_logged = download
        app = create_app()
        retry_parent_id = "browser-retry-parent"
        retry_child_id = "browser-retry-child"
        retry_parent_log = base_dir / "log" / f"deps-install-demo-{retry_parent_id}.log"
        retry_child_log = base_dir / "log" / f"deps-install-demo-{retry_child_id}.log"

        def seed_retry_chain():
            INSTALL_JOBS_FILE.write_text("{}", encoding="utf-8")
            retry_parent_log.parent.mkdir(parents=True, exist_ok=True)
            retry_parent_log.write_text("parent attempt failed\n", encoding="utf-8")
            retry_child_log.write_text("retry attempt failed\n", encoding="utf-8")
            persist_install_job({
                "id": retry_parent_id, "request_id": retry_parent_id, "kind": "dependency",
                "package": "demo", "log_file": str(retry_parent_log), "running": False,
                "finished": True, "status": "failed", "returncode": 1,
                "error": "parent attempt failed", "start_time": 1,
                "updated_at": "2026-11-18 20:10:00",
            })
            persist_install_job({
                "id": retry_child_id, "request_id": retry_child_id, "kind": "dependency",
                "retry_of": retry_parent_id, "package": "demo", "log_file": str(retry_child_log),
                "running": False, "finished": True, "status": "failed", "returncode": 2,
                "error": "retry attempt failed", "start_time": 2,
                "updated_at": "2026-11-18 20:11:00",
            })

        def seed_online_history():
            jobs = {}

            def online_record(install_id, started, updated, retry_of="", retried_by=""):
                log_file = base_dir / "log" / f"{install_id}.log"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                log_file.write_text(f"log for {install_id}\n", encoding="utf-8")
                return {
                    "id": install_id, "script_id": "browser-recovery", "script_name": "Install recovery",
                    "log_file": str(log_file), "proxy_id": "", "import_task": False,
                    "enable_task": False, "selected_task_indexes": [], "selected_tasks": [],
                    "force": False, "retry_of": retry_of, "retried_by": retried_by,
                    "running": False, "status": "失败", "phase": "failed",
                    "start_time": started, "returncode": 1, "error": "fixture failure",
                    "scheduler_reload_pending": False, "scheduler_reload_error": "",
                    "updated_at": updated,
                }

            chain = [
                online_record("online-old-parent", 1, "2026-11-01 00:00:00", retried_by="online-old-child"),
                online_record("online-old-child", 2, "2026-11-01 00:01:00", retry_of="online-old-parent"),
                online_record("online-latest-parent", 200, "2026-11-18 20:00:00", retried_by="online-latest-middle"),
                online_record("online-latest-middle", 201, "2026-11-18 20:01:00", retry_of="online-latest-parent", retried_by="online-latest-final"),
                online_record("online-latest-final", 202, "2026-11-18 20:02:00", retry_of="online-latest-middle"),
            ]
            jobs.update((item["id"], item) for item in chain)
            for index in range(95):
                item = online_record(f"online-filler-{index:03d}", 10 + index, "2026-11-10 00:00:00")
                jobs[item["id"]] = item
            ONLINE_INSTALL_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
            ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
            ONLINE_INSTALL_RUNNING.clear()
            ONLINE_INSTALL_RETRYING.clear()
            (base_dir / "scripts" / "demo.py").unlink(missing_ok=True)

        def seed_task_retry_recovery(width, missing_log=False):
            task_item = task_items[width]
            install_id = ("a" if width == 390 else "b") * 32
            target = base_dir / "scripts" / f"task-recovery-{width}.sh"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("task recovery fixture\n", encoding="utf-8")
            log_file = online_install_log_file(install_id, task_item["name"])
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("download completed\n", encoding="utf-8")
            if missing_log:
                log_file.unlink()
            job = {
                "id": install_id, "script_id": task_item["id"], "script_name": task_item["name"],
                "log_file": str(log_file), "proxy_id": "", "import_task": True,
                "enable_task": False, "selected_task_indexes": ["1"],
                "selected_tasks": [online_tasks.online_script_task_crons(task_item)[0]], "force": False,
                "running": False, "status": "失败", "phase": "importing_tasks",
                "target_path": str(target), "start_time": 1, "returncode": 1,
                "error": "task import failed", "scheduler_reload_pending": False,
                "scheduler_reload_error": "", "updated_at": "2026-11-20 00:00:00",
            }
            capture_install_outputs(job, target)
            task_reload_state.update(failures=1, calls=0)
            return install_id

        def seed_restart_receipt_recovery(width):
            mode = "before" if fault.get("persist_mode") == "before" else "after"
            prefix = ("g" if width == 390 else "h") if mode == "before" else ("e" if width == 390 else "f")
            install_id = prefix * 32
            log_file = online_install_log_file(install_id, item["name"])
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text("installer started before panel restart\n", encoding="utf-8")
            info = {
                "id": install_id, "script_id": item["id"], "script_name": item["name"],
                "log_file": str(log_file), "proxy_id": "", "import_task": False,
                "enable_task": False, "selected_task_indexes": [], "selected_tasks": [],
                "force": False, "retry_of": "", "retried_by": "", "running": True,
                "status": "运行中", "phase": "installing", "target_path": "",
                "start_time": 1, "returncode": None, "error": "",
                "scheduler_reload_pending": False, "scheduler_reload_error": "",
                "updated_at": "2026-11-21 00:00:00",
            }
            persist_online_install(info)
            ONLINE_INSTALL_RUNNING.clear()
            ONLINE_INSTALL_RETRYING.clear()
            restart_receipt_fault.update(
                install_id=install_id,
                remaining=int(fault.get("persist_failures") or 1),
                mode=mode,
            )
            return install_id

        def seed_delayed_online_log(width):
            install_id = ("c" if width == 390 else "d") * 32
            log_file = online_install_log_file(install_id, item["name"])
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.unlink(missing_ok=True)
            info = {
                "id": install_id, "script_id": item["id"], "script_name": item["name"],
                "log_file": str(log_file), "proxy_id": "", "import_task": False,
                "enable_task": False, "selected_task_indexes": [], "selected_tasks": [],
                "force": False, "retry_of": "", "retried_by": "", "running": True,
                "status": "运行中", "phase": "downloading", "target_path": "",
                "start_time": 1, "returncode": None, "error": "",
                "scheduler_reload_pending": False, "scheduler_reload_error": "",
                "updated_at": "2026-11-21 00:00:00",
            }
            ONLINE_INSTALL_RUNNING[install_id] = info
            persist_online_install(info)
            return install_id

        app_module.dispatch_frontend_open_event = lambda: None
        fault = {"status": 0}
        dependency_processes = []

        class CancelProcess:
            def __init__(self, process, fail_first_terminate=False):
                self.process = process
                self.fail_first_terminate = fail_first_terminate

            def poll(self):
                return self.process.poll()

            def terminate(self):
                if self.fail_first_terminate:
                    self.fail_first_terminate = False
                    raise OSError("fixture terminate denied")
                return self.process.terminate()

            def wait(self, timeout=None):
                return self.process.wait(timeout=timeout)

            def kill(self):
                return self.process.kill()

        @app.before_request
        def log_failure():
            if request.path.startswith(("/api/deps/install-log/", "/api/online-scripts/log/")):
                if fault["status"] == 502:
                    return Response("fixture upstream unavailable", status=502, content_type="text/plain")
                if fault["status"] == 200:
                    return Response("not JSON", content_type="text/html")

        @app.route("/api/fixture/install-logs", methods=["GET", "POST"])
        def fixture():
            if request.method == "POST":
                data = request.get_json()
                fault["status"] = int(data.get("status") or 0)
                fault["persist_mode"] = data.get("persist_mode") or "after"
                fault["persist_failures"] = int(data.get("persist_failures") or 1)
                if "stop_log_failure" in data:
                    stop_log_failure["remaining"] = int(data["stop_log_failure"] or 0)
                if data.get("seed_retry_chain"):
                    seed_retry_chain()
                if data.get("seed_online_history"):
                    seed_online_history()
                if data.get("seed_task_retry_recovery"):
                    install_id = seed_task_retry_recovery(int(data["width"]), missing_log=bool(data.get("missing_log")))
                    if data.get("fail_task_receipt"):
                        task_reload_state["failures"] = 0
                        receipt_fault.update(install_id=install_id, remaining=1)
                    if data.get("fail_scheduler_receipt_after_commit"):
                        scheduler_receipt_fault.update(install_id=install_id, remaining=1, mode="after")
                if data.get("seed_restart_receipt_recovery"):
                    seed_restart_receipt_recovery(int(data["width"]))
                if data.get("restart_task_install"):
                    ONLINE_INSTALL_RUNNING.clear()
                    ONLINE_INSTALL_RETRYING.clear()
                if data.get("seed_malformed_task_recovery"):
                    install_id = ("i" if int(data["width"]) == 390 else "j") * 32
                    jobs = json.loads(ONLINE_INSTALL_JOBS_FILE.read_text(encoding="utf-8")) if ONLINE_INSTALL_JOBS_FILE.exists() else {}
                    jobs[install_id] = {
                        "id": install_id, "script_id": "demo", "script_name": "Malformed recovery",
                        "log_file": str(base_dir / "log" / f"{install_id}.log"),
                        "running": False, "status": "失败", "task_recovery": "truncated-receipt",
                    }
                    ONLINE_INSTALL_JOBS_FILE.write_text(json.dumps(jobs), encoding="utf-8")
                if data.get("seed_malformed_task_recovery_memory"):
                    install_id = ("k" if int(data["width"]) == 390 else "l") * 32
                    ONLINE_INSTALL_RUNNING[install_id] = {
                        "id": install_id, "script_id": "demo", "script_name": "Malformed live recovery",
                        "log_file": str(base_dir / "log" / f"{install_id}.log"),
                        "running": True, "status": "运行中", "task_recovery": "truncated-receipt",
                    }
                if data.get("clear_malformed_task_recovery_memory"):
                    install_id = ("k" if int(data["width"]) == 390 else "l") * 32
                    ONLINE_INSTALL_RUNNING.pop(install_id, None)
                if data.get("seed_delayed_online_log"):
                    seed_delayed_online_log(int(data["width"]))
                if data.get("publish_delayed_online_log"):
                    install_id = "c" * 32 if int(data["width"]) == 390 else "d" * 32
                    job = ONLINE_INSTALL_RUNNING[install_id]
                    Path(job["log_file"]).write_text("installer log became available\n", encoding="utf-8")
                if data.get("complete_delayed_online_log"):
                    install_id = "c" * 32 if int(data["width"]) == 390 else "d" * 32
                    job = ONLINE_INSTALL_RUNNING[install_id]
                    job.update(
                        running=False, status="已完成", phase="completed", returncode=0,
                        error="", updated_at="2026-11-21 00:00:01",
                    )
                    persist_online_install(job)
                    ONLINE_INSTALL_RUNNING.pop(install_id, None)
                if data.get("remove_online_target"):
                    (base_dir / "scripts" / "demo.py").unlink(missing_ok=True)
                if data.get("remove_release"):
                    release.unlink(missing_ok=True)
                if data.get("fill_capacity"):
                    for index in range(99):
                        install_id = f"browser-capacity-{index:03d}"
                        persist_install_job({
                            "id": install_id, "request_id": install_id, "kind": "dependency",
                            "package": "demo", "log_file": str(base_dir / "log" / f"{install_id}.log"),
                            "running": False, "status": "succeeded", "returncode": 0,
                            "error": "", "updated_at": "2026-11-18 22:00:00",
                        })
                    latest_id = "browser-capacity-latest"
                    latest_log = base_dir / "log" / f"deps-install-demo-{latest_id}.log"
                    latest_log.write_text("latest retained failure\n", encoding="utf-8")
                    persist_install_job({
                        "id": latest_id, "request_id": latest_id, "kind": "dependency",
                        "package": "demo", "log_file": str(latest_log), "running": False,
                        "status": "failed", "returncode": 7, "error": "latest install failed",
                        "updated_at": "2026-11-18 23:00:00",
                    })
                if data.get("reset"):
                    release.unlink(missing_ok=True)
                    (base_dir / "scripts" / "demo.py").unlink(missing_ok=True)
                    log_file = base_dir / "log" / "deps-install-demo-browser-deps.log"
                    fp = log_file.open("wb", buffering=0)
                    proc = subprocess.Popen([sys.executable, "-u", "-c", command], stdout=fp, stderr=subprocess.STDOUT)
                    dependency_processes.append(proc)
                    DEPS_RUNNING["browser-deps"] = {
                        "kind": "dependency", "package": "demo", "process": proc,
                        "log_file": str(log_file), "log_fp": fp, "finished": False,
                        "returncode": None,
                    }
                if data.get("reset_cancel"):
                    cancel_id = str(data.get("cancel_id") or "browser-cancel")
                    log_file = base_dir / "log" / f"deps-install-demo-{cancel_id}.log"
                    fp = log_file.open("wb", buffering=0)
                    proc = subprocess.Popen(
                        [sys.executable, "-u", "-c", "import time; print('live cancel output', flush=True); time.sleep(120)"],
                        stdout=fp, stderr=subprocess.STDOUT,
                    )
                    dependency_processes.append(proc)
                    cancel_process = CancelProcess(proc, data.get("fail_first_terminate", False))
                    info = {
                        "id": cancel_id, "request_id": cancel_id,
                        "kind": "dependency", "package": "demo",
                        "log_file": str(log_file), "log_fp": fp, "process": cancel_process,
                        "running": True, "finished": False, "status": "running",
                        "returncode": None, "error": "", "start_time": 1,
                    }
                    DEPS_RUNNING[cancel_id] = info
                    persist_install_job(info)
                if data.get("seed_terminal_dependency_receipt"):
                    install_id = "terminal-receipt-" + str(data.get("width") or 390)
                    log_file = base_dir / "log" / f"deps-install-demo-{install_id}.log"
                    log_file.parent.mkdir(parents=True, exist_ok=True)
                    log_file.write_text("terminal receipt fixture output\n", encoding="utf-8")
                    info = {
                        "id": install_id, "request_id": install_id,
                        "kind": "dependency", "package": "demo",
                        "log_file": str(log_file), "log_fp": None,
                        "process": type("ExitedProcess", (), {
                            "poll": lambda _self: dependency_receipt_process.update(
                                polls=dependency_receipt_process["polls"] + 1
                            ) or 0,
                        })(),
                        "running": True, "finished": False, "status": "running",
                        "returncode": None, "error": "", "start_time": 1,
                    }
                    DEPS_RUNNING[install_id] = info
                    persist_install_job(info)
                    dependency_receipt_process["polls"] = 0
                    dependency_receipt_fault.update(
                        install_id=install_id, remaining=2, status="",
                        error="browser terminal install receipt unavailable",
                    )
                if data.get("start_running_dependency_receipt"):
                    install_id = "running-receipt-" + str(data.get("width") or 390)
                    INSTALL_JOBS_FILE.write_text("{}", encoding="utf-8")
                    dependency_start_count["calls"] = 0
                    dependency_receipt_fault.update(
                        install_id=install_id, remaining=1, status="running",
                        error="browser running install receipt unavailable",
                    )

                    class RunningInstallProcess:
                        def poll(self):
                            return None

                        def terminate(self):
                            return None

                        def wait(self, timeout=None):
                            return -15

                        def kill(self):
                            return None

                    def start_fixture_process(*_args, **kwargs):
                        dependency_start_count["calls"] += 1
                        kwargs["stdout"].write(b"browser running install fixture output\n")
                        kwargs["stdout"].flush()
                        return RunningInstallProcess()

                    deps_routes.subprocess = SimpleNamespace(
                        Popen=start_fixture_process, STDOUT=subprocess.STDOUT,
                        PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired,
                        run=subprocess.run,
                    )
                if "release" in data:
                    release.write_text(str(data["release"]), encoding="ascii")
            task_imports = {}
            for task in load_tasks():
                task_script_id = str(task.get("online_script_id") or "")
                if task_script_id.startswith("browser-task-recovery-"):
                    task_imports[task_script_id] = task_imports.get(task_script_id, 0) + 1
            return jsonify(
                downloads=len(downloads),
                task_reload_count=task_reload_state["calls"],
                task_imports=task_imports,
                tasks_fingerprint=hashlib.sha256(TASK_FILE.read_bytes()).hexdigest() if TASK_FILE.exists() else "",
                online=[{"id": job_id, "running": info.get("running")} for job_id, info in ONLINE_INSTALL_RUNNING.items()],
                restart_receipt_status=(
                    _load_persisted_online_install(restart_receipt_fault["install_id"]).get("status")
                    if restart_receipt_fault["install_id"] else ""
                ),
                restart_receipt_error=(
                    _load_persisted_online_install(restart_receipt_fault["install_id"]).get("error")
                    if restart_receipt_fault["install_id"] else ""
                ),
                dependency_receipt_status=(
                    json.loads(INSTALL_JOBS_FILE.read_text(encoding="utf-8"))
                    .get(dependency_receipt_fault["install_id"], {}).get("status")
                    if dependency_receipt_fault["install_id"] else ""
                ),
                dependency_receipt_failures=dependency_receipt_fault["remaining"],
                dependency_process_polls=dependency_receipt_process["polls"],
                dependency_start_count=dependency_start_count["calls"],
                deps_running=bool(DEPS_RUNNING.get("browser-deps", {}).get("process") and
                                  DEPS_RUNNING["browser-deps"]["process"].poll() is None),
            )

        server = make_server("127.0.0.1", 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            env = dict(os.environ, FLS_UI_BASE=f"http://127.0.0.1:{server.server_port}",
                       FLS_UI_TOKEN=TOKEN, FLS_PLAYWRIGHT_CORE=str(playwright), FLS_CHROMIUM=str(chromium))
            subprocess.run(["node", "-e", SCRIPT], env=env, check=True, timeout=240)
        finally:
            release.write_text("0", encoding="ascii")
            for malformed_id in ("k" * 32, "l" * 32):
                ONLINE_INSTALL_RUNNING.pop(malformed_id, None)
            for proc in dependency_processes:
                if proc.poll() is None:
                    proc.terminate()
                proc.wait(timeout=5)
            for job_id in list(ONLINE_INSTALL_RUNNING):
                install.request_stop_online_install(job_id)
            for info in DEPS_RUNNING.values():
                if info.get("log_fp"):
                    info["log_fp"].close()
            server.shutdown()
            server.server_close()
            scheduler.scheduler.shutdown(wait=False)


SCRIPT = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.FLS_PLAYWRIGHT_CORE);
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.FLS_CHROMIUM,args:['--no-sandbox']});
 try{
  for(const viewport of [{width:390,height:844},{width:1280,height:800}]){
   const context=await browser.newContext({viewport}),page=await context.newPage();
   const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('dialog',d=>d.accept());
   const base=process.env.FLS_UI_BASE,headers={'X-Token':process.env.FLS_UI_TOKEN};
   const fixture=async data=>{const r=await page.request.post(base+'/api/fixture/install-logs',{headers,data});assert.equal(r.status(),200);return r.json();};
   const state=async()=>(await page.request.get(base+'/api/fixture/install-logs',{headers})).json();
   const login=async()=>{
    if(new URL(page.url()).pathname==='/login'){
     await page.locator('[name="token"]').fill(process.env.FLS_UI_TOKEN);
     await page.locator('button[type="submit"]').click();
     await page.waitForLoadState('domcontentloaded');
    }
   };
   if(process.env.FLS_INSTALL_LOG_ONLY==='restart-receipt'){
    await page.goto(base+'/login');await login();
    await fixture({seed_restart_receipt_recovery:true,width:viewport.width});
    const installId=(viewport.width===390?'e':'f').repeat(32);
    const seeded=await state();assert.equal(seeded.restart_receipt_status,'运行中');
    await page.goto(base+'/online-scripts/log/'+installId);
    await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='结果未知'
      &&document.querySelector('#installError').textContent.includes('面板重启后无法确认'));
    await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('installer started before panel restart'));
    const diagnosis=await page.locator('#installError').textContent();
    assert.equal(diagnosis.includes('保存恢复状态失败'),false);
    assert.equal(await page.locator('#installRecoveryAction').getByRole('button',{name:'重试安装',exact:true}).count(),1);
    const saved=await state();
    assert.equal(saved.restart_receipt_status,'结果未知');
    assert.match(saved.restart_receipt_error,/面板重启后无法确认/);
    assert.equal(saved.restart_receipt_error.includes('保存恢复状态失败'),false);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
    assert.ok(await page.evaluate(()=>window.scrollY<=1),
     'initial log rendering scrolled the page away from its header');
    await page.screenshot({path:'/tmp/fls-install-restart-receipt-'+viewport.width+'.png',fullPage:true});
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='结果未知'
      &&document.querySelector('#installError').textContent.includes('面板重启后无法确认'));
    assert.equal((await state()).restart_receipt_status,'结果未知');

    await fixture({seed_restart_receipt_recovery:true,width:viewport.width,persist_mode:'before',persist_failures:2});
    const blockedId=(viewport.width===390?'g':'h').repeat(32);
    const beforeBlocked=await state();
    await page.goto(base+'/online-scripts/log/'+blockedId);
    await page.waitForFunction(()=>document.querySelector('#installPollError').textContent.includes('已禁用重试'));
    assert.equal(await page.locator('#installRecoveryAction button').count(),0);
    assert.equal(await page.locator('#installStateQuery').isVisible(),true);
    assert.equal(await page.locator('#log').textContent().then(text=>text.includes('installer started before panel restart')),true);
    await page.route('**/api/online-scripts/log/'+blockedId+'?state_only=1',route=>route.fulfill({
     status:503,contentType:'text/plain',body:'temporary install state outage'}),{times:1});
    await page.locator('#installStateQuery').click();
    await page.waitForFunction(()=>document.querySelector('#installPollError').textContent.includes('temporary install state outage'));
    assert.equal(await page.locator('#installRecoveryAction button').count(),0);
    assert.equal(await page.locator('#installStateQuery').isVisible(),true);
    assert.equal((await state()).restart_receipt_status,'运行中');
    await page.locator('#installStateQuery').click();
    await page.waitForFunction(()=>document.querySelector('#installPollError').textContent.includes('原安装状态已确认'));
    await page.locator('#installRecoveryAction').getByRole('button',{name:'重试安装',exact:true}).waitFor();
    const recoveredState=await state();
    assert.equal(recoveredState.restart_receipt_status,'结果未知');
    assert.equal(recoveredState.restart_receipt_error.includes('保存恢复状态失败'),false);
    assert.equal(recoveredState.downloads,beforeBlocked.downloads);
    assert.equal(await page.locator('#log').textContent().then(text=>text.includes('installer started before panel restart')),true);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
    assert.deepEqual(errors,[]);await context.close();console.log('restart install receipt recovery passed '+viewport.width);
    continue;
   }
   if(process.env.FLS_INSTALL_LOG_ONLY==='running-deps-receipt'){
    await page.goto(base+'/login');await login();
    await fixture({start_running_dependency_receipt:true,width:viewport.width});
    const installId='running-receipt-'+viewport.width;
    const submit=()=>page.request.post(base+'/api/deps/install',{headers,data:{name:'demo',request_id:installId}});
    let response=await submit();assert.equal(response.status(),503);
    let payload=await response.json();assert.equal(payload.install_id,installId);
    assert.equal(payload.status,'running');assert.equal(payload.running,true);
    assert.equal(payload.receipt_pending,true);
    let before=await state();assert.equal(before.dependency_start_count,1);
    assert.equal(before.dependency_receipt_status,'prepared');

    response=await submit();assert.equal(response.status(),503);
    payload=await response.json();assert.equal(payload.install_id,installId);
    assert.equal(payload.running,true);assert.equal(payload.receipt_pending,true);
    assert.equal((await state()).dependency_start_count,1);

    await page.goto(base+'/deps/install-log/'+installId+'?back=%2Fdeps');
    await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('browser running install fixture output'));
    await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='安装中'
      &&document.querySelector('#installError').hidden);
    let recovered=await state();assert.equal(recovered.dependency_receipt_status,'running');
    assert.equal(recovered.dependency_receipt_failures,0);
    assert.equal(recovered.dependency_start_count,1);
    assert.equal(await page.locator('.install-retry-form').count(),0);
    assert.equal(await page.locator('.install-cancel-form').count(),1);

    response=await submit();assert.equal(response.status(),200);
    payload=await response.json();assert.equal(payload.install_id,installId);
    assert.equal(payload.status,'running');assert.equal(payload.running,true);
    assert.equal((await state()).dependency_start_count,1);
    await page.reload();
    await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='安装中');
    assert.equal((await state()).dependency_start_count,1);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
    await page.screenshot({path:'/tmp/fls-running-deps-receipt-'+viewport.width+'.png',fullPage:true});
    assert.deepEqual(errors,[]);await context.close();console.log('running dependency receipt recovery passed '+viewport.width);
    continue;
   }
   if(process.env.FLS_INSTALL_LOG_ONLY==='terminal-deps-receipt'){
    await page.goto(base+'/login');await login();
    await fixture({seed_terminal_dependency_receipt:true,width:viewport.width});
    const installId='terminal-receipt-'+viewport.width;
    await page.goto(base+'/deps/install-log/'+installId+'?back=%2Fdeps');
    await page.waitForFunction(()=>document.querySelector('#installPollError').textContent.includes('browser terminal install receipt unavailable'));
    assert.equal(await page.locator('.install-retry-form').count(),0);
    assert.equal(await page.locator('#installPollRetry').isVisible(),true);
    assert.match(await page.locator('#installError').textContent(),/browser terminal install receipt unavailable/);
    await page.locator('#installPollRetry').click();
    await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='安装成功'
      &&document.querySelector('#installPollError').hidden);
    assert.equal(await page.locator('#log').textContent().then(text=>text.includes('terminal receipt fixture output')),true);
    assert.equal(await page.locator('.install-retry-form').count(),0);
    const recovered=await state();
    assert.equal(recovered.dependency_receipt_status,'succeeded');
    assert.equal(recovered.dependency_receipt_failures,0);
    assert.ok(recovered.dependency_process_polls>=1);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
    await page.screenshot({path:'/tmp/fls-terminal-deps-receipt-'+viewport.width+'.png',fullPage:true});
    assert.deepEqual(errors,[]);await context.close();console.log('terminal dependency receipt recovery passed '+viewport.width);
    continue;
   }
   if(process.env.FLS_INSTALL_LOG_ONLY==='malformed-recovery'){
    await page.goto(base+'/login');await login();
    await fixture({seed_malformed_task_recovery:true,width:viewport.width});
    const persistedId=(viewport.width===390?'i':'j').repeat(32);
    let response=await page.goto(base+'/online-scripts/log/'+persistedId);
    assert.equal(response.status(),500);
    await page.getByRole('heading',{name:'在线脚本安装记录无法读取',exact:true}).waitFor();
    assert.ok((await page.locator('body').innerText()).includes('任务恢复回执必须是对象'));
    assert.equal(await page.getByRole('link',{name:'重新读取安装记录'}).getAttribute('href'),
      '/online-scripts/log/'+persistedId);
    let state=await page.request.get(base+'/api/online-scripts/log/'+persistedId+'?state_only=1',{headers});
    assert.equal(state.status(),500);
    let payload=await state.json();assert.equal(payload.install_id,persistedId);
    assert.equal(payload.state_url,'/api/online-scripts/log/'+persistedId+'?state_only=1');
    assert.ok(payload.error.includes('任务恢复回执'));

    await fixture({seed_malformed_task_recovery_memory:true,width:viewport.width});
    const liveId=(viewport.width===390?'k':'l').repeat(32);
    response=await page.goto(base+'/online-scripts/log/'+liveId);
    assert.equal(response.status(),500);
    await page.getByRole('heading',{name:'在线脚本安装记录无法读取',exact:true}).waitFor();
    assert.ok((await page.locator('body').innerText()).includes('任务恢复回执必须是对象'));
    state=await page.request.get(base+'/api/online-scripts/log/'+liveId+'?state_only=1',{headers});
    assert.equal(state.status(),500);
    payload=await state.json();assert.equal(payload.install_id,liveId);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
    assert.deepEqual(errors,[]);
    await page.screenshot({path:'/tmp/fls-install-malformed-recovery-'+viewport.width+'.png',fullPage:true});
    await fixture({clear_malformed_task_recovery_memory:true,width:viewport.width});
    await context.close();console.log('malformed install recovery receipts passed '+viewport.width);
    continue;
   }
   if(process.env.FLS_INSTALL_LOG_ONLY==='task-receipt'){
    await page.goto(base+'/login');await login();
    await fixture({seed_task_retry_recovery:true,width:viewport.width,missing_log:true,fail_task_receipt:true});
    const installId=(viewport.width===390?'a':'b').repeat(32);
    const scriptId='browser-task-recovery-'+viewport.width;
    const before=await state();
    await page.goto(base+'/online-scripts/log/'+installId);
    await page.locator('#installRecoveryAction').getByRole('button',{name:'仅恢复任务导入',exact:true}).click();
    await page.locator('#installRecoveryAction').getByRole('button',{name:'核实任务导入结果',exact:true}).waitFor();
    assert.equal(await page.locator('#installStatus').textContent(),'结果未知');
    const published=await state();
    assert.equal(published.task_imports[scriptId],1);assert.equal(published.task_reload_count,1);
    assert.equal(published.downloads,before.downloads);
    await fixture({restart_task_install:true});
    await page.reload();
    await page.locator('#installRecoveryAction').getByRole('button',{name:'核实任务导入结果',exact:true}).waitFor();
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
    await page.screenshot({path:'/tmp/fls-install-task-confirm-'+viewport.width+'.png',fullPage:true});
    let confirmPosts=0;
    const confirmApi='**/api/online-scripts/install-task-confirm/'+installId;
    await page.route(confirmApi,async route=>{
     confirmPosts++;
     const committed=await route.fetch();assert.equal(committed.status(),200);
     await route.fulfill({status:502,contentType:'text/plain',body:'confirmation response lost'});
    });
    await page.locator('#installRecoveryAction').getByRole('button',{name:'核实任务导入结果',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#installPollError').textContent.includes('confirmation response lost'));
    assert.equal(await page.locator('#installRecoveryAction button').isDisabled(),true);
    await page.route('**/api/online-scripts/log/'+installId+'?state_only=1',route=>route.fulfill({
     status:503,contentType:'text/plain',body:'confirmation state unavailable'}),{times:1});
    await page.locator('#installStateQuery').click();
    await page.waitForFunction(()=>document.querySelector('#installPollError').textContent.includes('confirmation state unavailable'));
    assert.equal(await page.locator('#installRecoveryAction button').isDisabled(),true);
    await page.locator('#installStateQuery').click();
    await page.locator('#installRecoveryAction').getByRole('button',{name:'重新加载调度器',exact:true}).waitFor();
    assert.equal(confirmPosts,1);
    assert.equal(await page.locator('#installStatus').textContent(),'部分完成');
    const csrf=await page.evaluate(()=>flsGetCsrfToken());
    const denied=await page.request.post(base+'/api/online-scripts/install-reload/'+installId,{headers:{'X-CSRF-Token':'wrong'}});
    assert.equal(denied.status(),400);
    const pending=await state();assert.equal(pending.task_reload_count,1);
    assert.equal(pending.tasks_fingerprint,published.tasks_fingerprint);
    await Promise.all([page.waitForNavigation(),page.locator('#installRecoveryAction form').evaluate(form=>form.submit())]);
    await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='已完成');
    const completed=await state();
    assert.equal(completed.task_reload_count,2);assert.equal(completed.task_imports[scriptId],1);
    assert.equal(completed.tasks_fingerprint,published.tasks_fingerprint);assert.equal(completed.downloads,before.downloads);
    const repeated=await page.request.post(base+'/api/online-scripts/install-task-confirm/'+installId,{headers:{'X-CSRF-Token':csrf}});
    assert.equal(repeated.status(),200);assert.equal((await repeated.json()).replayed,true);
    await page.goto(base+'/online-scripts/install-history?script_id='+scriptId);
    await page.locator('tbody tr').first().getByRole('link',{name:'查看安装日志'}).click();
    await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='已完成');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
    await page.screenshot({path:'/tmp/fls-install-task-confirm-completed-'+viewport.width+'.png',fullPage:true});
    assert.deepEqual(errors,[]);await context.close();console.log('task receipt recovery passed '+viewport.width);
    continue;
   }
   if(process.env.FLS_INSTALL_LOG_ONLY==='scheduler-receipt'){
    await page.goto(base+'/login');await login();
    await fixture({seed_task_retry_recovery:true,width:viewport.width});
    const installId=(viewport.width===390?'a':'b').repeat(32);
    const scriptId='browser-task-recovery-'+viewport.width;
    const before=await state();
    await page.goto(base+'/online-scripts/log/'+installId);
    await page.locator('#installRecoveryAction').getByRole('button',{name:'仅恢复任务导入',exact:true}).click();
    await page.locator('#installRecoveryAction').getByRole('button',{name:'重新加载调度器',exact:true}).waitFor();
    const pending=await state();
    assert.equal(pending.task_imports[scriptId],1);
    assert.equal(pending.task_reload_count,1);
    await fixture({fail_scheduler_receipt_after_commit:true,width:viewport.width});
    let reloadPosts=0;
    await page.route('**/api/online-scripts/install-reload/'+installId,async route=>{
     reloadPosts++;
     const committed=await route.fetch();
     assert.equal(committed.status(),200);
     assert.equal((await committed.json()).receipt_confirmed,true);
     await route.fulfill({status:502,contentType:'text/plain',body:'scheduler response lost'});
    });
    await page.locator('#installRecoveryAction').getByRole('button',{name:'重新加载调度器',exact:true}).click();
    await page.waitForFunction(()=>document.querySelector('#installPollError').textContent.includes('scheduler response lost'));
    assert.equal(await page.locator('#installRecoveryAction button').isDisabled(),true);
    await page.locator('#installStateQuery').click();
    await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='已完成'
      &&document.querySelector('#installPollError').textContent.includes('原安装状态已确认'));
    const completed=await state();
    assert.equal(reloadPosts,1);
    assert.equal(completed.task_reload_count,2);
    assert.equal(completed.task_imports[scriptId],1);
    assert.equal(completed.downloads,before.downloads);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
    await page.screenshot({path:'/tmp/fls-install-scheduler-confirm-'+viewport.width+'.png',fullPage:true});
    assert.deepEqual(errors,[]);await context.close();console.log('scheduler receipt recovery passed '+viewport.width);
    continue;
   }
   if(process.env.FLS_INSTALL_LOG_ONLY!=='online-stop'){
   await fixture({seed_retry_chain:true});await fixture({reset:true});
   await page.goto(base+'/login');await login();
   await fixture({seed_delayed_online_log:true,width:viewport.width});
   const delayedLogId=viewport.width===390?'c'.repeat(32):'d'.repeat(32);
   await page.goto(base+'/online-scripts/log/'+delayedLogId);
   await page.waitForFunction(()=>document.querySelector('#installPollError').textContent.includes('日志尚未生成'));
   assert.equal(await page.locator('#installStatus').textContent(),'运行中');
   assert.equal(await page.locator('#installStopAction').isVisible(),true);
   assert.equal(await page.locator('#installPollLogs').isVisible(),false);
   assert.equal(await page.locator('#installPollRetry').isVisible(),true);
   assert.ok(await page.evaluate(()=>window.__FLS_ACTIVE_LOG_INTERVAL__));
   await fixture({publish_delayed_online_log:true,width:viewport.width});
   await page.waitForFunction(()=>document.querySelector('#installPollError').hidden
     &&document.querySelector('#log').textContent.includes('installer log became available'));
   assert.equal(await page.locator('#installStopAction').isVisible(),true);
   const beforeTransientRecovery=(await state()).downloads;
   const preservedOnlineLog=await page.locator('#log').textContent();
   const delayedLogApi='**/api/online-scripts/log/'+delayedLogId+'?lines=1600';
   for(const failure of [503,'network',429]){
    await page.route(delayedLogApi,route=>failure==='network'?route.abort('failed'):
      route.fulfill({status:failure,contentType:'text/plain',body:'transient install log outage'}),{times:1});
    await page.waitForFunction(()=>!document.querySelector('#installPollError').hidden
      &&document.querySelector('#installPollError').textContent.includes('自动重试'));
    assert.equal(await page.locator('#log').textContent(),preservedOnlineLog);
    assert.equal(await page.locator('#installStopAction').isVisible(),true);
    await page.waitForFunction(()=>document.querySelector('#installPollError').hidden);
    assert.equal(await page.locator('#log').textContent(),preservedOnlineLog);
   }
   assert.equal((await state()).downloads,beforeTransientRecovery);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
   await page.screenshot({path:'/tmp/fls-online-install-log-transient-'+viewport.width+'.png',fullPage:true});
   await fixture({complete_delayed_online_log:true,width:viewport.width});
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='已完成');
   assert.equal(await page.evaluate(()=>window.__FLS_ACTIVE_LOG_INTERVAL__),null);
   assert.equal(await page.locator('#installStopAction').isVisible(),false);
   await page.goto(base+'/deps/install-history?kind=dependency');
   const retryParent=page.locator('tr').filter({hasText:'browser-retry-parent'});
   assert.equal((await retryParent.locator('td').nth(7).textContent()).trim(),'1');
   const retryLink=retryParent.getByRole('link',{name:/后续重试.*browser-retry-child/});
   await retryLink.waitFor();
   assert.match(await retryLink.getAttribute('href'),/browser-retry-child\?back=\/deps\/install-history$/);
   await retryLink.click();
   await page.waitForURL('**/deps/install-log/browser-retry-child?back=/deps/install-history');
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='安装失败');
   assert.ok((await page.locator('#log').textContent()).includes('retry attempt failed'));
   assert.ok(await page.locator('.install-retry-form [name="request_id"]').inputValue());
   await page.goto(base+'/deps/install-history?kind=dependency');
   const retryChild=page.locator('tr').filter({hasText:'browser-retry-child'});
   const parentLink=retryChild.getByRole('link',{name:/原安装.*browser-retry-parent/});
   await parentLink.click();
   await page.waitForURL('**/deps/install-log/browser-retry-parent?back=/deps/install-history');
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='安装失败');
   assert.equal(await page.locator('.install-retry-form').count(),0);
   await page.getByRole('link',{name:'查看已提交重试'}).click();
   await page.waitForURL('**/deps/install-log/browser-retry-child?back=/deps/install-history');
   await fixture({fill_capacity:true});
   await page.goto(base+'/deps/install-history?kind=dependency&limit=100');
   assert.equal(await page.locator('tr').filter({hasText:'browser-retry-parent'}).count(),0);
   assert.equal(await page.locator('tr').filter({hasText:'browser-retry-child'}).count(),0);
   const latest=page.locator('tr').filter({hasText:'browser-capacity-latest'});
   assert.equal((await latest.locator('td').nth(3).textContent()).trim(),'安装失败');
   assert.equal((await latest.locator('td').nth(7).textContent()).trim(),'7');
   await latest.getByRole('link',{name:'查看日志与恢复',exact:true}).click();
   await page.waitForURL('**/deps/install-log/browser-capacity-latest?back=/deps');
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='安装失败');
   assert.ok((await page.locator('#log').textContent()).includes('latest retained failure'));
   assert.ok(await page.locator('.install-retry-form [name="request_id"]').inputValue());
   const before=(await state()).downloads;
   const csrf=await page.evaluate(()=>flsGetCsrfToken());
   const installRequestId=require('node:crypto').randomBytes(16).toString('hex');
   const installForm={request_id:installRequestId};
   const start=await page.request.post(base+'/online-scripts/install/browser-recovery',{
     headers:{'X-CSRF-Token':csrf,'X-Requested-With':'XMLHttpRequest'},form:installForm,maxRedirects:0});
   assert.equal(start.status(),302);const onlinePath=start.headers().location;
   const replay=await page.request.post(base+'/online-scripts/install/browser-recovery',{
     headers:{'X-CSRF-Token':csrf,'X-Requested-With':'XMLHttpRequest'},form:installForm,maxRedirects:0});
   assert.equal(replay.status(),302);assert.equal(replay.headers().location,onlinePath);
   const paths=[onlinePath,'/deps/install-log/browser-deps?back=%2Fonline-scripts'];
   for(const path of paths){
    await page.goto(base+path);
    await page.waitForFunction(()=>document.querySelector('#log').textContent.split('\n').includes('live installer output'));
    const original=await page.locator('#log').textContent(),status=await page.locator('#installStatus').textContent();
    for(const code of [502,200]){
     await fixture({status:code});
     const diagnostic=code===502?'fixture upstream unavailable':'响应解析失败';
     await page.waitForFunction(text=>document.querySelector('#installPollError').textContent.includes(text),diagnostic);
     assert.equal(await page.locator('#log').textContent(),original);
     assert.equal(await page.locator('#installStatus').textContent(),status);
     assert.equal((await state()).deps_running,true);
     await fixture({});await page.locator('#installPollRetry').click();
     await page.waitForFunction(()=>document.querySelector('#installPollError').hidden);
    }
    await context.clearCookies();
    await page.waitForFunction(()=>!document.querySelector('#installPollLogin').hidden);
    assert.equal(await page.evaluate(()=>window.__FLS_ACTIVE_LOG_INTERVAL__),null);
    assert.equal(await page.locator('#log').textContent(),original);
    await page.locator('#installPollLogin').click();await page.waitForLoadState('domcontentloaded');await login();
    assert.equal(new URL(page.url()).pathname,new URL(base+path).pathname);
    await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('live installer output'));
    assert.equal(await page.locator('#installPollLogin').isVisible(),false);
    const apiPattern=path.startsWith('/online-scripts/')?'**/api/online-scripts/log/*':'**/api/deps/install-log/*';
    await page.route(apiPattern,async route=>{
     const response=await route.fetch({url:route.request().url()+'&token=forbidden'});
     assert.equal(response.status(),403);await route.fulfill({response});
    },{times:1});
    await page.waitForFunction(()=>!document.querySelector('#installPollLogin').hidden);
    assert.equal(await page.evaluate(()=>window.__FLS_ACTIVE_LOG_INTERVAL__),null);
    if(path.startsWith('/online-scripts/')){
     await page.locator('#installPollRetry').click();
     await page.waitForFunction(()=>document.querySelector('#installPollError').hidden);
     assert.equal(await page.locator('#installStopAction').isVisible(),true);
     assert.ok(await page.evaluate(()=>window.__FLS_ACTIVE_LOG_INTERVAL__));
    }else{
     await fixture({});
     await page.reload();
     assert.equal(new URL(page.url()).pathname,new URL(base+path).pathname);
    }
    await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('live installer output'));
    await page.evaluate(path=>{const a=document.createElement('a');a.id='fixture-revisit';a.href=path;a.textContent='revisit';document.querySelector('.content').append(a);},path);
    await page.locator('#fixture-revisit').click();
    await page.waitForFunction(()=>!document.querySelector('#fixture-revisit'));
    await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('live installer output'));
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
   }
   assert.equal((await state()).downloads,before+1);
   await fixture({release:17});
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='安装失败');
   await page.goto(base+onlinePath);
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='失败');
   await page.locator('#installRecoveryAction').getByRole('button',{name:'重试安装',exact:true}).waitFor();
   assert.ok(await page.locator('#installRecoveryAction [name="csrf_token"]').inputValue());
   assert.equal(await page.evaluate(()=>window.__FLS_ACTIVE_LOG_INTERVAL__),null);
   await page.screenshot({path:'/tmp/fls-install-log-recovery-'+viewport.width+'.png',fullPage:true});
   const failed=await state();assert.equal(failed.deps_running,false);assert.equal(failed.online.some(j=>j.running),false);
   await page.locator('#installRecoveryAction').getByRole('button',{name:'重试安装',exact:true}).click();
   await page.getByRole('button',{name:'确认覆盖并继续',exact:true}).waitFor();
   await fixture({release:0});
   await page.getByRole('button',{name:'确认覆盖并继续',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('#installStatus')?.textContent==='已完成');
   assert.equal((await state()).downloads,before+2);
   assert.equal(await page.evaluate(()=>window.__FLS_ACTIVE_LOG_INTERVAL__),null);
   const cancelId='browser-cancel-'+viewport.width;
   await fixture({reset_cancel:true,cancel_id:cancelId});
   await page.goto(base+'/deps/install-log/'+cancelId+'?back=%2Fdeps');
   await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('live cancel output'));
   const cancelForm=page.locator('.install-cancel-form');
   await cancelForm.getByRole('button',{name:'取消安装'}).waitFor();
   const cancelRequestId=await cancelForm.locator('[name="request_id"]').inputValue();
   let cancelPosts=0;
   await page.route('**/api/deps/install-cancel/'+cancelId,async route=>{
    if(route.request().method()==='POST'){
     cancelPosts++;
     const committed=await route.fetch();
     assert.equal(committed.status(),200);
     await route.fulfill({status:502,contentType:'text/plain',body:'fixture response lost after commit'});
    }else await route.continue();
   });
   await cancelForm.getByRole('button',{name:'取消安装'}).click();
   await page.waitForFunction(()=>document.querySelector('#installCancelError').textContent.includes('fixture response lost after commit'));
   assert.equal(cancelPosts,1);
   await page.reload();
   assert.equal(await page.locator('.install-cancel-form').getAttribute('data-query-on-load'),'true');
   assert.equal(await page.locator('.install-cancel-form [name="request_id"]').inputValue(),cancelRequestId);
   await page.waitForFunction(()=>document.querySelector('#installCancelError').textContent.includes('已取消'));
   assert.equal(await page.locator('.install-cancel-form button[type="submit"]').isHidden(),true);
   assert.equal(await page.locator('#installCancelAgain').isHidden(),true);
   assert.equal(cancelPosts,1);
   assert.equal((await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)),true);
   await page.screenshot({path:'/tmp/fls-install-cancel-recovery-'+viewport.width+'.png',fullPage:true});
   const failedCancelId='browser-cancel-failed-'+viewport.width;
   await fixture({reset_cancel:true,cancel_id:failedCancelId,fail_first_terminate:true});
   await page.goto(base+'/deps/install-log/'+failedCancelId+'?back=%2Fdeps');
   await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('live cancel output'));
   let failedCancelPosts=0;
   await page.route('**/api/deps/install-cancel/'+failedCancelId,async route=>{
    if(route.request().method()==='POST') failedCancelPosts++;
    await route.continue();
   });
   const failedCancelForm=page.locator('.install-cancel-form');
   await failedCancelForm.getByRole('button',{name:'取消安装'}).click();
   await page.waitForFunction(()=>document.querySelector('#installCancelError').textContent.includes('fixture terminate denied'));
   assert.equal(failedCancelPosts,1);
   await page.locator('#installCancelQuery').click();
   await page.waitForFunction(()=>!document.querySelector('#installCancelAgain').hidden);
   const failedRequestId=await failedCancelForm.locator('[name="request_id"]').inputValue();
   await page.locator('#installCancelAgain').click();
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='已取消');
   assert.notEqual(await failedCancelForm.locator('[name="request_id"]').inputValue(),failedRequestId);
   assert.equal(failedCancelPosts,2);
   assert.equal(await page.locator('#installCancelAgain').isHidden(),true);
   await page.goto(base+'/deps/install-history?kind=dependency');
   const historyRow=page.locator('tr').filter({hasText:failedCancelId});
   await historyRow.waitFor();
   assert.equal((await historyRow.locator('td').nth(3).textContent()).trim(),'已取消');
   assert.equal((await historyRow.locator('td').nth(4).textContent()).trim(),'已取消');
   const historyRecovery=historyRow.getByRole('link',{name:'查看日志与恢复',exact:true});
   assert.match(await historyRecovery.getAttribute('href'),new RegExp('/deps/install-log/'+failedCancelId+'\\?back=/deps$'));
   await historyRecovery.click();
   await page.waitForURL('**/deps/install-log/'+failedCancelId+'?back=/deps');
   await page.locator('.install-retry-form button[type="submit"]').waitFor();
   assert.ok(await page.locator('.install-retry-form [name="request_id"]').inputValue());
   assert.equal((await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)),true);

   await fixture({seed_online_history:true});
   await page.goto(base+'/online-scripts');
   await page.locator('form[action="/online-scripts/install-retry/online-latest-final"]').waitFor();
   assert.ok((await page.locator('.content').textContent()).includes('上次安装状态：失败'));
   const onlineDownloadsBefore=(await state()).downloads;
   const capacityRequestId=require('node:crypto').randomBytes(16).toString('hex');
   const onlineCsrf=await page.evaluate(()=>flsGetCsrfToken());
   const capacityStart=await page.request.post(base+'/api/online-scripts/install/browser-recovery',{
    headers:{'X-CSRF-Token':onlineCsrf},data:{request_id:capacityRequestId}});
   assert.equal(capacityStart.status(),200,await capacityStart.text());
   const capacityResult=await capacityStart.json();
   assert.equal(capacityResult.ok,true);
   await page.waitForFunction(async id=>{
    const response=await fetch('/api/online-scripts/log/'+id,{cache:'no-store'});
    if(!response.ok)return false;const job=await response.json();return job.running===false;
   },capacityResult.install_id);
   const onlineHistoryResponse=await page.request.get(base+'/api/online-scripts/install-history?limit=100');
   assert.equal(onlineHistoryResponse.status(),200);
   const onlineHistory=await onlineHistoryResponse.json();
   const onlineIds=onlineHistory.items.map(item=>item.id);
   assert.equal(onlineIds.includes('online-old-parent'),false);
   assert.equal(onlineIds.includes('online-old-child'),false);
   for(const id of ['online-latest-parent','online-latest-middle','online-latest-final',capacityResult.install_id])
    assert.ok(onlineIds.includes(id),id+' should remain in the trimmed history');
   const onlineById=Object.fromEntries(onlineHistory.items.map(item=>[item.id,item]));
   assert.equal(onlineById['online-latest-middle'].retry_parent_id,'online-latest-parent');
   assert.equal(onlineById['online-latest-middle'].retry_child_id,'online-latest-final');
   assert.equal(onlineById['online-latest-final'].retry_relation_error,'');

   await page.goto(base+'/online-scripts/install-history');
   await page.locator('#install-history-script').fill('browser-recovery');
   await page.locator('#install-history-status').selectOption('失败');
   await page.locator('#install-history-limit').selectOption('1');
   await page.getByRole('button',{name:'筛选历史'}).click();
   await page.waitForFunction(()=>document.body.textContent.includes('当前显示 1–1 / '));
   let historyUrl=new URL(page.url());
   assert.equal(historyUrl.searchParams.get('script_id'),'browser-recovery');
   assert.equal(historyUrl.searchParams.get('status'),'失败');
   assert.equal(historyUrl.searchParams.get('limit'),'1');
   const firstFilteredRow=page.locator('tbody tr').first();
   assert.ok((await firstFilteredRow.textContent()).includes('online-latest-final'));
   await page.getByRole('link',{name:'下一页'}).click();
   await page.waitForFunction(()=>new URL(location.href).searchParams.get('offset')==='1');
   assert.ok((await page.locator('tbody tr').first().textContent()).includes('online-latest-middle'));
   const filteredSecondPage = page.url();
   await page.reload();
   await context.clearCookies();
   await page.reload();
   await page.waitForURL(url=>url.pathname==='/login');
   await login();
   await page.waitForURL(filteredSecondPage);
   assert.equal(await page.locator('#install-history-status').inputValue(),'失败');
   await page.getByRole('link',{name:'上一页'}).click();
   await page.waitForFunction(()=>!new URL(location.href).searchParams.has('offset'));
   await page.locator('tbody tr').first().getByRole('link',{name:'查看安装日志'}).click();
   await page.waitForURL(url=>url.pathname==='/online-scripts/log/online-latest-final'&&url.searchParams.has('back'));
   let logUrl=new URL(page.url());
   assert.equal(decodeURIComponent(logUrl.searchParams.get('back')),'/online-scripts/install-history?script_id=browser-recovery&status=失败&limit=1');
   await page.getByRole('link',{name:'查看原安装'}).click();
   await page.waitForURL(url=>url.pathname==='/online-scripts/log/online-latest-middle'&&url.searchParams.has('back'));
   logUrl=new URL(page.url());
   assert.equal(decodeURIComponent(logUrl.searchParams.get('back')),'/online-scripts/install-history?script_id=browser-recovery&status=失败&limit=1');
   await page.getByRole('link',{name:'返回',exact:true}).click();
   await page.waitForURL(url=>url.pathname==='/online-scripts/install-history'&&url.searchParams.get('limit')==='1');
   historyUrl=new URL(page.url());
   assert.equal(historyUrl.searchParams.get('script_id'),'browser-recovery');
   assert.equal(historyUrl.searchParams.get('status'),'失败');
   assert.equal(historyUrl.searchParams.get('limit'),'1');

   await page.goto(base+'/online-scripts/install-history');
   const latestParentRow=page.locator('tr').filter({hasText:'online-latest-parent'});
   await latestParentRow.getByRole('link',{name:'后续重试'}).click();
   await page.waitForURL('**/online-scripts/log/online-latest-middle?back=/online-scripts/install-history');
   await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('log for online-latest-middle'));
   await page.getByRole('link',{name:'查看后续重试'}).click();
   await page.waitForURL('**/online-scripts/log/online-latest-final?back=/online-scripts/install-history');
   await page.waitForFunction(()=>document.querySelector('#log').textContent.includes('log for online-latest-final'));
   await page.getByRole('link',{name:'查看原安装'}).click();
   await page.waitForURL('**/online-scripts/log/online-latest-middle?back=/online-scripts/install-history');
   await fixture({remove_online_target:true});
   await page.goto(historyUrl.href);
   const latestFinalRow=page.locator('tr').filter({hasText:'online-latest-final'});
   await latestFinalRow.getByRole('link',{name:'查看安装日志'}).click();
   await page.waitForURL(url=>url.pathname==='/online-scripts/log/online-latest-final');
   await page.locator('#installRecoveryAction [name="csrf_token"]').waitFor({state:'attached'});
   await page.locator('#installRecoveryAction').getByRole('button',{name:'重试安装',exact:true}).click();
   await page.waitForURL(url=>/^\/online-scripts\/log\/[a-f0-9]{32}$/.test(url.pathname));
   const recoveredInstallId=new URL(page.url()).pathname.split('/').pop();
   assert.equal(decodeURIComponent(new URL(page.url()).searchParams.get('back')),
    '/online-scripts/install-history?script_id=browser-recovery&status=失败&limit=1');
   assert.notEqual(recoveredInstallId,'online-latest-final');
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='已完成');
   await page.goto(base+'/online-scripts/install-history');
   const retriedFinalRow=page.locator('tr').filter({hasText:'online-latest-final'});
   const newRetryLink=retriedFinalRow.getByRole('link',{name:'后续重试'});
   assert.match(await newRetryLink.getAttribute('href'),new RegExp('/online-scripts/log/'+recoveredInstallId+'\\?back=/online-scripts/install-history$'));
   await newRetryLink.click();
   await page.waitForURL('**/online-scripts/log/'+recoveredInstallId+'?back=/online-scripts/install-history');
   await page.getByRole('link',{name:'查看原安装'}).waitFor();
   assert.equal((await state()).downloads,onlineDownloadsBefore+2);

   const beforeTaskRecovery=await state();
   await fixture({seed_task_retry_recovery:true,width:viewport.width,missing_log:true});
   const taskRecoveryId=(viewport.width===390?'a':'b').repeat(32);
   await page.goto(base+'/online-scripts/log/'+taskRecoveryId);
   await page.locator('#installStateQuery').waitFor({state:'visible'});
   await page.locator('#installRecoveryAction').getByRole('button',{name:'仅恢复任务导入',exact:true}).waitFor();
   assert.equal(await page.locator('#installStatus').textContent(),'失败');
   assert.equal(await page.locator('#installPollError').isVisible(),true);
   await page.locator('#installStateQuery').click();
   await page.waitForFunction(()=>document.querySelector('#installPollError').textContent.includes('原安装状态已确认'));
   assert.match(await page.locator('#installPollError').textContent(),/日志/);
   assert.equal((await state()).downloads,beforeTaskRecovery.downloads);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
   await page.screenshot({path:'/tmp/fls-online-install-missing-log-'+viewport.width+'.png',fullPage:true});
   await fixture({seed_task_retry_recovery:true,width:viewport.width});
   await page.reload();
   await page.locator('#installRecoveryAction').getByRole('button',{name:'仅恢复任务导入',exact:true}).waitFor();
   await page.locator('#installRecoveryAction').getByRole('button',{name:'仅恢复任务导入',exact:true}).click();
   await page.locator('#installRecoveryAction').getByRole('button',{name:'重新加载调度器',exact:true}).waitFor();
   assert.equal(await page.locator('#installStatus').textContent(),'部分完成');
   await page.locator('#installRecoveryAction').getByRole('button',{name:'重新加载调度器',exact:true}).click();
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='已完成');
   const afterTaskRecovery=await state();
   assert.equal(afterTaskRecovery.task_imports['browser-task-recovery-'+viewport.width],1);
   assert.equal(afterTaskRecovery.task_reload_count,2);
   assert.equal(afterTaskRecovery.downloads,beforeTaskRecovery.downloads);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);
   await page.screenshot({path:'/tmp/fls-install-task-recovery-'+viewport.width+'.png',fullPage:true});

   const taskScriptId='browser-task-recovery-'+viewport.width;
   const pendingInstall={request_id:taskRecoveryId,install_id:taskRecoveryId,script_id:taskScriptId,
    action:'/online-scripts/install/'+taskScriptId};
   await page.evaluate(value=>sessionStorage.setItem('fls-online-install-request',JSON.stringify(value)),pendingInstall);
   await page.goto(base+'/online-scripts/install-select/'+taskScriptId);
   await page.locator('#onlineInstallRecoveryClear').waitFor({state:'visible'});
   assert.match(await page.locator('#onlineInstallRecoveryStatus').textContent(),/已明确结束/);
   assert.equal(await page.locator('#onlineInstallSelectForm [name="request_id"]').inputValue(),taskRecoveryId);
   assert.equal(await page.locator('#onlineInstallSelectForm button[type="submit"]').first().isDisabled(),true);
   const clearTerminal=page.locator('#onlineInstallRecoveryClear');
   await Promise.all([page.waitForNavigation(),clearTerminal.click()]);
   await page.locator('#onlineInstallSelectForm').waitFor();
   const freshRequestId=await page.locator('#onlineInstallSelectForm [name="request_id"]').inputValue();
   assert.notEqual(freshRequestId,taskRecoveryId);
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-online-install-request')),null);

   const corruptInstallState='{broken-install-state';
   await page.evaluate(value=>sessionStorage.setItem('fls-online-install-request',value),corruptInstallState);
   await page.reload();
   await page.waitForFunction(()=>document.querySelector('#onlineInstallRecoveryStatus').textContent.includes('状态损坏'));
   assert.equal(await page.locator('#onlineInstallSelectForm button[type="submit"]').first().isDisabled(),true);
   await page.route('**/api/online-scripts/install-history*',route=>route.fulfill({
    status:503,contentType:'text/html',body:'history unavailable'}),{times:1});
   await page.locator('#onlineInstallRecoveryCheckHistory').click();
   await page.waitForFunction(()=>document.querySelector('#onlineInstallRecoveryStatus').textContent.includes('未能核实'));
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-online-install-request')),corruptInstallState);
   assert.equal(await page.locator('#onlineInstallRecoveryClear').isHidden(),true);
   await page.locator('#onlineInstallRecoveryCheckHistory').click();
   await page.locator('#onlineInstallRecoveryClear').waitFor({state:'visible'});
   await Promise.all([page.waitForNavigation(),page.locator('#onlineInstallRecoveryClear').click()]);
   await page.locator('#onlineInstallSelectForm').waitFor();
   assert.equal(await page.evaluate(()=>sessionStorage.getItem('fls-online-install-request')),null);
   assert.notEqual(await page.locator('#onlineInstallSelectForm [name="request_id"]').inputValue(),freshRequestId);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true);

   await page.goto(historyUrl.href);
   assert.equal((await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)),true);
   await page.screenshot({path:'/tmp/fls-online-install-history-'+viewport.width+'.png',fullPage:true});
   }
   await page.goto(base+'/login');await login();
   await page.goto(base+'/online-scripts');
   await fixture({remove_online_target:true,remove_release:true});
   const stopRequestId=require('node:crypto').randomBytes(16).toString('hex');
   const stopCsrf=await page.evaluate(()=>flsGetCsrfToken());
   const stopStart=await page.request.post(base+'/api/online-scripts/install/browser-recovery',{
    headers:{'X-CSRF-Token':stopCsrf},data:{request_id:stopRequestId}});
   assert.equal(stopStart.status(),200,await stopStart.text());
   const stopStarted=await stopStart.json();
   assert.ok(stopStarted.install_id);
   await page.goto(base+'/online-scripts/log/'+stopStarted.install_id+'?back='+encodeURIComponent('/online-scripts/install-history?script_id=browser-recovery'));
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='运行中');
   await page.waitForFunction(()=>document.querySelector('#log').textContent.split('\n').includes('live installer output'));
   const visibleLog=await page.locator('#log').textContent();
   let stopPosts=0;
   await page.route('**/api/online-scripts/install-stop/'+stopStarted.install_id,async route=>{
    if(route.request().method()==='POST'){
     stopPosts++;
     if(stopPosts===1){
      await route.fulfill({status:503,contentType:'text/plain',body:'stop service unavailable before commit'});
      return;
     }
      const committed=await route.fetch();
      assert.equal(committed.status(),200);
      if(viewport.width===390){
      const payload=await committed.json();assert.equal(payload.ok,true);
      assert.match(payload.msg,/停止请求日志写入失败：browser stop log unavailable/);
      assert.match(payload.warning,/停止请求日志写入失败：browser stop log unavailable/);
      await route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(payload)});
     }else await route.fulfill({status:502,contentType:'text/plain',body:'stop response lost after commit'});
    }else await route.continue();
   });
   await page.locator('#installStopAction button').click();
   await page.waitForFunction(()=>!document.querySelector('#installStopQuery').hidden);
   assert.equal(stopPosts,1);
   assert.equal(await page.locator('#log').textContent(),visibleLog);
   const stopStateUrl='**/api/online-scripts/log/'+stopStarted.install_id+'?state_only=1';
   await page.route(stopStateUrl,route=>route.fulfill({status:403,contentType:'text/plain',body:'state access denied'}));
   await page.locator('#installStopQuery').click();
   await page.locator('#installPollLogin').waitFor();
   assert.equal(await page.locator('#log').textContent(),visibleLog);
   assert.equal(await page.locator('#installStopAction').isHidden(),true);
   await page.unroute(stopStateUrl);
   await page.locator('#installStopQuery').click();
   await page.waitForFunction(()=>!document.querySelector('#installStopAction').hidden &&
    !document.querySelector('#installStopAction button').disabled);
   assert.equal(await page.locator('#installStatus').textContent(),'运行中');
   assert.equal(stopPosts,1);
   await fixture({stop_log_failure:1});
   await page.locator('#installStopAction button').click();
   if(viewport.width===390){
    await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='已停止');
   }else await page.waitForFunction(()=>!document.querySelector('#installStopQuery').hidden);
   assert.equal(stopPosts,2);
   assert.ok((await page.locator('#log').textContent()).includes('live installer output'));
   if(viewport.width===390){
    assert.match(await page.locator('#installPollError').textContent(),/原安装状态已确认：已停止.*停止请求日志写入失败：browser stop log unavailable/);
    await page.screenshot({path:'/tmp/fls-online-install-stop-log-warning-'+viewport.width+'.png',fullPage:true});
    await page.goto(base+'/online-scripts/install-history?script_id=browser-recovery');
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    assert.deepEqual(errors,[]);await context.close();console.log('install stop log warning passed '+viewport.width);
    continue;
   }
   await page.locator('#installStopQuery').click();
   await page.waitForFunction(()=>document.querySelector('#installStatus').textContent==='已停止');
   assert.match(await page.locator('#installPollError').textContent(),/原安装状态已确认：已停止.*停止请求日志写入失败：browser stop log unavailable/);
   assert.ok((await page.locator('#log').textContent()).includes('live installer output'));
   const stopVisibility=await page.locator('#installStopAction').evaluate(el=>({
    hidden:el.hidden,display:getComputedStyle(el).display,html:el.outerHTML,
   }));
   assert.equal(await page.locator('#installStopAction').isHidden(),true,JSON.stringify(stopVisibility));
   await page.screenshot({path:'/tmp/fls-online-install-stop-'+viewport.width+'.png',fullPage:true});
   const overflow=await page.evaluate(()=>[...document.querySelectorAll('body *')].filter(el=>{
    const rect=el.getBoundingClientRect();return rect.width>0&&rect.right>innerWidth+1;
   }).map(el=>({tag:el.tagName,id:el.id,class:el.className,text:el.textContent.slice(0,120)})));
   assert.equal((await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1)),true,JSON.stringify(overflow));
   await page.goto(base+'/online-scripts/install-history?script_id=browser-recovery');
   assert.equal((await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)),true);
   await page.screenshot({path:'/tmp/fls-online-install-history-'+viewport.width+'.png',fullPage:true});
   assert.deepEqual(errors,[]);await context.close();console.log('install log recovery passed '+viewport.width);
  }
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
'''


if __name__ == "__main__":
    main()
