from ._common import *
from ...utils import now_str
from ...online_scripts.constants import (
    ONLINE_INSTALL_LOCK,
    ONLINE_INSTALL_RUNNING,
    ONLINE_INSTALL_RETRYING,
    active_online_install_for_script,
    get_online_install_job,
    persist_online_install,
)


def _start_install_job(
    script_id,
    item,
    proxy_id="",
    import_task=False,
    force=False,
    enable_task=False,
    selected_task_indexes=None,
    retry_of="",
):
    """Register and launch one install, keeping startup failures durable."""
    if retry_of:
        target = online_script_target(item)
        if target.exists() and not force:
            return _install_confirmation(
                script_id, item, target, proxy_id, import_task, enable_task,
                selected_task_indexes or [],
                action=url_for("online_scripts.online_scripts_install_retry", install_id=retry_of),
            )

    install_id = uuid.uuid4().hex
    log_file = online_install_log_file(
        install_id,
        item.get("name") or item.get("id"),
    )
    selected_task_indexes = list(selected_task_indexes or [])
    task_crons = online_script_task_crons(item)

    with ONLINE_INSTALL_LOCK:
        if retry_of:
            previous = get_online_install_job(retry_of)
            if previous and previous.get("retried_by"):
                return _install_retry_consumed(retry_of, previous["retried_by"])
        active_id, _ = active_online_install_for_script(script_id)
        if active_id:
            if retry_of:
                return _install_retry_error(
                    retry_of, "无法重试在线安装", "该脚本仍有安装任务正在运行，请先查看当前任务。", 409,
                    actions_html=f'<a class="btn btn-orange" href="/online-scripts/log/{h(active_id)}">查看当前安装</a>',
                )
            return redirect(
                url_for(
                    "online_scripts.online_scripts_page",
                    msg="该脚本正在安装中",
                )
            )
        prune_completed_records(ONLINE_INSTALL_RUNNING)
        info = {
            "id": install_id,
            "script_id": script_id,
            "script_name": item.get("name"),
            "log_file": str(log_file),
            "proxy_id": proxy_id,
            "import_task": bool(import_task),
            "enable_task": bool(enable_task),
            "selected_task_indexes": selected_task_indexes,
            "selected_tasks": [
                task_crons[int(index) - 1]
                for index in selected_task_indexes
            ] if import_task else [],
            "force": bool(force),
            "retry_of": retry_of,
            "running": True,
            "status": "准备中",
            "start_time": time.time(),
            "returncode": None,
            "error": "",
            "process": None,
            "updated_at": now_str(),
        }
        ONLINE_INSTALL_RUNNING[install_id] = info
        try:
            persist_online_install(info)
        except Exception as e:
            info.update(
                running=False,
                status="启动失败",
                error=f"保存安装状态失败：{e}",
                updated_at=now_str(),
            )
            return _install_start_error(install_id, info["error"])

    try:
        start_install_thread(
            install_id=install_id,
            item=item,
            proxy_id=proxy_id,
            import_task=import_task,
            force=force,
            enable_task=enable_task,
            selected_task_indexes=selected_task_indexes,
        )
    except Exception as e:
        with ONLINE_INSTALL_LOCK:
            info = ONLINE_INSTALL_RUNNING[install_id]
            info.update(
                running=False,
                status="启动失败",
                error=f"启动安装线程失败：{e}",
                updated_at=now_str(),
            )
            try:
                persist_online_install(info)
            except Exception as storage_error:
                info["error"] += f"；保存安装状态失败：{storage_error}"
        return _install_start_error(install_id, info["error"])

    return redirect(
        url_for(
            "online_scripts.online_install_log",
            install_id=install_id,
            back="/online-scripts",
        )
    )


@bp.route("/online-scripts/install/<script_id>", methods=["POST"])
def online_scripts_install(script_id):
    item = get_online_script(script_id)

    if not item:
        abort(404)

    with ONLINE_INSTALL_LOCK:
        active_id, _ = active_online_install_for_script(script_id)
        if active_id:
            return redirect(
                url_for(
                    "online_scripts.online_scripts_page",
                    msg="该脚本正在安装中",
                )
            )

    proxy_id = request.form.get("proxy_id", "").strip()
    import_task = request.form.get("import_task") == "1"
    enable_task = request.form.get("enable_task") == "1"
    force = request.form.get("force") == "1"

    selected_task_indexes = []
    select_mode = request.form.get("select_mode", "").strip()
    excluded_task_indexes = request.form.get("excluded_task_indexes", "").strip()

    if not import_task:
        enable_task = False
        selected_task_indexes = []
    else:
        try:
            selected_task_indexes = selected_task_indexes_from_form(item)
            selected_task_indexes = validated_install_task_indexes(item, selected_task_indexes)
        except ValueError as e:
            body = page_header_card(
                "任务选择无效", help_html=f"{h(e)}。请重新选择任务后重试。",
                actions_html=f'<a class="btn btn-gray" href="/online-scripts/install-select/{h(script_id)}">重新选择任务</a>',
            )
            return layout("在线脚本安装失败", "online_scripts", body), 400

    try:
        target = online_script_target(item)
    except Exception as e:
        body = page_header_card(
            "目标路径非法",
            help_html=f"{h(e)}。下一步：检查脚本保存路径后重试",
            actions_html='<a class="btn btn-gray" href="/online-scripts">返回</a>',
        )
        return layout("在线脚本安装失败", "online_scripts", body), 400

    if target.exists() and not force:
        return _install_confirmation(
            script_id, item, target, proxy_id, import_task, enable_task, selected_task_indexes,
            select_mode=select_mode, excluded_task_indexes=excluded_task_indexes,
        )

    return _start_install_job(
        script_id=script_id,
        item=item,
        proxy_id=proxy_id,
        import_task=import_task,
        force=force,
        enable_task=enable_task,
        selected_task_indexes=selected_task_indexes,
    )


