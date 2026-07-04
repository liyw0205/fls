import os
import time
import uuid
import signal
from urllib.parse import quote
import threading
import subprocess

from flask import redirect, abort, jsonify, request

from . import bp
from .helpers import (
    script_safe_path,
    script_rel_path,
    debug_task_for_script,
    debug_command_info,
)

from ...paths import LOG_DIR
from ...utils import h, now_str, safe_name, get_back_url
from ...ui.layout import layout
from ...ui.components import page_header_card
from ...ui.log_controls import log_controls
from ...logs import tail_file
from ...models import load_global_env
from ...constants import TASK_PROCESS_PREFIX

SCRIPT_DEBUG_RUNNING = {}


def script_debug_log_file(debug_id, script_name):
    safe = safe_name(script_name or "script-debug")
    return LOG_DIR / f"script-debug-{safe}-{debug_id}.log"


def terminate_debug_process(proc):
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


def script_debug_watch(debug_id, proc, log_fp):
    try:
        return_code = proc.wait()

        try:
            log_fp.write(
                f"\n===== 调试运行结束: {now_str()}，退出码: {return_code} =====\n".encode("utf-8")
            )
            log_fp.close()
        except Exception:
            pass

        if debug_id in SCRIPT_DEBUG_RUNNING:
            SCRIPT_DEBUG_RUNNING[debug_id]["running"] = False
            SCRIPT_DEBUG_RUNNING[debug_id]["returncode"] = return_code
            SCRIPT_DEBUG_RUNNING[debug_id]["process"] = None

    except Exception as e:
        try:
            log_fp.write(
                f"\n===== 调试监听异常: {e} =====\n".encode("utf-8")
            )
            log_fp.close()
        except Exception:
            pass

        if debug_id in SCRIPT_DEBUG_RUNNING:
            SCRIPT_DEBUG_RUNNING[debug_id]["running"] = False
            SCRIPT_DEBUG_RUNNING[debug_id]["returncode"] = -1
            SCRIPT_DEBUG_RUNNING[debug_id]["error"] = str(e)
            SCRIPT_DEBUG_RUNNING[debug_id]["process"] = None


@bp.route("/scripts/debug/<path:rel_path>")
def scripts_debug_run(rel_path):
    back_url = get_back_url("/pull")

    try:
        target = script_safe_path(rel_path)
    except Exception as e:
        return f"路径非法：{h(e)}", 400

    if not target.exists() or not target.is_file():
        abort(404)

    rel = script_rel_path(target)

    debug_id = uuid.uuid4().hex
    log_file = script_debug_log_file(debug_id, target.name)
    log_fp = open(log_file, "ab", buffering=0)

    task = debug_task_for_script(rel, target, debug_id)

    try:
        cmd_info = debug_command_info(task)

        env = os.environ.copy()
        env.update(load_global_env())
        env["PYTHONUNBUFFERED"] = "1"
        env["FLS_SCRIPT_DEBUG_ID"] = debug_id
        env["FLS_SCRIPT_DEBUG_FILE"] = str(target)
        env["FLS_TASK_PROCESS_NAME"] = (TASK_PROCESS_PREFIX + safe_name(f"script-debug-{target.name}"))[:120]

        display_cmd = cmd_info.get("display_cmd", cmd_info.get("cmd"))

        header = (
            f"===== 脚本调试运行 =====\n"
            f"时间: {now_str()}\n"
            f"调试ID: {debug_id}\n"
            f"脚本: {target}\n"
            f"相对路径: {rel}\n"
            f"命令: {task['command']}\n"
            f"工作目录: {cmd_info.get('cwd')}\n"
            f"实际启动命令: {display_cmd}\n"
            f"日志文件: {log_file}\n"
            f"============================================================\n"
        )

        log_fp.write(header.encode("utf-8"))

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
            **popen_kwargs,
        )

        SCRIPT_DEBUG_RUNNING[debug_id] = {
            "id": debug_id,
            "script": str(target),
            "rel": rel,
            "log_file": str(log_file),
            "running": True,
            "process": proc,
            "pid": proc.pid,
            "start_time": time.time(),
            "returncode": None,
            "error": "",
        }

        th = threading.Thread(
            target=script_debug_watch,
            args=(debug_id, proc, log_fp),
            daemon=True,
            name=f"fls-script-debug-{debug_id[:8]}",
        )
        th.start()

    except Exception as e:
        try:
            log_fp.write(f"启动调试失败: {e}\n".encode("utf-8"))
            log_fp.close()
        except Exception:
            pass

        SCRIPT_DEBUG_RUNNING[debug_id] = {
            "id": debug_id,
            "script": str(target),
            "rel": rel,
            "log_file": str(log_file),
            "running": False,
            "process": None,
            "pid": "-",
            "start_time": time.time(),
            "returncode": -1,
            "error": str(e),
        }

    return redirect(f"/scripts/debug-log/{debug_id}?back={quote(back_url)}")


