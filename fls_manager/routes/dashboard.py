import os
import sys
import time
import shutil
import platform
from datetime import datetime
from math import ceil
from urllib.parse import quote

from flask import Blueprint, jsonify, request

from ..models import load_tasks, load_task_history
from ..task_runner import is_running
from ..ui.layout import layout
from ..ui.components import table_card, page_header, section, data_toolbar, empty_table_row, status_badge
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


def dashboard_history_badge(status):
    status = str(status or "")
    mapping = {
        "success": ("success", "成功"),
        "failed": ("error", "失败"),
        "timeout": ("error", "超时"),
        "stopped": ("stopped", "手动停止"),
        "running": ("running", "运行中"),
        "starting": ("starting", "启动中"),
        "delaying": ("warning", "延迟中"),
        "start_failed": ("error", "启动失败"),
    }
    tone, text = mapping.get(status, ("neutral", status or "-"))
    return status_badge(status, label=text, tone=tone)


def dashboard_history_rows(items, empty_text):
    rows = ""

    for item in items:
        log_file = str(item.get("log_file") or "")
        log_btn = ""

        if log_file:
            filename = log_file.split("/")[-1].split("\\")[-1]
            log_btn = f'<a class="btn btn-orange" href="/logfile/{h(filename)}?back=/">日志</a>'

        rows += f"""
<tr>
    <td><b>{h(item.get("task_name") or item.get("task_id") or "-")}</b></td>
    <td>{dashboard_history_badge(item.get("status"))}</td>
    <td>{h(item.get("start_at") or "-")}</td>
    <td>{h(item.get("duration_seconds", 0))} 秒</td>
    <td>{h(item.get("message") or "-")}</td>
    <td>{log_btn}</td>
</tr>
"""

    if not rows:
        rows = empty_table_row(6, empty_text)

    return rows


HISTORY_STATUS_OPTIONS = [
    ("", "全部状态"),
    ("success", "成功"),
    ("failed", "失败"),
    ("timeout", "超时"),
    ("start_failed", "启动失败"),
    ("stopped", "手动停止"),
    ("running", "运行中"),
    ("starting", "启动中"),
    ("delaying", "延迟中"),
]


def history_page_links(q, status, page, pages):
    if pages <= 1:
        return ""

    def url_for_page(p):
        url = f"/history?page={int(p)}"

        if q:
            url += "&q=" + quote(q)

        if status:
            url += "&status=" + quote(status)

        return url

    def item(p, text=None, active=False, disabled=False):
        text = text if text is not None else str(p)

        if disabled:
            return f'<span class="btn btn-gray" style="opacity:.45;cursor:not-allowed;">{h(text)}</span>'

        cls = "btn-primary" if active else "btn-gray"
        return f'<a class="btn {cls}" href="{h(url_for_page(p))}">{h(text)}</a>'

    page = max(1, min(int(page), int(pages)))
    show = {1, pages}

    for p in range(page - 2, page + 3):
        if 1 <= p <= pages:
            show.add(p)

    buttons = [item(page - 1, "上一页", disabled=page <= 1)]
    last = 0

    for p in sorted(show):
        if last and p - last > 1:
            buttons.append('<span class="btn btn-gray" style="opacity:.75;cursor:default;">...</span>')
        buttons.append(item(p, active=p == page))
        last = p

    buttons.append(item(page + 1, "下一页", disabled=page >= pages))

    return f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
        <div class="help">第 <b>{page}</b> / <b>{pages}</b> 页</div>
        <div class="action-row">{''.join(buttons)}</div>
    </div>
</div>
"""


def history_table_rows(items):
    rows = ""

    for item in items:
        log_file = str(item.get("log_file") or "")
        log_btn = ""

        if log_file:
            filename = log_file.split("/")[-1].split("\\")[-1]
            log_btn = f'<a class="btn btn-orange" href="/logfile/{h(filename)}?back=/history">日志</a>'

        rows += f"""