def _install_confirmation(
    script_id, item, target, proxy_id, import_task, enable_task, selected_task_indexes,
    select_mode="", excluded_task_indexes="", action="",
):
    selected_task_hidden = ""

    for idx in selected_task_indexes:
        try:
            n = int(idx)
            if n > 0:
                selected_task_hidden += f'<input type="hidden" name="task_indexes" value="{h(n)}">'
        except Exception:
            pass

    install_option_hidden = ""

    if import_task:
        install_option_hidden += '<input type="hidden" name="import_task" value="1">'

    if enable_task:
        install_option_hidden += '<input type="hidden" name="enable_task" value="1">'

    select_mode_hidden = ""

    if select_mode:
        select_mode_hidden += f'<input type="hidden" name="select_mode" value="{h(select_mode)}">'

    if excluded_task_indexes:
        select_mode_hidden += f'<input type="hidden" name="excluded_task_indexes" value="{h(excluded_task_indexes)}">'

    proxy_options = proxy_select_options(proxy_id)
    has_task = script_has_task(item)
    action = action or url_for("online_scripts.online_scripts_install", script_id=script_id)

    header_card = page_header_card(
        "目标已存在，请确认",
        help_html=f"""
        <span style="color:#dc2626;">检测到同名文件或文件夹已经存在。为避免意外覆盖，已暂停操作。</span><br>
        目标路径：<b>{h(target)}</b><br>
        如果是 Git 仓库目录，继续后会执行 <code>git pull</code> 更新。<br>
        如果是 raw 文件，继续后会覆盖该文件。<br>
        如果目标是非 Git 文件夹，继续也不会强行覆盖，需要你手动处理。
""",
    )

    body = f"""
{header_card}
<form method="post" action="{h(action)}">
<section class="section fls-form-section">
    <h2 class="section-title">覆盖确认</h2>
        <input type="hidden" name="force" value="1">
        {install_option_hidden}
        {select_mode_hidden}
        {selected_task_hidden}

        <div class="form-item">
            <label>代理</label>
            <select name="proxy_id" aria-label="覆盖安装使用的代理">{proxy_options}</select>
        </div>

        <br>

        <div class="help">
            导入任务：{"是" if import_task else "否"}<br>
            导入后启用：{"是" if enable_task else "否"}<br>
            已选择任务数：{len(selected_task_indexes) if import_task and has_task else 0}
        </div>

        <br>

        <div class="danger-confirm">
            <div class="danger-confirm-description">继续操作可能覆盖现有文件或更新 Git 仓库，请确认目标路径和导入选项。</div>
            <div class="row-actions">
                <div class="row-actions-danger">
                    <button class="btn btn-orange" type="submit" onclick="return confirm('确定继续吗？可能会覆盖文件或更新仓库。')">确认覆盖并继续</button>
                </div>
                <div class="row-actions-secondary">
                    <a class="btn btn-gray" href="/online-scripts">取消</a>
                </div>
            </div>
        </div>
</section>
</form>
"""
    return layout("目标已存在", "online_scripts", body)


def _install_start_error(install_id, error):
    body = page_header_card(
        "在线脚本安装启动失败",
        help_html=h(error),
        actions_html=f'<a class="btn btn-gray" href="/online-scripts/log/{h(install_id)}">查看安装记录</a>',
    )
    return layout("在线脚本安装失败", "online_scripts", body), 500


def _install_retry_error(install_id, title, message, code, actions_html=""):
    body = page_header_card(
        title,
        help_html=h(message),
        actions_html=(
            f'<a class="btn btn-gray" href="/online-scripts/log/{h(install_id)}">查看安装记录</a>'
            '<a class="btn btn-blue" href="/online-scripts">返回在线脚本</a>'
            + actions_html
        ),
    )
    return layout(title, "online_scripts", body), code


