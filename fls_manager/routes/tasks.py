import os
import uuid
from pathlib import Path
from math import ceil
from urllib.parse import quote
from flask import Blueprint, request, redirect, url_for, abort, Response

from ..models import load_tasks, save_tasks, get_task
from ..utils import h, now_str, get_back_url
from ..ui.layout import layout
from ..ui.log_controls import log_controls
from ..ui.tables import tasks_table
from ..scheduler import reload_scheduler, cron_to_trigger
from ..task_runner import run_task_now, stop_task_now, is_running
from ..logs import latest_log_for_task, tail_file
from ..state import RUNNING
from ..proxy import proxy_select_options
from ..notify import notify_select_options
from ..paths import SCRIPT_DIR

bp = Blueprint("tasks", __name__)

def task_config_safe_path(rel_path):
    """
    任务配置文件安全路径。

    只允许编辑 scripts 目录下的文件，例如：
      checkbox/config.yml
      kgcheckin/config.json

    不允许：
      /etc/passwd
      ../../xxx
    """
    rel_path = str(rel_path or "").strip().lstrip("/")

    if not rel_path:
        raise ValueError("配置文件路径为空")

    target = (SCRIPT_DIR / rel_path).resolve()
    base = SCRIPT_DIR.resolve()

    if target != base and not str(target).startswith(str(base) + os.sep):
        raise ValueError("配置文件路径非法")

    return target
    
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

    for key, value in env.items():
        value = str(value)

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
            "config_path": "",
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
        <textarea name="command" required style="min-height:160px;" placeholder="task 1.py&#10;&#10;也可以写多行命令，例如：&#10;cd /root/fls/scripts&#10;python3 test.py&#10;echo 完成">{h(task.get('command', ''))}</textarea>
        <div class="help">
            单行运行脚本可以写：<code>task 1.py</code><br>
            如果要写多行命令，请不要以 <code>task</code> 开头，直接写 Shell 命令即可。
        </div>
    </div>

    <br>

    <div class="form-item">
        <label>配置文件路径，可空</label>
        <input name="config_path" value="{h(task.get('config_path', ''))}" placeholder="例如：checkbox/config.yml">
        <div class="help">
            相对于 scripts 目录。填写后任务列表会显示“配置”按钮。<br>
            例如：<code>checkbox/config.yml</code> 对应 <code>{h(str(SCRIPT_DIR / 'checkbox/config.yml'))}</code>
        </div>
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

def tasks_page_links(q, page, pages):
    if pages <= 1:
        return ""

    def build_url(p):
        url = f"/tasks?page={int(p)}"
        if q:
            url += "&q=" + quote(q)
        return url

    def page_btn(p, text=None, active=False, disabled=False):
        text = text if text is not None else str(p)

        if disabled:
            return f'<span class="btn btn-gray" style="opacity:.45;cursor:not-allowed;">{h(text)}</span>'

        cls = "btn-primary" if active else "btn-gray"
        return f'<a class="btn {cls}" href="{h(build_url(p))}">{h(text)}</a>'

    page = max(1, min(int(page), int(pages)))

    items = []

    # 上一页
    items.append(
        page_btn(page - 1, "上一页", disabled=(page <= 1))
    )

    # 页码窗口：
    # 始终显示：1、最后一页、当前页附近 2 页
    show = {1, pages}

    for p in range(page - 2, page + 3):
        if 1 <= p <= pages:
            show.add(p)

    show = sorted(show)

    last = 0

    for p in show:
        if last and p - last > 1:
            items.append('<span class="btn btn-gray" style="opacity:.75;cursor:default;">...</span>')

        items.append(
            page_btn(p, active=(p == page))
        )

        last = p

    # 下一页
    items.append(
        page_btn(page + 1, "下一页", disabled=(page >= pages))
    )

    return f"""
<div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;">
        <div class="help">
            第 <b>{page}</b> / <b>{pages}</b> 页
        </div>
        <div class="action-row">
            {''.join(items)}
        </div>
    </div>
</div>
"""


def filter_tasks_for_page(tasks, q):
    q = str(q or "").strip().lower()

    if not q:
        return tasks

    result = []

    for task in tasks:
        fields = [
            task.get("name", ""),
            task.get("remark", ""),
            task.get("command", ""),
            task.get("cron", ""),
            task.get("config_path", ""),
            task.get("id", ""),
        ]

        text = "\n".join(str(x or "") for x in fields).lower()

        if q in text:
            result.append(task)

    return result

