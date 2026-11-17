from ._common import *


@bp.route("/logfile/<filename>")
def logfile_view(filename):
    back_url = get_back_url("/logs")
    if (not isinstance(filename, str) or filename != filename.strip()
        or "/" in filename or "\\" in filename or filename in ("", ".", "..")):
        abort(404)
    file_path = safe_log_file(filename)
    try:
        version = log_file_version(file_path)
    except (OSError, ValueError):
        abort(404)

    body = f"""
<div class="card">
    <div class="card-title">日志文件：{h(filename)}</div>
    <div class="row-actions" aria-label="日志文件操作">
        <div class="row-actions-primary">
            <a class="btn btn-gray" href="{h(back_url)}">返回日志列表</a>
        </div>
        <div class="row-actions-danger">
            <form class="inline-form" method="post" action="/logfile/delete/{h(filename)}?back={h(back_url)}">
                <input type="hidden" name="version" value="{h(version)}">
                <button class="btn btn-red" type="submit" onclick="return confirm('确定删除日志吗？')">删除日志</button>
            </form>
        </div>
    </div>
</div>

<pre class="log" id="log">加载中...</pre>
<section class="section" id="logRecovery" role="alert" hidden></section>
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
        const text = await flsReadTextResponse(res);
        flsLogPollRecovered(loadLog);
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
        flsLogPollFailed(e, loadLog, "日志文件");
    }}
}}

if(window.__FLS_ACTIVE_LOG_INTERVAL__) clearInterval(window.__FLS_ACTIVE_LOG_INTERVAL__);
loadLog();
if(!window.__FLS_ACTIVE_LOG_INTERVAL__) window.__FLS_ACTIVE_LOG_INTERVAL__ = setInterval(loadLog, 2000);
</script>
"""
    return layout("日志文件", "logs", body)


@bp.route("/api/logfile/<filename>")
def api_logfile(filename):
    file_path = safe_log_file(filename)

    try:
        lines = int(request.args.get("lines", "1500"))
    except (TypeError, ValueError):
        return Response(
            "lines 必须为整数",
            status=400,
            content_type="text/plain; charset=utf-8",
        )

    return Response(
        tail_file(str(file_path), lines),
        mimetype="text/plain; charset=utf-8"
    )


@bp.route("/logfile/delete/<filename>", methods=["POST"])
def logfile_delete(filename):
    back_url = get_back_url("/logs")
    version = request.form.get("version", "")

    def failure(message, status):
        body = f'''<section class="section" role="alert"><p>{h(message)}</p>
<form method="post" action="/logfile/delete/{h(filename)}?back={h(back_url)}">
<input type="hidden" name="version" value="{h(version)}">
<button class="btn btn-primary" type="submit">查询或重试删除</button></form>
<a class="btn btn-gray" href="/logfile/{h(filename)}?back={h(back_url)}">返回日志文件</a>
<a class="btn btn-gray" href="{h(back_url)}">返回日志列表</a></section>'''
        return layout("日志删除恢复", "logs", body), status

    if (not isinstance(filename, str) or filename != filename.strip()
        or "/" in filename or "\\" in filename or filename in ("", ".", "..")):
        return failure("日志文件选择无效，未删除任何文件。请从日志列表重新选择。", 400)
    if not isinstance(version, str) or len(version) != 64 or any(c not in "0123456789abcdef" for c in version):
        return failure("日志文件版本无效，未删除任何文件。请刷新日志页面后重试。", 400)

    file_path = LOG_DIR / filename
    try:
        actual = log_file_version(file_path)
    except FileNotFoundError:
        return redirect(back_url)
    except (OSError, ValueError):
        return failure("日志文件不是可安全删除的常规文件，未删除任何文件。", 409)
    if actual != version:
        return failure("日志文件已变化，未删除同名新文件。请刷新列表后重新选择。", 409)

    try:
        file_path.unlink()
    except FileNotFoundError:
        return redirect(back_url)
    except Exception as exc:
        try:
            if not file_path.exists():
                return redirect(back_url)
        except Exception:
            return failure(f"删除结果无法确认：{exc}", 503)
        return failure(f"日志删除失败：{exc}。文件仍在，请检查后重试。", 500)

    try:
        if file_path.exists():
            return failure("删除结果无法确认，日志文件仍存在。请查询后重试。", 503)
    except Exception as exc:
        return failure(f"删除结果无法确认：{exc}", 503)
    return redirect(back_url)


@bp.route("/api/logs/groups/delete", methods=["POST"])
def api_log_groups_delete():
    data = request.get_json(silent=True) or {}
    raw_groups = data.get("groups") or request.form.getlist("groups")
    raw_versions = data.get("group_versions", {})

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
    if not isinstance(raw_versions, dict):
        return jsonify({"ok": False, "msg": "日志分组版本无效，未删除任何文件"}), 400
    group_versions = {}
    for group_name, version in raw_versions.items():
        if (not isinstance(group_name, str) or not isinstance(version, str)
            or len(version) != 64 or any(c not in "0123456789abcdef" for c in version)):
            return jsonify({"ok": False, "msg": "日志分组版本无效，未删除任何文件"}), 400
        group_versions[group_name] = version

    groups = load_log_groups()
    deleted = 0
    missing = []
    failed = []
    conflicts = []
    results = []

    for group_name in group_names:
        log_files = groups.get(group_name)

        if not log_files:
            missing.append(group_name)
            results.append({"group": group_name, "status": "missing", "deleted": 0})
            continue

        expected = group_versions.get(group_name)
        try:
            actual = log_group_version(log_files)
        except (OSError, ValueError) as exc:
            failed.append(f"{group_name}: {exc}")
            results.append({"group": group_name, "status": "failed", "deleted": 0})
            continue
        if expected and expected != actual:
            conflicts.append(group_name)
            results.append({"group": group_name, "status": "conflict", "deleted": 0})
            continue

        group_deleted = 0
        group_failed = False
        for file_path in log_files:
            try:
                if file_path.exists() and file_path.is_file() and file_path.parent == LOG_DIR:
                    file_path.unlink()
                    deleted += 1
                    group_deleted += 1
            except Exception as e:
                failed.append(f"{file_path.name}: {e}")
                group_failed = True
        results.append({"group": group_name, "status": "failed" if group_failed else "deleted", "deleted": group_deleted})

    retry_groups = conflicts + [row["group"] for row in results if row["status"] == "failed"]
    if failed or conflicts:
        return jsonify({
            "ok": False, "partial": bool(deleted), "retry_groups": retry_groups, "results": results,
            "msg": f"已删除 {deleted} 个日志文件，{len(conflicts)} 个分组已变化，{len(failed)} 个删除失败，请核对后重试",
        }), 409 if conflicts else 500

    msg = f"已删除 {deleted} 个日志文件"

    if missing:
        msg += f"，跳过 {len(missing)} 个不存在的分组"

    return jsonify({
        "ok": True,
        "msg": msg,
        "deleted": deleted,
        "groups": group_names,
        "results": results,
    })
