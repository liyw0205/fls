import uuid
from flask import Blueprint, request, redirect, url_for, abort, Response

from ..models import load_tasks, save_tasks, get_task
from ..utils import h, now_str
from ..ui.layout import layout
from ..ui.log_controls import log_controls
from ..ui.tables import tasks_table
from ..scheduler import reload_scheduler, cron_to_trigger
from ..task_runner import run_task_now, stop_task_now, is_running
from ..logs import latest_log_for_task, tail_file
from ..state import RUNNING
from ..proxy import proxy_select_options
from ..notify import notify_select_options

bp = Blueprint("tasks", __name__)


def parse_task_env_from_form():
    keys = request.form.getlist("env_key")
    values = request.form.getlist("env_value")
    env = {}

    for idx, key in enumerate(keys):
        key = str(key or "").strip()
        if not key:
            continue
        value = values[idx] if idx < len(values) else ""
        env[key] = value

    return env


def task_env_rows(env):
    env = env or {}
    rows = ""

    for key in sorted(env.keys()):
        value = str(env.get(key, ""))

        rows += f"""
<tr>
    <td><input name="env_key" value="{h(key)}" placeholder="变量名"></td>
    <td><textarea name="env_value" style="min-height:72px;" placeholder="变量值">{h(value)}</textarea></td>
    <td><button class="btn btn-red" type="button" onclick="flsTaskEnvRemoveRow(this)">删除</button></td>
</tr>
"""
    return rows


def normalize_task_notify(task):
    notify = task.get("notify")

    if isinstance(notify, dict):
        mode = notify.get("mode", "none")
        ids = notify.get("ids", []) if isinstance(notify.get("ids"), list) else []

        if mode in ("none", "default", "custom"):
            return {"mode": mode, "ids": ids}

    old_ids = task.get("notify_ids")

    if isinstance(old_ids, list) and old_ids:
        if "__none__" in old_ids:
            return {"mode": "none", "ids": []}
        if "__default__" in old_ids:
            return {"mode": "default", "ids": []}
        return {"mode": "custom", "ids": old_ids}

    return {"mode": "default", "ids": []}


def parse_notify_from_form():
    mode = request.form.get("notify_mode", "default").strip()

    if mode == "default":
        return {"mode": "default", "ids": []}

    if mode == "custom":
        ids = [x for x in request.form.getlist("notify_ids") if x]
        if not ids:
            return {"mode": "none", "ids": []}
        return {"mode": "custom", "ids": ids}

    return {"mode": "none", "ids": []}


def normalize_task_random_delay(task):
    delay = task.get("random_delay")

    if isinstance(delay, dict):
        mode = delay.get("mode", "none")
        seconds = delay.get("seconds", 0)

        try:
            seconds = int(seconds or 0)
        except Exception:
            seconds = 0

        seconds = max(0, min(120, seconds))

        if mode in ("none", "default", "custom"):
            return {
                "mode": mode,
                "seconds": seconds,
            }

    return {
        "mode": "none",
        "seconds": 0,
    }


def parse_random_delay_from_form():
    mode = request.form.get("random_delay_mode", "none").strip()

    if mode == "default":
        return {
            "mode": "default",
            "seconds": 0,
        }

    if mode == "custom":
        try:
            seconds = int(request.form.get("random_delay_seconds", "0") or 0)
        except Exception:
            seconds = 0

        seconds = max(1, min(120, seconds))

        return {
            "mode": "custom",
            "seconds": seconds,
        }

    return {
        "mode": "none",
        "seconds": 0,
    }


