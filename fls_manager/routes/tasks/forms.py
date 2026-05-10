from flask import request

from ...utils import h
from ...paths import SCRIPT_DIR
from ...proxy import proxy_select_options
from ...notify import notify_select_options
from .helpers import collection_select_options


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
            return {
                "mode": mode,
                "ids": ids,
            }

    old_ids = task.get("notify_ids")

    if isinstance(old_ids, list) and old_ids:
        if "__none__" in old_ids:
            return {
                "mode": "none",
                "ids": [],
            }

        if "__default__" in old_ids:
            return {
                "mode": "default",
                "ids": [],
            }

        return {
            "mode": "custom",
            "ids": old_ids,
        }

    return {
        "mode": "default",
        "ids": [],
    }


def parse_notify_from_form():
    mode = request.form.get("notify_mode", "default").strip()

    if mode == "default":
        return {
            "mode": "default",
            "ids": [],
        }

    if mode == "custom":
        ids = [x for x in request.form.getlist("notify_ids") if x]

        if not ids:
            return {
                "mode": "none",
                "ids": [],
            }

        return {
            "mode": "custom",
            "ids": ids,
        }

    return {
        "mode": "none",
        "ids": [],
    }


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
            "collection_id": "",
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
        <label>所属合集</label>
        <select name="collection_id">{collection_select_options(task.get("collection_id", ""))}</select>
        <div class="help">
            放入合集的任务不会在普通任务列表显示，但仍会正常运行。
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