<tr>
    <td><b>{h(item.get("task_name") or item.get("task_id") or "-")}</b><div class="help">{h(item.get("command") or "")}</div></td>
    <td>{dashboard_history_badge(item.get("status"))}</td>
    <td>{h(item.get("start_at") or "-")}</td>
    <td>{h(item.get("end_at") or "-")}</td>
    <td>{h(item.get("duration_seconds", 0))} 秒</td>
    <td>{h(item.get("return_code") if item.get("return_code") is not None else "-")}</td>
    <td>{h(item.get("source") or "-")}</td>
    <td>{h(item.get("retry_attempt", 0))}/{h(item.get("max_retries", 0))}</td>
    <td>{h(item.get("message") or "-")}</td>
    <td>{log_btn}</td>
</tr>
"""

    if not rows:
        rows = empty_table_row(10, "暂无匹配历史", '<a class="btn btn-gray" href="/history">查看运行历史</a>')

    return rows

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
        "current_time": panel_now().strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": get_panel_timezone_text(),
        "panel_uptime": fmt_duration(time.time() - PANEL_START_TIME),
        "panel_cpu_current": panel_cpu_current,
        "panel_cpu_peak": panel_cpu_peak,
    })


@bp.route("/history")
def history_page():
    q = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()
    page = max(1, int(request.args.get("page", "1") or 1))
    per_page = 30

    allowed_statuses = {item[0] for item in HISTORY_STATUS_OPTIONS}
    if status not in allowed_statuses:
        status = ""

    items = load_task_history()

    if q:
        q_lower = q.lower()
        items = [
            item for item in items
            if q_lower in str(item.get("task_name") or "").lower()
            or q_lower in str(item.get("command") or "").lower()
            or q_lower in str(item.get("message") or "").lower()
            or q_lower in str(item.get("source") or "").lower()
        ]

    if status:
        items = [
            item for item in items
            if str(item.get("status") or "") == status
        ]

    total = len(items)
    pages = max(1, ceil(total / per_page))
    page = min(page, pages)
    show = items[(page - 1) * per_page: page * per_page]

    status_options = ""

    for value, text in HISTORY_STATUS_OPTIONS:
        selected = "selected" if value == status else ""
        status_options += f'<option value="{h(value)}" {selected}>{h(text)}</option>'

    header = page_header(
        "运行历史",
        help_html=f"记录任务每次运行、重试、失败和耗时。当前匹配 <b>{total}</b> 条。",
    )
    history_filter = data_toolbar(
        f"""
        <h2 class="section-title">筛选运行历史</h2>
        <div class="help">记录任务每次运行、重试、失败和耗时。当前匹配 <b>{total}</b> 条。</div>
        <div class="form-item">
            <label for="history-search">关键词</label>
            <input id="history-search" name="q" value="{h(q)}" placeholder="任务名 / 命令 / 说明 / 来源" aria-label="搜索运行历史">
        </div>
        <div class="form-item">
            <label for="history-status">状态</label>
            <select id="history-status" name="status">{status_options}</select>
        </div>
        <div class="action-row">
            <button class="btn btn-primary" type="submit">筛选运行历史</button>
            <a class="btn btn-gray" href="/history">重置筛选</a>
        </div>
        """,
        toolbar_id="history-filter-toolbar",
    )
    history_table = table_card(
        "运行历史",
        ["任务", "状态", "开始时间", "结束时间", "耗时", "退出码", "来源", "重试", "说明", "日志"],
        history_table_rows(show),
        section_id="history-results",
    )
    body = f"""
{header}
<section class="section" id="history-filters">
<form method="get">
{history_filter}
</form>
</section>

{history_table}