def task_form(task=None):
    if task is None:
        task = {
            "name": "",
            "remark": "",
            "command": "task ",
            "cron": "",
            "enabled": True,
            "env": {},
            "proxy_id": "",
            "notify": {"mode": "default", "ids": []},
            "random_delay": {"mode": "none", "seconds": 0},
        }

    checked = "checked" if task.get("enabled", True) else ""
    proxy_options = proxy_select_options(task.get("proxy_id", ""))

    notify = normalize_task_notify(task)
    notify_mode = notify.get("mode", "default")
    notify_ids = notify.get("ids", [])

    mode_none_checked = "checked" if notify_mode == "none" else ""
    mode_default_checked = "checked" if notify_mode == "default" else ""
    mode_custom_checked = "checked" if notify_mode == "custom" else ""

    notify_options = notify_select_options(notify_ids)

    random_delay = normalize_task_random_delay(task)
    random_delay_mode = random_delay.get("mode", "none")
    random_delay_seconds = int(random_delay.get("seconds", 0) or 0)

    delay_none_checked = "checked" if random_delay_mode == "none" else ""
    delay_default_checked = "checked" if random_delay_mode == "default" else ""
    delay_custom_checked = "checked" if random_delay_mode == "custom" else ""

    env_rows = task_env_rows(task.get("env", {}) or {})
    empty_style = "" if not env_rows else "display:none;"

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">任务信息</div>
    <div class="form-grid">
        <div class="form-item">
            <label>任务名，必填</label>
            <input name="name" required value="{h(task.get('name', ''))}">
        </div>
        <div class="form-item">
            <label>Cron 表达式</label>
            <input name="cron" value="{h(task.get('cron', ''))}" placeholder="0 8 * * *">
            <div class="help">留空表示手动任务。支持 5 位或 6 位 Cron。</div>
        </div>
    </div>

    <br>

    <div class="form-item">
        <label>备注，可空</label>
        <input name="remark" value="{h(task.get('remark', ''))}" placeholder="例如：账号1、主号、备用任务等">
        <div class="help">备注默认为空，仅用于任务列表展示和区分任务。</div>
    </div>

    <br>

    <div class="form-item">
        <label>命令，必填</label>
        <input name="command" required value="{h(task.get('command', ''))}" placeholder="task 1.py">
    </div>

    <br>

    <div class="form-item">
        <label>代理</label>
        <select name="proxy_id">{proxy_options}</select>
        <div class="help">只显示已启用代理。任务运行时会自动注入 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY。</div>
    </div>

    <br>

    <div class="form-item">
        <label>任务结束通知</label>

        <label style="display:block;margin:8px 0;">
            <input type="radio" name="notify_mode" value="none" {mode_none_checked} style="width:auto;" onchange="toggleNotifyBox()">
            不通知
        </label>

        <label style="display:block;margin:8px 0;">
            <input type="radio" name="notify_mode" value="default" {mode_default_checked} style="width:auto;" onchange="toggleNotifyBox()">
            使用全局默认通知
        </label>

        <label style="display:block;margin:8px 0;">
            <input type="radio" name="notify_mode" value="custom" {mode_custom_checked} style="width:auto;" onchange="toggleNotifyBox()">
            指定通知渠道
        </label>

        <div id="notifyCustomBox" style="display:none;margin-top:10px;">
            <select name="notify_ids" multiple size="8">{notify_options}</select>
            <div class="help">可以多选。选择指定通知但未选择任何渠道时，会自动按“不通知”处理。</div>
        </div>
    </div>

    <br>

    <div class="form-item">
        <label>随机延迟</label>

        <label style="display:block;margin:8px 0;">
            <input type="radio" name="random_delay_mode" value="none" {delay_none_checked} style="width:auto;" onchange="toggleRandomDelayBox()">
            不启用
        </label>

        <label style="display:block;margin:8px 0;">
            <input type="radio" name="random_delay_mode" value="default" {delay_default_checked} style="width:auto;" onchange="toggleRandomDelayBox()">
            使用配置里的全局随机延迟
        </label>

        <label style="display:block;margin:8px 0;">
            <input type="radio" name="random_delay_mode" value="custom" {delay_custom_checked} style="width:auto;" onchange="toggleRandomDelayBox()">
            自定义随机延迟
        </label>

        <div id="randomDelayCustomBox" style="display:none;margin-top:10px;">
            <input name="random_delay_seconds" type="number" min="1" max="120" value="{h(random_delay_seconds or 1)}">
            <div class="help">
                范围 1-120 秒。任务启动前会随机等待 1 到该秒数。
            </div>
        </div>
    </div>

    <br>

    <label>
        <input type="checkbox" name="enabled" value="1" {checked} style="width:auto;">
        启用任务
    </label>
</div>

