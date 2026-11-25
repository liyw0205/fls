from ._common import *


def _delete_group_file(file_path, expected_version):
    """Delete one file only when its identity is unchanged, then confirm absence."""
    with LOG_FILES_MUTATION_LOCK:
        try:
            actual_version = log_file_version(file_path)
        except FileNotFoundError:
            return "missing", 0, ""
        except (OSError, ValueError) as exc:
            return "failed", 0, f"{file_path.name}: {exc}"

        if actual_version != expected_version:
            return "conflict", 0, f"{file_path.name}: 文件在删除前已变化"

        try:
            file_path.unlink()
        except FileNotFoundError:
            return "missing", 0, ""
        except Exception as exc:
            try:
                after_version = log_file_version(file_path)
            except FileNotFoundError:
                return "deleted", 1, ""
            except (OSError, ValueError) as check_error:
                return "unknown", 0, f"{file_path.name}: 删除结果无法确认：{exc}；{check_error}"
            if after_version != expected_version:
                return "conflict", 0, f"{file_path.name}: 删除期间文件已变化，未删除新文件"
            return "failed", 0, f"{file_path.name}: {exc}"

        try:
            file_path.lstat()
        except FileNotFoundError:
            return "deleted", 1, ""
        except OSError as exc:
            return "unknown", 0, f"{file_path.name}: 删除结果无法确认：{exc}"

        try:
            after_version = log_file_version(file_path)
        except (OSError, ValueError) as exc:
            return "unknown", 0, f"{file_path.name}: 删除后仍存在且无法确认：{exc}"
        if after_version != expected_version:
            return "conflict", 0, f"{file_path.name}: 删除期间文件已变化，未删除新文件"
        return "failed", 0, f"{file_path.name}: 删除后文件仍存在"


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

    json_format = request.args.get("format") == "json"
    if json_format:
        if lines < 1 or lines > 5000:
            return jsonify({"ok": False, "msg": "lines 必须在 1 到 5000 之间"}), 400
    text = tail_file(str(file_path), lines)
    if json_format:
        response = jsonify({"ok": True, "filename": file_path.name, "lines": lines, "log": text})
        response.headers["Cache-Control"] = "no-store"
        return response
    return Response(text, mimetype="text/plain; charset=utf-8")


@bp.route("/api/logfile/<filename>/download")
def api_logfile_download(filename):
    file_path = safe_log_file(filename)
    try:
        size = file_path.stat().st_size
    except OSError:
        abort(404)
    if size > 8 * 1024 * 1024:
        return jsonify({"ok": False, "msg": "日志文件超过 8 MB 下载限制，请使用日志筛选查看"}), 413
    try:
        raw_content = file_path.read_bytes()
    except OSError:
        abort(404)
    if len(raw_content) > 8 * 1024 * 1024:
        return jsonify({"ok": False, "msg": "日志文件超过 8 MB 下载限制，请使用日志筛选查看"}), 413
    content = mask_sensitive_text(raw_content.decode("utf-8", errors="replace"))
    response = Response(content, mimetype="text/plain; charset=utf-8")
    quoted_filename = quote(file_path.name)
    response.headers["Content-Disposition"] = (
        f"attachment; filename=\"{quoted_filename}\"; filename*=UTF-8''{quoted_filename}"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


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
    raw_groups = data.get("groups") if isinstance(data, dict) and "groups" in data else request.form.getlist("groups")
    raw_versions = data.get("group_versions", {}) if isinstance(data, dict) else {}

    if isinstance(raw_groups, str):
        raw_groups = [raw_groups]
    elif raw_groups is None:
        raw_groups = []
    elif not isinstance(raw_groups, list):
        return jsonify({"ok": False, "msg": "日志分组选择无效，未删除任何文件"}), 400

    group_names = []
    seen = set()

    for raw_name in raw_groups or []:
        if not isinstance(raw_name, str):
            return jsonify({"ok": False, "msg": "日志分组选择无效，未删除任何文件"}), 400
        name = raw_name.strip()

        if not name or name in seen:
            return jsonify({"ok": False, "msg": "日志分组选择无效，未删除任何文件"}), 400

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

        try:
            file_versions = log_group_file_versions(log_files)
            actual = log_group_version_from_files(file_versions)
        except (OSError, ValueError) as exc:
            failed.append(f"{group_name}: {exc}")
            results.append({"group": group_name, "status": "failed", "deleted": 0})
            continue
        expected = group_versions.get(group_name)
        if expected and expected != actual:
            conflicts.append(group_name)
            results.append({"group": group_name, "status": "conflict", "deleted": 0})
            continue

        group_deleted = 0
        group_failed = False
        group_conflict = False
        for file_path in sorted(file_versions, key=lambda item: item.name):
            outcome, count, detail = _delete_group_file(file_path, file_versions[file_path])
            deleted += count
            group_deleted += count
            if outcome == "conflict":
                group_conflict = True
            elif outcome in ("failed", "unknown"):
                group_failed = True
            if detail:
                failed.append(detail)
        if group_conflict:
            conflicts.append(group_name)
        status = "conflict" if group_conflict else "failed" if group_failed else "deleted"
        results.append({"group": group_name, "status": status, "deleted": group_deleted})

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