{history_page_links(q, status, page, pages)}
"""
    return layout("运行历史", "history", body)


@bp.route("/")
def dashboard():
    tasks = load_tasks()

    total = len(tasks)
    enabled = sum(1 for t in tasks if t.get("enabled", True))
    running = sum(1 for t in tasks if is_running(t["id"]))
    cron_count = sum(1 for t in tasks if str(t.get("cron", "")).strip())
    run_total = sum(int(t.get("run_count", 0)) for t in tasks)

    current_time_text = panel_now().strftime("%Y-%m-%d %H:%M:%S")
    timezone_text = get_panel_timezone_text()

    ram = get_ram_status()
    cpu_percent = get_cpu_percent()
    load_avg = get_load_avg_text()
    process_rss = get_process_rss()
    panel_cpu_current, panel_cpu_peak = get_panel_cpu_status()
    panel_cpu_peak_period = panel_cpu_peak_slot_text()
    panel_uptime = fmt_duration(time.time() - PANEL_START_TIME)
    history = load_task_history()
    recent_history = history[:8]
    abnormal_history = [
        item for item in history
        if str(item.get("status") or "") in ("failed", "timeout", "start_failed")
    ][:8]

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

    env_items = ""

    for k, v in env_data:
        env_items += f"""
<div class="environment-status-item">
    <div class="environment-status-label">{h(k)}</div>
    <div class="environment-status-value">{h(v)}</div>
</div>
"""

    history_headers = ["任务", "状态", "开始时间", "耗时", "说明", "日志"]
    recent_table = table_card(
        "最近运行",
        history_headers,
        dashboard_history_rows(recent_history, "暂无运行历史"),
        section_id="dashboard-recent",
    )
    abnormal_table = table_card(
        "最近异常",
        history_headers,
        dashboard_history_rows(abnormal_history, "暂无异常记录"),
        section_id="dashboard-errors",
    )
    environment_table = f"""
<section class="section environment-status-section" id="dashboard-environment">
    <div class="section-header">
        <h2 class="section-title">环境状态</h2>
        <div class="help">峰值 CPU 每天 00:00 和 12:00 自动重置，当前周期：{h(panel_cpu_peak_period)}</div>
    </div>
    <div class="environment-status-grid">{env_items}</div>
</section>
"""
    shortcut_section = section(
        "常用入口",
        """
<div class="action-row">
    <a class="btn btn-primary" href="/tasks">任务</a>
    <a class="btn btn-blue" href="/pull">脚本</a>
    <a class="btn btn-orange" href="/logs">日志</a>
    <a class="btn btn-primary" href="/online-scripts">在线脚本</a>
    <a class="btn btn-primary" href="/notify">通知</a>
    <a class="btn btn-gray" href="/panel/status">面板状态</a>
    <a class="btn btn-gray" href="/config">面板配置</a>
    <a class="btn btn-gray" href="/about">关于</a>
</div>
""",
        section_id="dashboard-shortcuts",
    )

    header = page_header(
        "仪表盘",
        help_html="查看任务摘要、最近活动和面板运行状态。",
        actions_html='<a class="btn btn-primary" href="/task/new">新建任务</a><a class="btn btn-gray" href="/panel/status">查看面板状态</a>',
    )
    panel_control_section = section(
        "面板控制",
        """
<div class="help">重启或停止面板会影响当前访问，请确认没有正在进行的重要操作。</div>
<div class="row-actions" aria-label="面板控制危险操作">
    <div class="row-actions-danger">
        <form method="post" action="/about/restart-panel" class="inline-form">
            <button class="btn btn-orange" type="submit" onclick="return confirm('确定重启面板吗？重启期间页面会短暂无法访问。')">
                重启面板
            </button>
        </form>
        <form method="post" action="/about/stop-panel" class="inline-form">
            <button class="btn btn-red" type="submit" onclick="return confirm('确定停止面板吗？停止后需要手动重新启动。')">
                停止面板
            </button>
        </form>
    </div>