@bp.route("/tasks")
def tasks_page():
    all_tasks = load_tasks()

    q = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", "1") or 1))
    per_page = 20

    filtered_tasks = filter_tasks_for_page(all_tasks, q)

    total = len(filtered_tasks)
    pages = max(1, ceil(total / per_page))
    page = min(page, pages)

    start = (page - 1) * per_page
    end = page * per_page
    show_tasks = filtered_tasks[start:end]

    page_links_html = tasks_page_links(q, page, pages)

    body = f"""
<div class="card">
    <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;">
        <div>
            <div class="card-title">任务管理</div>
            <div class="help">
                Cron 留空表示手动任务。<br>
                共 {len(all_tasks)} 个任务，当前匹配 {total} 个，每页 {per_page} 个。
            </div>
        </div>
        <a class="btn btn-primary" href="/task/new">新建任务</a>
    </div>
</div>

<form method="get">
<div class="card">
    <div class="form-grid">
        <div class="form-item">
            <label>搜索任务</label>
            <input name="q" value="{h(q)}" placeholder="任务名 / 备注 / 命令 / Cron / 配置路径">
        </div>

        <div class="form-item">
            <label>&nbsp;</label>
            <button class="btn btn-primary" type="submit">搜索</button>
            <a class="btn btn-gray" href="/tasks">重置</a>
        </div>
    </div>
</div>
</form>

<div class="card">
    {tasks_table(show_tasks)}
</div>

{page_links_html}
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
            "config_path": request.form.get("config_path", "").strip(),
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
        task["config_path"] = request.form.get("config_path", "").strip()
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
    back_url = get_back_url("/tasks")

    if not ok:
        return f"{h(msg)}<br><a href='{h(back_url)}'>返回</a>", 400

    return redirect(url_for("tasks.log_view", task_id=task_id, back=back_url))


@bp.route("/stop/<task_id>")
def stop_task_route(task_id):
    stop_task_now(task_id)
    return redirect(get_back_url("/tasks"))

@bp.route("/task/config/<task_id>", methods=["GET", "POST"])
def task_config_edit(task_id):
    task = get_task(task_id)

    if not task:
        abort(404)

    back_url = get_back_url("/tasks")
    config_path = str(task.get("config_path") or "").strip()

    if not config_path:
        body = f"""
<div class="card">
    <div class="card-title">任务配置文件</div>
    <div class="help">该任务没有配置 config_path。</div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/task/edit/{h(task_id)}">编辑任务</a>
</div>
"""
        return layout("任务配置文件", "tasks", body)

    try:
        target = task_config_safe_path(config_path)
    except Exception as e:
        body = f"""
<div class="card">
    <div class="card-title">配置文件路径非法</div>
    <div class="help" style="color:#dc2626;">{h(e)}</div>
    <br>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <a class="btn btn-blue" href="/task/edit/{h(task_id)}">编辑任务</a>
</div>
"""
        return layout("配置文件路径非法", "tasks", body), 400

    msg = ""

    if request.method == "POST":
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(request.form.get("content", ""), encoding="utf-8")
            msg = "保存成功"
        except Exception as e:
            msg = f"保存失败：{e}"

    if target.exists():
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            content = f"# 读取失败：{e}\n"
    else:
        content = None

        if not content:
            content = (
                "# 配置文件不存在，可在这里新建。\n"
                "# 示例：\n"
                "# key: value\n"
            )

    body = f"""
<form method="post">
<div class="card">
    <div class="card-title">编辑任务配置：{h(task.get('name') or task_id)}</div>
    <div class="help">
        配置路径：<code>{h(config_path)}</code><br>
        实际路径：<code>{h(target)}</code><br>
        状态：{"已存在" if target.exists() else "文件不存在，保存后会自动创建"}
    </div>
    <br>
    <button class="btn btn-primary" type="submit">保存配置</button>
    <a class="btn btn-blue" href="/run/{h(task_id)}?back={h(back_url)}">运行任务</a>
    <a class="btn btn-orange" href="/task/edit/{h(task_id)}">编辑任务</a>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</div>

{"<div class='card'><div class='help' style='color:#18a058;font-weight:800;'>" + h(msg) + "</div></div>" if msg and msg.startswith("保存成功") else ""}
{"<div class='card'><div class='help' style='color:#dc2626;font-weight:800;'>" + h(msg) + "</div></div>" if msg and not msg.startswith("保存成功") else ""}

<div class="card">
    <div class="card-title">配置内容</div>
    <textarea name="content" style="min-height:680px;">{h(content)}</textarea>
</div>

<div class="card">
    <button class="btn btn-primary" type="submit">保存配置</button>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
</div>
</form>
"""
    return layout("编辑任务配置", "tasks", body)

@bp.route("/log/<task_id>")
def log_view(task_id):
    task = get_task(task_id)

    if not task:
        abort(404)

    back_url = get_back_url("/tasks")

    running = is_running(task_id)

    if running:
        log_file = RUNNING.get(task_id, {}).get("log_file", "")
        pid = RUNNING.get(task_id, {}).get("pid", "")
    else:
        log_file = latest_log_for_task(task)
        pid = ""

    config_btn = ""
    if str(task.get("config_path") or "").strip():
        config_btn = f'<a class="btn btn-blue" href="/task/config/{h(task_id)}?back={h(back_url)}">配置</a>'

    body = f"""
<div class="card">
    <div class="card-title">日志：{h(task.get('name') or task.get('command'))}</div>
    <div class="help">
        状态：<b>{"运行中" if running else "已停止"}</b><br>
        PID：{h(pid or "-")}<br>
        日志文件：{h(log_file or "暂无")}
    </div>
    <br>
    <a class="btn btn-primary" href="/run/{h(task_id)}?back={h(back_url)}">运行</a>
    <a class="btn btn-red" href="/stop/{h(task_id)}?back={h(back_url)}" onclick="return confirm('确定结束该任务吗？')">结束</a>
    {config_btn}
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
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
