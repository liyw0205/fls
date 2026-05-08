import os
import sys
import time
import shutil
import platform
from datetime import datetime

from flask import Blueprint, jsonify

from ..models import load_tasks
from ..task_runner import is_running
from ..ui.layout import layout
from ..utils import h
from ..paths import BASE_DIR, DATA_DIR, LOG_DIR, SCRIPT_DIR
from ..config import get_host, get_port, fls_get_admin_token, panel_now, get_panel_timezone_text
from ..state import PANEL_START_TIME, PANEL_START_STR
from ..constants import MAIN_PROCESS_NAME, TASK_PROCESS_PREFIX

bp = Blueprint("dashboard", __name__)

# 面板进程 CPU 采样状态
_PANEL_CPU_LAST = None
_PANEL_CPU_PEAK = 0.0

# 面板 CPU 峰值统计周期。
# 每天 00:00 和 12:00 分成两个周期：
#   YYYY-MM-DD-00
#   YYYY-MM-DD-12
_PANEL_CPU_PEAK_SLOT = None


def fmt_bytes(n):
    try:
        n = float(n)
    except Exception:
        return "-"

    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0

    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1

    return f"{n:.1f} {units[i]}"


def fmt_duration(seconds):
    try:
        seconds = int(seconds)
    except Exception:
        return "-"

    if seconds < 0:
        seconds = 0

    days = seconds // 86400
    seconds %= 86400

    hours = seconds // 3600
    seconds %= 3600

    minutes = seconds // 60
    seconds %= 60

    parts = []

    if days:
        parts.append(f"{days} 天")

    if hours:
        parts.append(f"{hours} 小时")

    if minutes:
        parts.append(f"{minutes} 分钟")

    if not parts:
        parts.append(f"{seconds} 秒")

    return " ".join(parts)

def panel_cpu_peak_slot():
    """
    返回当前面板 CPU 峰值统计周期。

    每天两个周期：
    - 00:00 - 11:59 => YYYY-MM-DD-00
    - 12:00 - 23:59 => YYYY-MM-DD-12
    """
    now = datetime.now()
    slot_hour = 0 if now.hour < 12 else 12
    return now.strftime("%Y-%m-%d") + f"-{slot_hour:02d}"


def panel_cpu_peak_slot_text():
    """
    返回当前峰值周期的人类可读文本。
    """
    now = datetime.now()

    if now.hour < 12:
        return now.strftime("%Y-%m-%d") + " 00:00 - 12:00"
    else:
        return now.strftime("%Y-%m-%d") + " 12:00 - 24:00"


def read_mem_info():
    """
    读取 /proc/meminfo。
    返回单位：Bytes
    """
    info = {}

    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if ":" not in line:
                    continue

                key, value = line.split(":", 1)
                parts = value.strip().split()

                if not parts:
                    continue

                try:
                    # /proc/meminfo 默认单位是 kB
                    info[key] = int(parts[0]) * 1024
                except Exception:
                    pass

    except Exception:
        pass

    return info


def get_ram_status():
    mem = read_mem_info()

    total = int(mem.get("MemTotal", 0) or 0)
    available = int(mem.get("MemAvailable", 0) or 0)

    if total <= 0:
        return {
            "total": 0,
            "available": 0,
            "used": 0,
            "percent": "-",
        }

    used = max(0, total - available)
    percent = used / total * 100

    return {
        "total": total,
        "available": available,
        "used": used,
        "percent": f"{percent:.1f}%",
    }


def read_proc_stat_cpu():
    """
    读取 /proc/stat 第一行 CPU 时间。
    返回：
      idle, total
    """
    try:
        with open("/proc/stat", "r", encoding="utf-8", errors="ignore") as f:
            line = f.readline()

        parts = line.strip().split()

        if not parts or parts[0] != "cpu":
            return None

        nums = [int(x) for x in parts[1:]]

        idle = nums[3] if len(nums) > 3 else 0
        iowait = nums[4] if len(nums) > 4 else 0

        idle_all = idle + iowait
        total = sum(nums)

        return idle_all, total

    except Exception:
        return None