<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">任务变量</div>
            <div class="help">任务变量仅对此任务生效，会覆盖同名全局变量。</div>
        </div>
        <button class="btn btn-primary" type="button" onclick="flsTaskEnvAddRow()">新增任务变量</button>
    </div>

    <br>

    <div class="table-wrap">
        <table>
            <thead>
                <tr>
                    <th>变量名</th>
                    <th>值</th>
                    <th>操作</th>
                </tr>
            </thead>
            <tbody id="taskEnvTbody">
                {env_rows}
                <tr id="taskEnvEmptyRow" style="{empty_style}">
                    <td colspan="3">暂无任务变量，请点击“新增任务变量”</td>
                </tr>
            </tbody>
        </table>
    </div>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存</button>
    <a class="btn btn-gray" href="/tasks">返回</a>
</div>
</form>

<script>
function toggleNotifyBox(){{
    var checked = document.querySelector('input[name="notify_mode"]:checked');
    var box = document.getElementById("notifyCustomBox");
    if(!checked || !box) return;
    box.style.display = checked.value === "custom" ? "block" : "none";
}}

function toggleRandomDelayBox(){{
    var checked = document.querySelector('input[name="random_delay_mode"]:checked');
    var box = document.getElementById("randomDelayCustomBox");
    if(!checked || !box) return;
    box.style.display = checked.value === "custom" ? "block" : "none";
}}

function flsEscapeHtml(s){{
    return String(s).replace(/[&<>"']/g, function(c){{
        return {{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c];
    }});
}}

function refreshEmptyRow(){{
    var tbody = document.getElementById("taskEnvTbody");
    var empty = document.getElementById("taskEnvEmptyRow");
    if(!tbody || !empty) return;

    var count = 0;
    tbody.querySelectorAll("tr").forEach(function(row){{
        if(row.id !== "taskEnvEmptyRow") count++;
    }});

    empty.style.display = count > 0 ? "none" : "";
}}

function flsTaskEnvAddRow(key, value){{
    var tbody = document.getElementById("taskEnvTbody");
    if(!tbody) return;

    key = key || "";
    value = value || "";

    var tr = document.createElement("tr");
    tr.innerHTML =
        '<td><input name="env_key" value="' + flsEscapeHtml(key) + '" placeholder="变量名"></td>' +
        '<td><textarea name="env_value" style="min-height:72px;" placeholder="变量值">' + flsEscapeHtml(value) + '</textarea></td>' +
        '<td><button class="btn btn-red" type="button" onclick="flsTaskEnvRemoveRow(this)">删除</button></td>';

    tbody.appendChild(tr);
    refreshEmptyRow();
}}

function flsTaskEnvRemoveRow(btn){{
    var tr = btn ? btn.closest("tr") : null;
    if(tr) tr.remove();
    refreshEmptyRow();
}}

toggleNotifyBox();
toggleRandomDelayBox();
refreshEmptyRow();
</script>
"""
    return body


@bp.route("/tasks")
def tasks_page():
    tasks = load_tasks()
    body = f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">任务管理</div>
            <div class="help">Cron 留空表示手动任务。</div>
        </div>
        <a class="btn btn-primary" href="/task/new">新建任务</a>
    </div>
</div>
<div class="card">
    <div class="table-wrap">{tasks_table(tasks)}</div>
</div>
"""
    return layout("任务管理", "tasks", body)


@bp.route("/task/new", methods=["GET", "POST"])
def task_new():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        command = request.form.get("command", "").strip()
        cron_expr = request.form.get("cron", "").strip()

        if not name:
            return "任务名不能为空", 400
        if not command:
            return "命令不能为空", 400
        if cron_expr:
            try:
                cron_to_trigger(cron_expr)
            except Exception as e:
                return f"Cron 不合法：{e}", 400

        tasks = load_tasks()
        task = {
            "id": uuid.uuid4().hex,
            "name": name,
            "remark": request.form.get("remark", "").strip(),
            "command": command,
            "cron": cron_expr,
            "enabled": request.form.get("enabled") == "1",
            "env": parse_task_env_from_form(),
            "proxy_id": request.form.get("proxy_id", "").strip(),
            "notify": parse_notify_from_form(),
            "random_delay": parse_random_delay_from_form(),
            "run_count": 0,
            "created_at": now_str(),
            "updated_at": now_str(),
        }

        tasks.append(task)
        save_tasks(tasks)
        reload_scheduler()
        return redirect(url_for("tasks.tasks_page"))

    return layout("新建任务", "tasks", task_form())


