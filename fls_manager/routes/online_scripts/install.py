from ._common import *


@bp.route("/online-scripts/install/<script_id>", methods=["POST"])
def online_scripts_install(script_id):
    item = get_online_script(script_id)

    if not item:
        abort(404)

    for install_id, info in ONLINE_INSTALL_RUNNING.items():
        if info.get("script_id") == script_id and info.get("running"):
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

    selected_task_indexes = selected_task_indexes_from_form(item)
    select_mode = request.form.get("select_mode", "").strip()
    excluded_task_indexes = request.form.get("excluded_task_indexes", "").strip()

    if not import_task:
        enable_task = False
        selected_task_indexes = []

    if import_task and script_has_task(item) and not selected_task_indexes:
        return redirect(
            url_for(
                "online_scripts.online_scripts_page",
                err="已勾选导入任务，但没有选择任何任务",
            )
        )

    try:
        target = online_script_target(item)
    except Exception as e:
        return layout("在线脚本安装失败", "online_scripts", f"""
<div class="card">
    <div class="card-title">目标路径非法</div>
    <div class="help">{h(e)}</div>
    <br>
    <a class="btn btn-gray" href="/online-scripts">返回</a>
</div>
"""), 400

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

    if target.exists() and not force:
        proxy_options = proxy_select_options(proxy_id)
        has_task = script_has_task(item)

        body = f"""
<div class="card">
    <div class="card-title">目标已存在，请确认</div>
    <div class="help" style="color:#dc2626;">
        检测到同名文件或文件夹已经存在。为避免意外覆盖，已暂停操作。<br>
        目标路径：<b>{h(target)}</b>
    </div>
    <br>
    <div class="help">
        如果是 Git 仓库目录，继续后会执行 <code>git pull</code> 更新。<br>
        如果是 raw 文件，继续后会覆盖该文件。<br>
        如果目标是非 Git 文件夹，继续也不会强行覆盖，需要你手动处理。
    </div>
</div>

<div class="card">
    <form method="post" action="/online-scripts/install/{h(script_id)}">
        <input type="hidden" name="force" value="1">
        {install_option_hidden}
        {select_mode_hidden}
        {selected_task_hidden}

        <div class="form-item">
            <label>代理</label>
            <select name="proxy_id">{proxy_options}</select>
        </div>

        <br>

        <div class="help">
            导入任务：{"是" if import_task else "否"}<br>
            导入后启用：{"是" if enable_task else "否"}<br>
            已选择任务数：{len(selected_task_indexes) if import_task and has_task else 0}
        </div>

        <br>

        <button class="btn btn-orange" type="submit" onclick="return confirm('确定继续吗？可能会覆盖文件或更新仓库。')">确认继续</button>
        <a class="btn btn-gray" href="/online-scripts">取消</a>
    </form>
</div>
"""
        return layout("目标已存在", "online_scripts", body)

    install_id = uuid.uuid4().hex
    log_file = online_install_log_file(
        install_id,
        item.get("name") or item.get("id"),
    )

    ONLINE_INSTALL_RUNNING[install_id] = {
        "id": install_id,
        "script_id": script_id,
        "script_name": item.get("name"),
        "log_file": str(log_file),
        "running": True,
        "status": "准备中",
        "start_time": time.time(),
        "returncode": None,
        "error": "",
        "process": None,
    }

    start_install_thread(
        install_id=install_id,
        item=item,
        proxy_id=proxy_id,
        import_task=import_task,
        force=force,
        enable_task=enable_task,
        selected_task_indexes=selected_task_indexes,
    )

    return redirect(
        url_for(
            "online_scripts.online_install_log",
            install_id=install_id,
            back="/online-scripts",
        )
    )


@bp.route("/online-scripts/install-stop/<install_id>", methods=["POST"])
def online_scripts_install_stop(install_id):
    ok, msg = request_stop_online_install(install_id)

    return redirect(
        url_for(
            "online_scripts.online_scripts_page",
            msg=msg if ok else "",
            err="" if ok else msg,
        )
    )

