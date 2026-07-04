from ._common import *


@bp.route("/logfile/<filename>")
def logfile_view(filename):
    back_url = get_back_url("/logs")
    filename = filename.split("/")[-1]
    file_path = LOG_DIR / filename

    if not file_path.exists():
        abort(404)

    body = f"""
<div class="card">
    <div class="card-title">日志文件：{h(filename)}</div>
    <a class="btn btn-gray" href="{h(back_url)}">返回</a>
    <form class="inline-form" method="post" action="/logfile/delete/{h(filename)}?back={h(back_url)}">
        <button class="btn btn-red" type="submit" onclick="return confirm('确定删除日志吗？')">删除</button>
    </form>
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

        const res = await fetch("/api/logfile/{h(filename)}?lines=1500", {{cache:"no-store"}});
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
    return layout("日志文件", "logs", body)


@bp.route("/api/logfile/<filename>")
def api_logfile(filename):
    filename = filename.split("/")[-1]
    file_path = LOG_DIR / filename

    if not file_path.exists():
        abort(404)

    lines = int(request.args.get("lines", "1500"))

    return Response(
        tail_file(str(file_path), lines),
        mimetype="text/plain; charset=utf-8"
    )


@bp.route("/logfile/delete/<filename>", methods=["POST"])
def logfile_delete(filename):
    back_url = get_back_url("/logs")
    filename = filename.split("/")[-1]
    file_path = LOG_DIR / filename

    if file_path.exists():
        file_path.unlink()

    return redirect(back_url)


@bp.route("/api/logs/groups/delete", methods=["POST"])
def api_log_groups_delete():
    data = request.get_json(silent=True) or {}
    raw_groups = data.get("groups") or request.form.getlist("groups")

    if isinstance(raw_groups, str):
        raw_groups = [raw_groups]

    group_names = []
    seen = set()

    for raw_name in raw_groups or []:
        name = str(raw_name or "").strip()

        if not name or name in seen:
            continue

        seen.add(name)
        group_names.append(name)

    if not group_names:
        return jsonify({"ok": False, "msg": "请选择日志分组"}), 400

    groups = load_log_groups()
    deleted = 0
    missing = []
    failed = []

    for group_name in group_names:
        log_files = groups.get(group_name)

        if not log_files:
            missing.append(group_name)
            continue

        for file_path in log_files:
            try:
                if file_path.exists() and file_path.is_file() and file_path.parent == LOG_DIR:
                    file_path.unlink()
                    deleted += 1
            except Exception as e:
                failed.append(f"{file_path.name}: {e}")

    if failed:
        return jsonify({
            "ok": False,
            "msg": f"已删除 {deleted} 个日志文件，{len(failed)} 个删除失败：{'；'.join(failed[:3])}",
        }), 500

    msg = f"已删除 {deleted} 个日志文件"

    if missing:
        msg += f"，跳过 {len(missing)} 个不存在的分组"

    return jsonify({
        "ok": True,
        "msg": msg,
        "deleted": deleted,
        "groups": group_names,
    })
