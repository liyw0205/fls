#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import subprocess
import importlib.util

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

_bootstrap_deps()

from fls_manager.app import create_app
from fls_manager.config import get_host, get_port, fls_get_admin_token, save_config, load_config
from fls_manager.scheduler import reload_scheduler
from fls_manager.logs import cleanup_logs
from fls_manager.paths import BASE_DIR, DATA_DIR, LOG_DIR, SCRIPT_DIR
from fls_manager.state import scheduler
from fls_manager.constants import MAIN_PROCESS_NAME, TASK_PROCESS_PREFIX

try:
    import setproctitle
    setproctitle.setproctitle(MAIN_PROCESS_NAME)
except Exception:
    pass

app = create_app()

if __name__ == "__main__":
    try:
        save_config(load_config())
    except Exception as e:
        print(f"[Config] 初始化配置失败: {e}")

    reload_scheduler()
    cleanup_logs()

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
        print(f"Token访问: http://服务器IP:{port}/?token={token}")
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