@bp.route("/task/edit/<task_id>", methods=["GET", "POST"])
def task_edit(task_id):
    tasks = load_tasks()
    task = None

    for t in tasks:
        if t.get("id") == task_id:
            task = t
            break

    if not task:
        abort(404)

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        command = request.form.get("command", "").strip()
        cron_expr = request.form.get("cron", "").strip()

        if not name:
            return "任务名不能为空", 400
        if not command:
            return "命令不能为空", 400
        if cron_expr:
            try:
                cron_to_trigger(cron_expr)
            except Exception as e:
                return f"Cron 不合法：{e}", 400

        task["name"] = name
        task["remark"] = request.form.get("remark", "").strip()
        task["command"] = command
        task["cron"] = cron_expr
        task["enabled"] = request.form.get("enabled") == "1"
        task["env"] = parse_task_env_from_form()
        task["proxy_id"] = request.form.get("proxy_id", "").strip()
        task["notify"] = parse_notify_from_form()
        task["random_delay"] = parse_random_delay_from_form()
        task["updated_at"] = now_str()
        task.setdefault("run_count", 0)

        save_tasks(tasks)
        reload_scheduler()
        return redirect(url_for("tasks.tasks_page"))

    return layout("编辑任务", "tasks", task_form(task))


@bp.route("/task/delete/<task_id>")
def task_delete(task_id):
    stop_task_now(task_id)
    tasks = [t for t in load_tasks() if t.get("id") != task_id]
    save_tasks(tasks)
    reload_scheduler()
    return redirect(url_for("tasks.tasks_page"))


@bp.route("/task/toggle/<task_id>")
def task_toggle(task_id):
    tasks = load_tasks()

    for task in tasks:
        if task.get("id") == task_id:
            task["enabled"] = not task.get("enabled", True)
            task["updated_at"] = now_str()
            break

    save_tasks(tasks)
    reload_scheduler()
    return redirect(url_for("tasks.tasks_page"))


@bp.route("/run/<task_id>")
def run_task_route(task_id):
    ok, msg = run_task_now(task_id, source="manual")

    if not ok:
        return f"{h(msg)}<br><a href='/tasks'>返回</a>", 400

    return redirect(url_for("tasks.log_view", task_id=task_id))


@bp.route("/stop/<task_id>")
def stop_task_route(task_id):
    stop_task_now(task_id)
    return redirect(url_for("tasks.tasks_page"))



@bp.route("/log/<task_id>")
def log_view(task_id):
    task = get_task(task_id)

    if not task:
        abort(404)

    running = is_running(task_id)

    if running:
        log_file = RUNNING.get(task_id, {}).get("log_file", "")
        pid = RUNNING.get(task_id, {}).get("pid", "")
    else:
        log_file = latest_log_for_task(task)
        pid = ""

    body = f"""
<div class="card">
    <div class="card-title">日志：{h(task.get('name') or task.get('command'))}</div>
    <div class="help">
        状态：<b>{"运行中" if running else "已停止"}</b><br>
        PID：{h(pid or "-")}<br>
        日志文件：{h(log_file or "暂无")}
    </div>
    <br>
    <a class="btn btn-primary" href="/run/{h(task_id)}">运行</a>
    <a class="btn btn-red" href="/stop/{h(task_id)}" onclick="return confirm('确定结束该任务吗？')">结束</a>
    <a class="btn btn-gray" href="/tasks">返回</a>
</div>

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

async function loadLog(){{
    try {{
        const beforeScroll = window.scrollY;
        const beforeHeight = document.documentElement.scrollHeight;
        const wasNearBottom = nearBottom();

        const res = await fetch("/api/log/{h(task_id)}?lines=1200", {{cache:"no-store"}});
        const text = await res.text();
        const old = window.__FLS_LOG_LAST_TEXT__ || "";
        const changed = text !== old;

        document.getElementById("log").textContent = text;
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
    }} catch(e) {{
        document.getElementById("log").textContent = "日志读取失败: " + e;
    }}
}}

if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
loadLog();
window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
</script>
"""
    return layout("任务日志", "logs", body)


@bp.route("/api/log/<task_id>")
def api_log(task_id):
    task = get_task(task_id)

    if not task:
        abort(404)

    lines = int(request.args.get("lines", "800"))

    if is_running(task_id):
        log_file = RUNNING.get(task_id, {}).get("log_file", "")
    else:
        log_file = latest_log_for_task(task)

    return Response(tail_file(log_file, lines), mimetype="text/plain; charset=utf-8")