def get_cpu_percent():
    """
    读取系统 CPU 使用率。
    不依赖 psutil，通过 /proc/stat 采样计算。
    """
    first = read_proc_stat_cpu()

    if not first:
        return "-"

    time.sleep(0.08)

    second = read_proc_stat_cpu()

    if not second:
        return "-"

    idle1, total1 = first
    idle2, total2 = second

    idle_delta = idle2 - idle1
    total_delta = total2 - total1

    if total_delta <= 0:
        return "-"

    usage = (1 - idle_delta / total_delta) * 100

    if usage < 0:
        usage = 0

    if usage > 100:
        usage = 100

    return f"{usage:.1f}%"


def get_load_avg_text():
    try:
        load1, load5, load15 = os.getloadavg()
        return f"{load1:.2f} / {load5:.2f} / {load15:.2f}"
    except Exception:
        return "-"


def get_process_rss():
    """
    获取当前 FLS 面板进程实际内存占用 RSS。
    返回单位：Bytes
    """
    try:
        with open("/proc/self/status", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except Exception:
        pass

    return 0


def read_total_cpu_jiffies():
    """
    读取系统总 CPU jiffies。
    """
    try:
        with open("/proc/stat", "r", encoding="utf-8", errors="ignore") as f:
            line = f.readline()

        parts = line.strip().split()

        if not parts or parts[0] != "cpu":
            return None

        nums = [int(x) for x in parts[1:]]
        return sum(nums)

    except Exception:
        return None


def read_process_cpu_jiffies():
    """
    读取当前面板进程 CPU jiffies。

    /proc/self/stat:
      utime 第 14 项
      stime 第 15 项

    注意：
    comm 字段在括号中，可能包含空格，所以不能直接 split 整行。
    """
    try:
        with open("/proc/self/stat", "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()

        end = text.rfind(")")
        if end < 0:
            return None

        after = text[end + 2:].split()

        # after[0] 是 state，对应原始第 3 项。
        # 原始第 14 项 utime => after[11]
        # 原始第 15 项 stime => after[12]
        utime = int(after[11])
        stime = int(after[12])

        return utime + stime

    except Exception:
        return None


def get_panel_cpu_status():
    """
    获取面板进程当前 CPU 占用率和峰值 CPU 占用率。

    返回：
      current_text: 当前占用率文本
      peak_text: 峰值占用率文本

    峰值重置规则：
    - 每天 00:00 重置一次；
    - 每天 12:00 重置一次；
    - 即一天两个统计周期。

    说明：
    - 第一次访问没有前后两次采样，所以当前值显示 0.0%；
    - 峰值从当前统计周期开始累计；
    - 只有访问仪表盘时才会采样更新；
    - 多核环境下，进程 CPU 占用可能超过 100%。
      例如 200% 表示约占满 2 个核心。
    """
    global _PANEL_CPU_LAST, _PANEL_CPU_PEAK, _PANEL_CPU_PEAK_SLOT

    current_slot = panel_cpu_peak_slot()

    # 首次初始化或跨过 00:00 / 12:00 后，重置峰值。
    if _PANEL_CPU_PEAK_SLOT != current_slot:
        _PANEL_CPU_PEAK_SLOT = current_slot
        _PANEL_CPU_PEAK = 0.0
        _PANEL_CPU_LAST = None

    total = read_total_cpu_jiffies()
    proc = read_process_cpu_jiffies()

    if total is None or proc is None:
        return "-", "-"

    cpu_count = os.cpu_count() or 1

    if _PANEL_CPU_LAST is None:
        _PANEL_CPU_LAST = {
            "total": total,
            "proc": proc,
        }
        return "0.0%", f"{_PANEL_CPU_PEAK:.1f}%"

    total_delta = total - int(_PANEL_CPU_LAST.get("total", total))
    proc_delta = proc - int(_PANEL_CPU_LAST.get("proc", proc))

    _PANEL_CPU_LAST = {
        "total": total,
        "proc": proc,
    }

    if total_delta <= 0 or proc_delta < 0:
        current = 0.0
    else:
        # 乘以 CPU 核心数，得到类似 top/htop 的进程 CPU 百分比。
        current = (proc_delta / total_delta) * cpu_count * 100

    if current < 0:
        current = 0.0

    if current > _PANEL_CPU_PEAK:
        _PANEL_CPU_PEAK = current

    return f"{current:.1f}%", f"{_PANEL_CPU_PEAK:.1f}%"


@bp.route("/api/dashboard/runtime")
def api_dashboard_runtime():
    """
    仪表盘动态数据。

    用于前端定时刷新：
    - 当前时间
    - 面板已运行时间
    - 面板当前 CPU
    - 面板峰值 CPU
    """
    panel_cpu_current, panel_cpu_peak = get_panel_cpu_status()

    return jsonify({
        "ok": True,
        "current_time": panel_now().strftime("%Y %m-%d %H:%M:%S"),
        "timezone": get_panel_timezone_text(),
        "panel_uptime": fmt_duration(time.time() - PANEL_START_TIME),
        "panel_cpu_current": panel_cpu_current,
        "panel_cpu_peak": panel_cpu_peak,
    })


@bp.route("/")
def dashboard():
    tasks = load_tasks()

    total = len(tasks)
    enabled = sum(1 for t in tasks if t.get("enabled", True))
    running = sum(1 for t in tasks if is_running(t["id"]))
    cron_count = sum(1 for t in tasks if str(t.get("cron", "")).strip())
    run_total = sum(int(t.get("run_count", 0)) for t in tasks)

    current_time_text = panel_now().strftime("%Y %m-%d %H:%M:%S")
    timezone_text = get_panel_timezone_text()

    ram = get_ram_status()
    cpu_percent = get_cpu_percent()
    load_avg = get_load_avg_text()
    process_rss = get_process_rss()
    panel_cpu_current, panel_cpu_peak = get_panel_cpu_status()
    panel_cpu_peak_period = panel_cpu_peak_slot_text()
    panel_uptime = fmt_duration(time.time() - PANEL_START_TIME)

    try:
        disk = shutil.disk_usage(str(BASE_DIR))
        disk_total = fmt_bytes(disk.total)
        disk_used = fmt_bytes(disk.used)
        disk_free = fmt_bytes(disk.free)
    except Exception:
        disk_total = disk_used = disk_free = "-"

    env_data = [
        ("面板时区", timezone_text),
        ("系统", platform.platform()),
        ("Python", sys.version.split()[0]),
        ("工作目录", str(BASE_DIR)),
        ("数据目录", str(DATA_DIR)),
        ("日志目录", str(LOG_DIR)),
        ("脚本目录", str(SCRIPT_DIR)),
        ("主进程 PID", os.getpid()),
        ("主进程名", MAIN_PROCESS_NAME),
        ("任务进程名前缀", TASK_PROCESS_PREFIX),
        ("Host / Port", f"{get_host()}:{get_port()}"),
        ("鉴权", "已开启" if fls_get_admin_token() else "未开启"),
        ("面板启动时间", PANEL_START_STR),
        ("面板已运行", panel_uptime),

        ("CPU 使用率", cpu_percent),
        ("CPU 负载 1/5/15 分钟", load_avg),
        ("面板当前 CPU", panel_cpu_current),
        ("面板峰值 CPU", panel_cpu_peak),
        ("面板峰值统计周期", panel_cpu_peak_period),

        ("RAM 总量", fmt_bytes(ram.get("total", 0))),
        ("RAM 已用", fmt_bytes(ram.get("used", 0))),
        ("RAM 可用", fmt_bytes(ram.get("available", 0))),
        ("RAM 使用率", ram.get("percent", "-")),
        ("面板进程 RAM", fmt_bytes(process_rss) if process_rss else "-"),

        ("磁盘总量", disk_total),
        ("磁盘已用", disk_used),
        ("磁盘可用", disk_free),
    ]

    env_rows = ""

    for k, v in env_data:
        env_rows += f"""
<tr>
    <td><b>{h(k)}</b></td>
    <td>{h(v)}</td>
</tr>
"""

    body = f"""
<div class="grid">
    <div class="stat">
        <div class="label">当前时间</div>
        <div class="num" id="flsDashboardNow" style="color:#2563eb;font-size:22px;">{h(current_time_text)}</div>
    </div>

    <div class="stat">
        <div class="label">面板已运行</div>
        <div class="num" id="flsDashboardUptime" style="color:#18a058;font-size:22px;">{h(panel_uptime)}</div>
    </div>

    <div class="stat">
        <div class="label">任务总数</div>
        <div class="num">{total}</div>
    </div>

    <div class="stat">
        <div class="label">已启用</div>
        <div class="num" style="color:#18a058;">{enabled}</div>
    </div>

    <div class="stat">
        <div class="label">运行中</div>
        <div class="num" style="color:#2563eb;">{running}</div>
    </div>

    <div class="stat">
        <div class="label">定时任务</div>
        <div class="num" style="color:#f59e0b;">{cron_count}</div>
    </div>

    <div class="stat">
        <div class="label">累计运行次数</div>
        <div class="num" style="color:#7c3aed;">{run_total}</div>
    </div>

    <div class="stat">
        <div class="label">CPU 使用率</div>
        <div class="num" style="color:#dc2626;">{h(cpu_percent)}</div>
    </div>

    <div class="stat">
        <div class="label">RAM 使用率</div>
        <div class="num" style="color:#2563eb;">{h(ram.get("percent", "-"))}</div>
    </div>

    <div class="stat">
        <div class="label">RAM 可用</div>
        <div class="num" style="color:#18a058;font-size:22px;">{h(fmt_bytes(ram.get("available", 0)))}</div>
    </div>

    <div class="stat">
        <div class="label">RAM 总量</div>
        <div class="num" style="color:#7c3aed;font-size:22px;">{h(fmt_bytes(ram.get("total", 0)))}</div>
    </div>

    <div class="stat">
        <div class="label">面板 RAM</div>
        <div class="num" style="color:#f59e0b;font-size:22px;">{h(fmt_bytes(process_rss) if process_rss else "-")}</div>
    </div>

    <div class="stat">
        <div class="label">面板当前 CPU</div>
        <div class="num" id="flsDashboardPanelCpuCurrent" style="color:#f59e0b;font-size:22px;">{h(panel_cpu_current)}</div>
    </div>

    <div class="stat">
        <div class="label">面板峰值 CPU</div>
        <div class="num" id="flsDashboardPanelCpuPeak" style="color:#dc2626;font-size:22px;">{h(panel_cpu_peak)}</div>
    </div>
</div>

<div class="card">
    <div class="card-title">常用入口</div>
    <div class="action-row">
        <a class="btn btn-primary" href="/tasks">任务管理</a>
        <a class="btn btn-blue" href="/pull">脚本管理</a>
        <a class="btn btn-orange" href="/logs">日志管理</a>
        <a class="btn btn-primary" href="/online-scripts">在线脚本</a>
        <a class="btn btn-primary" href="/notify">通知管理</a>
        <a class="btn btn-gray" href="/panel/status">环境状态</a>
        <a class="btn btn-gray" href="/config">配置</a>
        <a class="btn btn-gray" href="/about">关于</a>
    </div>
</div>

<div class="card">
    <div class="card-title">环境状态</div>
    <div class="help">
        面板峰值 CPU 每天 00:00 和 12:00 自动重置。<br>
        当前峰值统计周期：{h(panel_cpu_peak_period)}
    </div>
    <br>
    <div class="table-wrap">
        <table>
            <tbody>{env_rows}</tbody>
        </table>
    </div>
</div>

<script>
async function flsDashboardRefreshRuntime(){{
    try {{
        const nowEl = document.getElementById("flsDashboardNow");
        const uptimeEl = document.getElementById("flsDashboardUptime");
        const cpuEl = document.getElementById("flsDashboardPanelCpuCurrent");
        const peakEl = document.getElementById("flsDashboardPanelCpuPeak");

        if(!nowEl && !uptimeEl && !cpuEl && !peakEl){{
            if(window.__FLS_DASHBOARD_RUNTIME_INTERVAL__){{
                clearInterval(window.__FLS_DASHBOARD_RUNTIME_INTERVAL__);
                window.__FLS_DASHBOARD_RUNTIME_INTERVAL__ = null;
            }}
            return;
        }}

        const res = await fetch("/api/dashboard/runtime", {{cache:"no-store"}});
        const json = await res.json();

        if(!json.ok) return;

        if(nowEl) nowEl.textContent = json.current_time || "-";
        if(uptimeEl) uptimeEl.textContent = json.panel_uptime || "-";
        if(cpuEl) cpuEl.textContent = json.panel_cpu_current || "-";
        if(peakEl) peakEl.textContent = json.panel_cpu_peak || "-";
    }} catch(e) {{}}
}}

if(window.__FLS_DASHBOARD_RUNTIME_INTERVAL__){{
    clearInterval(window.__FLS_DASHBOARD_RUNTIME_INTERVAL__);
    window.__FLS_DASHBOARD_RUNTIME_INTERVAL__ = null;
}}

flsDashboardRefreshRuntime();
window.__FLS_DASHBOARD_RUNTIME_INTERVAL__ = setInterval(flsDashboardRefreshRuntime, 500);
</script>
"""
    return layout("仪表盘", "dashboard", body)