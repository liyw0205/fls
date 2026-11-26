#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import importlib.util

from fls_manager.instance_lock import acquire_instance_lock, release_instance_lock


def _bootstrap_deps():
    required = [
        ("flask", "flask"),
        ("apscheduler", "apscheduler"),
        ("requests", "requests"),
        ("socks", "PySocks"),
    ]

    missing = []

    for mod, pkg in required:
        try:
            if importlib.util.find_spec(mod) is None:
                missing.append(pkg)
        except Exception:
            missing.append(pkg)

    if not missing:
        return

    print("[FLS] 缺少依赖，尝试安装：" + " ".join(missing))

    cmds = [
        [sys.executable, "-m", "pip", "install"] + missing,
        [sys.executable, "-m", "pip", "install", "--break-system-packages"] + missing,
        [sys.executable, "-m", "pip", "install", "--user"] + missing,
    ]

    for cmd in cmds:
        try:
            r = subprocess.run(cmd, timeout=900)
            if r.returncode == 0:
                return
        except Exception:
            pass

    print("[FLS] 依赖安装失败，请手动执行：")
    print(sys.executable + " -m pip install " + " ".join(missing))
    sys.exit(1)


def main():
    try:
        instance_lock = acquire_instance_lock()
    except Exception as exc:
        print(f"[FLS] 无法获取实例锁，拒绝启动：{exc}")
        return 1

    if instance_lock is None:
        print("[FLS] 已有实例正在运行，忽略重复启动")
        return 0

    try:
        _bootstrap_deps()

        from fls_manager.app import create_app, initialize_startup_runtime
        from fls_manager.config import get_host, get_port, fls_get_admin_token
        from fls_manager.paths import BASE_DIR, DATA_DIR, LOG_DIR, SCRIPT_DIR
        from fls_manager.state import scheduler
        from fls_manager.constants import MAIN_PROCESS_NAME, TASK_PROCESS_PREFIX

        try:
            import setproctitle
            setproctitle.setproctitle(MAIN_PROCESS_NAME)
        except Exception:
            pass

        app = create_app()

        if app.config.get("RESTORE_STARTUP_RECONCILIATION_PENDING"):
            app.config["RESTORE_STARTUP_RUNTIME_PENDING"] = True
        else:
            initialize_startup_runtime(app)

        host = get_host()
        port = get_port()
        token = fls_get_admin_token()

        print("====================================================")
        print("FLS Flask Script Manager - Modular Core")
        print(f"主进程名: {MAIN_PROCESS_NAME}")
        print(f"任务进程标识前缀: {TASK_PROCESS_PREFIX}")
        print(f"工作目录: {BASE_DIR}")
        print(f"数据目录: {DATA_DIR}")
        print(f"日志目录: {LOG_DIR}")
        print(f"脚本目录: {SCRIPT_DIR}")
        print(f"访问地址: http://{host}:{port}")

        if token:
            print("Token 已配置，请使用 /login 或 API 的 X-Token 请求头访问")
        else:
            print("警告: 当前未设置登录 Token，首次访问将进入 /setup 引导设置")

        print("====================================================")

        try:
            app.run(host=host, port=port, debug=False, use_reloader=False)
        finally:
            try:
                scheduler.shutdown(wait=False)
            except Exception:
                pass
    finally:
        release_instance_lock(instance_lock)


if __name__ == "__main__":
    raise SystemExit(main())
