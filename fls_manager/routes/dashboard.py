from flask import Blueprint
from ..models import load_tasks
from ..task_runner import is_running
from ..ui.layout import layout
from ..ui.tables import tasks_table

bp = Blueprint("dashboard", __name__)

@bp.route("/")
def dashboard():
    tasks = load_tasks()
    total = len(tasks)
    enabled = sum(1 for t in tasks if t.get("enabled", True))
    running = sum(1 for t in tasks if is_running(t["id"]))
    cron_count = sum(1 for t in tasks if str(t.get("cron", "")).strip())
    run_total = sum(int(t.get("run_count", 0)) for t in tasks)

    body = f"""
<div class="grid">
    <div class="stat"><div class="label">任务总数</div><div class="num">{total}</div></div>
    <div class="stat"><div class="label">已启用</div><div class="num" style="color:#18a058;">{enabled}</div></div>
    <div class="stat"><div class="label">运行中</div><div class="num" style="color:#2563eb;">{running}</div></div>
    <div class="stat"><div class="label">定时任务</div><div class="num" style="color:#f59e0b;">{cron_count}</div></div>
    <div class="stat"><div class="label">累计运行次数</div><div class="num" style="color:#7c3aed;">{run_total}</div></div>
</div>
<div class="card">
    <div class="card-title">任务状态</div>
    <div class="table-wrap">{tasks_table(tasks)}</div>
</div>
"""
    return layout("仪表盘", "dashboard", body)