</div>
""",
        section_id="dashboard-panel-control",
    )
    body = f"""
{header}
{panel_control_section}
<section class="section" id="dashboard-summary">
<div class="grid">
    <div class="stat">
        <div class="label">当前时间</div>
        <div class="num stat-emphasis" id="flsDashboardNow">{h(current_time_text)}</div>
    </div>

    <div class="stat">
        <div class="label">面板已运行</div>
        <div class="num stat-emphasis" id="flsDashboardUptime">{h(panel_uptime)}</div>
    </div>

    <div class="stat">
        <div class="label">任务总数</div>
        <div class="num">{total}</div>
    </div>

    <div class="stat">
        <div class="label">已启用</div>
        <div class="num">{enabled}</div>
    </div>

    <div class="stat">
        <div class="label">运行中</div>
        <div class="num">{running}</div>
    </div>

    <div class="stat">
        <div class="label">定时任务</div>
        <div class="num">{cron_count}</div>
    </div>

    <div class="stat">
        <div class="label">累计运行次数</div>
        <div class="num">{run_total}</div>
    </div>

    <div class="stat">
        <div class="label">CPU 使用率</div>
        <div class="num stat-warning">{h(cpu_percent)}</div>
    </div>

    <div class="stat">
        <div class="label">RAM 使用率</div>
        <div class="num">{h(ram.get("percent", "-"))}</div>
    </div>

    <div class="stat">
        <div class="label">RAM 可用</div>
        <div class="num stat-emphasis">{h(fmt_bytes(ram.get("available", 0)))}</div>
    </div>

    <div class="stat">
        <div class="label">RAM 总量</div>
        <div class="num stat-emphasis">{h(fmt_bytes(ram.get("total", 0)))}</div>
    </div>

    <div class="stat">
        <div class="label">面板 RAM</div>
        <div class="num stat-emphasis">{h(fmt_bytes(process_rss) if process_rss else "-")}</div>
    </div>

    <div class="stat">
        <div class="label">面板当前 CPU</div>
        <div class="num stat-warning stat-emphasis" id="flsDashboardPanelCpuCurrent">{h(panel_cpu_current)}</div>
    </div>

    <div class="stat">
        <div class="label">面板峰值 CPU</div>
        <div class="num stat-danger stat-emphasis" id="flsDashboardPanelCpuPeak">{h(panel_cpu_peak)}</div>
    </div>
</div>
</section>

{shortcut_section}
{recent_table}
{abnormal_table}
{environment_table}

<script>
const FLS_DASHBOARD_RUNTIME_INTERVAL_MS = 3000;

async function flsDashboardRefreshRuntime(){{
    if(document.hidden){{
        return;
    }}

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

function flsDashboardStartRuntimeTimer(){{
    if(window.__FLS_DASHBOARD_RUNTIME_INTERVAL__){{
        clearInterval(window.__FLS_DASHBOARD_RUNTIME_INTERVAL__);
        window.__FLS_DASHBOARD_RUNTIME_INTERVAL__ = null;
    }}

    if(!document.hidden){{
        window.__FLS_DASHBOARD_RUNTIME_INTERVAL__ = setInterval(
            flsDashboardRefreshRuntime,
            FLS_DASHBOARD_RUNTIME_INTERVAL_MS
        );
    }}
}}

if(window.__FLS_DASHBOARD_RUNTIME_INTERVAL__){{
    clearInterval(window.__FLS_DASHBOARD_RUNTIME_INTERVAL__);
    window.__FLS_DASHBOARD_RUNTIME_INTERVAL__ = null;
}}

if(window.__FLS_DASHBOARD_VISIBILITY_HANDLER__){{
    document.removeEventListener("visibilitychange", window.__FLS_DASHBOARD_VISIBILITY_HANDLER__);
}}

window.__FLS_DASHBOARD_VISIBILITY_HANDLER__ = function(){{
    if(document.hidden){{
        if(window.__FLS_DASHBOARD_RUNTIME_INTERVAL__){{
            clearInterval(window.__FLS_DASHBOARD_RUNTIME_INTERVAL__);
            window.__FLS_DASHBOARD_RUNTIME_INTERVAL__ = null;
        }}
    }}else{{
        flsDashboardRefreshRuntime();
        flsDashboardStartRuntimeTimer();
    }}
}};

document.addEventListener("visibilitychange", window.__FLS_DASHBOARD_VISIBILITY_HANDLER__);

flsDashboardRefreshRuntime();
flsDashboardStartRuntimeTimer();
</script>
"""
    return layout("仪表盘", "dashboard", body)