def _install_retry_consumed(install_id, retry_id):
    return _install_retry_error(
        install_id, "无法重试在线安装", "该记录已发起重试，请查看新的安装记录。", 409,
        actions_html=f'<a class="btn btn-orange" href="/online-scripts/log/{h(retry_id)}">查看重试记录</a>',
    )


@bp.route("/online-scripts/install-retry/<install_id>", methods=["POST"])
def online_scripts_install_retry(install_id):
    info = get_online_install_job(install_id)
    if not info:
        return _install_retry_error(
            install_id,
            "无法重试在线安装",
            "安装记录不存在。请确认安装 ID 后重试。",
            404,
        )

    if info.get("running"):
        return _install_retry_error(
            install_id,
            "无法重试在线安装",
            "该安装任务仍在运行，请等待完成或先停止任务。",
            409,
        )

    if info.get("status") not in ("结果未知", "失败", "超时", "已停止", "启动失败"):
        return _install_retry_error(
            install_id,
            "无法重试在线安装",
            f"当前状态为 {info.get('status') or '未知'}，仅失败或结果未知的安装允许重试。",
            409,
        )

    with ONLINE_INSTALL_LOCK:
        info = get_online_install_job(install_id)
        if not info:
            return _install_retry_error(install_id, "无法重试在线安装", "安装记录不存在。", 404)
        if info.get("retried_by"):
            return _install_retry_consumed(install_id, info["retried_by"])
        active_id, _active = active_online_install_for_script(info.get("script_id"))
        if active_id:
            return _install_retry_error(
                install_id, "无法重试在线安装", "该脚本仍有安装任务正在运行，请先查看当前任务。", 409,
                actions_html=f'<a class="btn btn-orange" href="/online-scripts/log/{h(active_id)}">查看当前安装</a>',
            )
        if install_id in ONLINE_INSTALL_RETRYING:
            return _install_retry_error(
                install_id,
                "无法重试在线安装",
                "该安装任务正在提交重试，请勿重复提交。",
                409,
            )
        ONLINE_INSTALL_RETRYING.add(install_id)

    try:
        script_id = str(info.get("script_id") or "").strip()
        item = get_online_script(script_id)
        if not item:
            return _install_retry_error(
                install_id,
                "无法重试在线安装",
                "脚本已不在本地缓存中，请先同步在线脚本源，再从脚本列表重试。",
                409,
            )

        try:
            online_script_target(item)
        except Exception as e:
            return _install_retry_error(
                install_id,
                "无法重试在线安装",
                f"脚本目标路径非法：{e}。请检查脚本源数据后重试。",
                400,
            )

        import_task = bool(info.get("import_task"))
        enable_task = bool(info.get("enable_task")) and import_task
        try:
            selected_task_indexes = (
                validated_install_retry_task_indexes(item, info)
                if import_task else []
            )
        except (TypeError, ValueError):
            return _install_retry_error(
                install_id,
                "无法重试在线安装",
                "原安装记录的任务选择已失效，请重新选择任务后重试。",
                409,
                actions_html=f'<a class="btn btn-primary" href="/online-scripts/install-select/{h(script_id)}">重新选择任务</a>',
            )

        # Only a new confirmation POST may authorize overwriting a partial target.
        return _start_install_job(
            script_id=script_id,
            item=item,
            proxy_id=request.form.get("proxy_id", str(info.get("proxy_id") or "")).strip(),
            import_task=import_task,
            force=request.form.get("force") == "1",
            enable_task=enable_task,
            selected_task_indexes=selected_task_indexes,
            retry_of=install_id,
        )
    finally:
        with ONLINE_INSTALL_LOCK:
            ONLINE_INSTALL_RETRYING.discard(install_id)


def _stop_install_result(install_id):
    try:
        ok, msg = request_stop_online_install(install_id)
        code = 200 if ok else (404 if get_online_install_job(install_id) is None else 409)
        return ok, msg, code
    except Exception as e:
        return False, f"停止请求失败：{e}", 500


@bp.route("/online-scripts/install-stop/<install_id>", methods=["POST"])
def online_scripts_install_stop(install_id):
    ok, msg, code = _stop_install_result(install_id)
    if not ok:
        body = page_header_card(
            "无法停止在线安装",
            help_html=h(msg),
            actions_html=f'<a class="btn btn-gray" href="/online-scripts/log/{h(install_id)}">查看安装记录</a>',
        )
        return layout("无法停止在线安装", "online_scripts", body), code

    return redirect(
        url_for(
            "online_scripts.online_scripts_page",
            msg=msg if ok else "",
            err="" if ok else msg,
        )
    )


@bp.route("/api/online-scripts/install-stop/<install_id>", methods=["POST"])
def api_online_scripts_install_stop(install_id):
    ok, msg, code = _stop_install_result(install_id)
    return jsonify({"ok": ok, "msg": msg}), code