@bp.route("/scripts/debug-stop/<debug_id>", methods=["POST"])
def scripts_debug_stop(debug_id):
    info = SCRIPT_DEBUG_RUNNING.get(debug_id)

    if info and info.get("running"):
        proc = info.get("process")

        if proc:
            terminate_debug_process(proc)

        info["running"] = False
        info["process"] = None
        info["error"] = "用户手动停止"

        log_file = info.get("log_file")

        if log_file:
            try:
                with open(log_file, "ab") as f:
                    f.write(
                        f"\n===== 用户手动停止调试: {now_str()} =====\n".encode("utf-8")
                    )
            except Exception:
                pass

    return redirect(get_back_url("/pull"))


@bp.route("/scripts/debug-log/<debug_id>")
def scripts_debug_log(debug_id):
    back_url = get_back_url("/pull")
    info = SCRIPT_DEBUG_RUNNING.get(debug_id)

    if not info:
        body = page_header_card(
            "脚本调试日志",
            help_html="""
        调试记录不存在或面板已重启。<br>
        可以到日志管理里查找 <code>script-debug-*.log</code>。
""",
            actions_html=f"""
<a class="btn btn-gray" href="{h(back_url)}">返回</a>
<a class="btn btn-blue" href="/logs?back={h(back_url)}">日志管理</a>
""",
        )
        return layout("脚本调试日志", "pull", body)

    stop_btn = ""

    if info.get("running"):
        stop_btn = f"""
<form class="inline-form" method="post" action="/scripts/debug-stop/{h(debug_id)}?back={h(back_url)}">
    <button class="btn btn-red" type="submit" onclick="return confirm('确定停止该调试运行吗？')">
        停止调试
    </button>
</form>
"""

    header_card = page_header_card(
        "脚本调试日志",
        help_html=f"""
        状态：<b id="debugStatus">{"运行中" if info.get("running") else "已结束"}</b><br>
        PID：{h(info.get("pid") or "-")}<br>
        脚本：{h(info.get("script") or "-")}<br>
        日志文件：{h(info.get("log_file") or "-")}
""",
        actions_html=f"""
{stop_btn}
<a class="btn btn-gray" href="{h(back_url)}">返回</a>
<a class="btn btn-blue" href="/pull">脚本管理</a>
""",
    )

    body = f"""
{header_card}

<pre class="log" id="log">加载中...</pre>
{log_controls()}

<script>
window.__FLS_LOG_LAST_TEXT__ = "";
window.__FLS_LOG_NEAR_BOTTOM__ = true;

function nearBottom(){{
    return document.documentElement.scrollHeight - window.innerHeight - window.scrollY < 90;
}}

window.addEventListener("scroll", function(){{
    window.__FLS_LOG_NEAR_BOTTOM__ = nearBottom();
}}, {{passive:true}});

async function loadScriptDebugLog(){{
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const wasNearBottom = nearBottom();

        const res = await fetch("/api/scripts/debug-log/{h(debug_id)}?lines=1600", {{cache:"no-store"}});
        const json = await res.json();

        const statusEl = document.getElementById("debugStatus");
        if(statusEl) statusEl.textContent = json.running ? "运行中" : "已结束";

        const text = json.log || "暂无日志";
        const old = window.__FLS_LOG_LAST_TEXT__ || "";
        const changed = text !== old;

        var logEl = document.getElementById("log");

        if(typeof flsRenderLogText === "function"){{
            flsRenderLogText(logEl, text);
        }}else{{
            logEl.textContent = text;
        }}

        window.__FLS_LOG_LAST_TEXT__ = text;

        if(changed){{
            if(wasNearBottom || window.__FLS_LOG_NEAR_BOTTOM__){{
                const tip = document.getElementById("flsLogNewTip");
                if(tip) tip.style.display = "none";
                window.scrollTo(0, document.documentElement.scrollHeight);
            }}else{{
                const afterHeight = document.documentElement.scrollHeight;
                window.scrollTo(0, beforeScroll + Math.max(afterHeight - beforeHeight, 0));
                const tip = document.getElementById("flsLogNewTip");
                if(tip) tip.style.display = "block";
            }}
        }}

        if(!json.running){{
            clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
            window.__FLS_ACTIVE_LOG_INTERVAL__ = null;
        }}
    }} catch(e) {{
        document.getElementById("log").textContent = "日志读取失败: " + e;
    }}
}}

if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
loadScriptDebugLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadScriptDebugLog, 2000);
</script>
"""
    return layout("脚本调试日志", "pull", body)


@bp.route("/api/scripts/debug-log/<debug_id>")
def api_scripts_debug_log(debug_id):
    info = SCRIPT_DEBUG_RUNNING.get(debug_id)

    if not info:
        return jsonify({
            "running": False,
            "log": "调试记录不存在或面板已重启。请到日志管理中查找 script-debug-*.log。",
        })

    lines = int(request.args.get("lines", "1200") or 1200)

    return jsonify({
        "running": bool(info.get("running")),
        "returncode": info.get("returncode"),
        "error": info.get("error", ""),
        "log_file": info.get("log_file", ""),
        "log": tail_file(info.get("log_file", ""), lines),
    })
