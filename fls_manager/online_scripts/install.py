import os
import time
import signal
import shutil
import threading
import subprocess

import requests

from ..paths import SCRIPT_DIR
from ..utils import now_str
from ..proxy import (
    requests_proxy_dict,
    apply_proxy_env,
    github_proxy_url,
    build_git_command_with_github_proxy,
)
from .constants import ONLINE_INSTALL_RUNNING, ONLINE_INSTALL_STOPPING
from .logs import append_log
from .tasks import import_task_if_needed


def get_running_install_by_script_id(script_id):
    script_id = str(script_id or "")

    for install_id, info in ONLINE_INSTALL_RUNNING.items():
        if info.get("script_id") == script_id and info.get("running"):
            return install_id, info

    return "", None


def online_install_should_stop(install_id):
    return install_id in ONLINE_INSTALL_STOPPING


def terminate_install_process(proc):
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


def request_stop_online_install(install_id):
    info = ONLINE_INSTALL_RUNNING.get(install_id)

    if not info:
        return False, "安装记录不存在或面板已重启"

    if not info.get("running"):
        return False, "安装任务已结束"

    ONLINE_INSTALL_STOPPING.add(install_id)
    info["status"] = "停止中"
    info["error"] = "用户请求停止安装"

    proc = info.get("process")
    if proc:
        terminate_install_process(proc)

    log_file = info.get("log_file")
    if log_file:
        append_log(log_file, "")
        append_log(log_file, f"===== 用户请求停止安装: {now_str()} =====")

    return True, "已请求停止安装"


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
            popen_kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen(
            cmd,
            **popen_kwargs,
        )

        if install_id and install_id in ONLINE_INSTALL_RUNNING:
            ONLINE_INSTALL_RUNNING[install_id]["process"] = proc

        while proc.poll() is None:
            if install_id and online_install_should_stop(install_id):
                append_log(log_file, "")
                append_log(log_file, "===== 检测到停止请求，正在结束当前安装命令 =====")
                terminate_install_process(proc)

                if install_id in ONLINE_INSTALL_RUNNING:
                    ONLINE_INSTALL_RUNNING[install_id]["process"] = None

                raise RuntimeError("安装已停止")

            time.sleep(0.5)

        return_code = proc.returncode

        if install_id and install_id in ONLINE_INSTALL_RUNNING:
            ONLINE_INSTALL_RUNNING[install_id]["process"] = None

    append_log(log_file, "------------------------------------------------------------")
    append_log(log_file, f"命令结束，退出码：{return_code}")

    if return_code != 0:
        raise RuntimeError(f"命令执行失败，退出码：{return_code}")


def download_online_script_logged(item, proxy_id, log_file, force=False, install_id=""):
    SCRIPT_DIR.mkdir(parents=True, exist_ok=True)

    script_type = item.get("type")
    link = item.get("link")
    target = online_script_target(item)

    append_log(log_file, "")
    append_log(log_file, "===== 下载/拉取脚本 =====")
    append_log(log_file, f"脚本类型: {script_type}")
    append_log(log_file, f"原始链接: {link}")
    append_log(log_file, f"保存目标: {target}")
    append_log(log_file, f"代理ID: {proxy_id or '不使用代理'}")
    append_log(log_file, f"允许覆盖/更新: {'是' if force else '否'}")
    append_log(log_file, "============================================================")

    if install_id and online_install_should_stop(install_id):
        raise RuntimeError("安装已停止")

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

        r = requests.get(
            real_url,
            timeout=60,
            stream=True,
            headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
            proxies=requests_proxy_dict(proxy_id),
        )
        r.raise_for_status()

        with open(target, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 128):
                if install_id and online_install_should_stop(install_id):
                    append_log(log_file, "")
                    append_log(log_file, "===== 检测到停止请求，已中断文件下载 =====")
                    raise RuntimeError("安装已停止")

                if chunk:
                    f.write(chunk)

        append_log(log_file, f"文件下载完成：{target}")

        try:
            if target.suffix.lower() in (".sh", ".bash"):
                target.chmod(target.stat().st_mode | 0o755)
                append_log(log_file, "已添加可执行权限")
        except Exception as e:
            append_log(log_file, f"添加可执行权限失败：{e}")

        return target

    if script_type == "repo":
        git_bin = shutil.which("git")
        if not git_bin:
            raise RuntimeError("未安装 git，无法拉取仓库")

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

        append_log(log_file, f"仓库拉取/更新完成：{target}")
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
    log_file = ONLINE_INSTALL_RUNNING[install_id]["log_file"]

    ONLINE_INSTALL_RUNNING[install_id]["running"] = True
    ONLINE_INSTALL_RUNNING[install_id]["status"] = "运行中"

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

    try:
        download_online_script_logged(
            item=item,
            proxy_id=proxy_id,
            log_file=log_file,
            force=force,
            install_id=install_id,
        )

        if online_install_should_stop(install_id):
            raise RuntimeError("安装已停止")

        if import_task:
            append_log(log_file, "")
            append_log(log_file, "===== 导入任务 =====")
            import_task_if_needed(
                item,
                log_file=log_file,
                enable_task=enable_task,
                selected_task_indexes=selected_task_indexes,
            )

        if online_install_should_stop(install_id):
            raise RuntimeError("安装已停止")

        install_cmd = str(item.get("install") or "").strip()

        if install_cmd:
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

        ONLINE_INSTALL_RUNNING[install_id]["running"] = False
        ONLINE_INSTALL_RUNNING[install_id]["status"] = "已完成"
        ONLINE_INSTALL_RUNNING[install_id]["returncode"] = 0
        ONLINE_INSTALL_RUNNING[install_id]["process"] = None

        append_log(log_file, "")
        append_log(log_file, f"===== 全部完成: {now_str()} =====")

    except Exception as e:
        stopped = online_install_should_stop(install_id) or str(e) == "安装已停止"

        ONLINE_INSTALL_RUNNING[install_id]["running"] = False
        ONLINE_INSTALL_RUNNING[install_id]["status"] = "已停止" if stopped else "失败"
        ONLINE_INSTALL_RUNNING[install_id]["returncode"] = -1 if stopped else 1
        ONLINE_INSTALL_RUNNING[install_id]["error"] = str(e)
        ONLINE_INSTALL_RUNNING[install_id]["process"] = None

        append_log(log_file, "")

        if stopped:
            append_log(log_file, f"===== 已停止: {now_str()} =====")
        else:
            append_log(log_file, f"===== 失败: {now_str()} =====")
            append_log(log_file, f"错误: {e}")

    finally:
        ONLINE_INSTALL_STOPPING.discard(install_id)

        if install_id in ONLINE_INSTALL_RUNNING:
            ONLINE_INSTALL_RUNNING[install_id]["process"] = None


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