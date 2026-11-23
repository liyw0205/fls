from ._common import *
import hashlib
import json
import re

from ...utils import now_str
from ...models import TASKS_MUTATION_LOCK, TaskSchedulerReloadError
from ...storage import storage_transaction
from ...scheduler import reload_scheduler
from ...online_scripts.constants import (
    ONLINE_INSTALL_LOCK,
    ONLINE_INSTALL_RUNNING,
    ONLINE_INSTALL_RETRYING,
    active_online_install_for_script,
    get_online_install_job,
    InstallHistoryLimitError,
    online_install_retry_relation,
    persist_online_install,
    persist_online_install_scheduler_reload,
    persist_online_install_task_recovery,
    _install_snapshot,
    _load_persisted_online_install,
)
from ...online_scripts.install import capture_install_outputs, _persist_install_state
from ...online_scripts.tasks import task_import_file_signature, task_import_publication_state


def _install_request_id(raw):
    value = str(raw or "").strip() or uuid.uuid4().hex
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("在线脚本安装请求标识无效，请刷新选择页后重试")
    return value


def _install_request_matches(info, script_id, proxy_id, import_task, enable_task, force, selected_task_indexes):
    return (
        info.get("script_id") == script_id
        and str(info.get("proxy_id") or "") == proxy_id
        and bool(info.get("import_task")) == bool(import_task)
        and bool(info.get("enable_task")) == bool(enable_task)
        and bool(info.get("force")) == bool(force)
        and list(info.get("selected_task_indexes") or []) == list(selected_task_indexes or [])
    )


def _online_install_script_fingerprint(item):
    payload = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _install_start_receipt_retryable(info):
    return bool(
        isinstance(info, dict)
        and not info.get("running")
        and info.get("status") == "启动失败"
        and info.get("phase") == "startup_receipt_failed"
        and not info.get("retry_of")
        and re.fullmatch(r"[a-f0-9]{64}", str(info.get("script_fingerprint") or ""))
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
    request_id="",
):
    """Register and launch one install, keeping startup failures durable."""
    try:
        validate_proxy_id(proxy_id)
    except ValueError as e:
        return _install_retry_error(
            retry_of or script_id, "代理选择无效", str(e), 400,
            actions_html=f'<a class="btn btn-orange" href="/online-scripts/install-select/{h(script_id)}">重新选择安装选项</a>',
        )
    except Exception as e:
        return _install_retry_error(
            retry_of or script_id, "代理配置无法读取", f"读取代理配置失败：{e}", 500,
            actions_html=f'<a class="btn btn-orange" href="/online-scripts/install-select/{h(script_id)}">重新选择安装选项</a>',
        )
    if retry_of:
        target = online_script_target(item)
        if target.exists() and not force:
            return _install_confirmation(
                script_id, item, target, proxy_id, import_task, enable_task,
                selected_task_indexes or [],
                action=url_for(
                    "online_scripts.online_scripts_install_retry", install_id=retry_of,
                    back=get_back_url("/online-scripts"),
                ),
            )

    install_id = request_id if request_id and not retry_of else uuid.uuid4().hex
    log_file = online_install_log_file(
        install_id,
        item.get("name") or item.get("id"),
    )
    selected_task_indexes = list(selected_task_indexes or [])
    task_crons = online_script_task_crons(item)
    try:
        script_fingerprint = _online_install_script_fingerprint(item)
    except (TypeError, ValueError) as exc:
        return _install_retry_error(
            retry_of or request_id or script_id,
            "在线脚本无法安全启动",
            f"脚本内容无法核验：{exc}",
            400,
        )

    with ONLINE_INSTALL_LOCK:
        if not retry_of and request_id:
            try:
                previous = get_online_install_job(request_id)
            except Exception as exc:
                return _install_retry_error(
                    request_id, "无法查询原在线安装", f"安装记录无法读取：{exc}", 500,
                )
            if previous:
                if not _install_request_matches(
                    previous, script_id, proxy_id, import_task, enable_task, force, selected_task_indexes,
                ):
                    return _install_retry_error(
                        request_id, "在线安装请求冲突", "该请求标识已绑定其他安装选项，请重新打开任务选择页。", 409,
                    )
                if not _install_start_receipt_retryable(previous):
                    return redirect(url_for("online_scripts.online_install_log", install_id=request_id, back="/online-scripts"))
                if previous.get("script_fingerprint") != script_fingerprint:
                    return _install_retry_error(
                        request_id,
                        "原在线安装内容已变化",
                        "脚本源内容已变化，未沿原请求启动新内容；请重新选择安装选项。",
                        409,
                        actions_html=f'<a class="btn btn-orange" href="/online-scripts/install-select/{h(script_id)}">重新选择安装选项</a>',
                    )
        if retry_of:
            try:
                previous = get_online_install_job(retry_of)
            except Exception as exc:
                return _install_retry_error(
                    retry_of,
                    "无法重试在线安装",
                    f"安装记录无法读取，未覆盖原文件：{exc}。修复记录文件后重试。",
                    500,
                )
            relation = online_install_retry_relation(previous) if previous else None
            if relation and relation.get("retry_relation_error"):
                return _install_retry_error(
                    retry_of, "无法重试在线安装", relation["retry_relation_error"], 409,
                )
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
            "script_fingerprint": script_fingerprint,
            "force": bool(force),
            "retry_of": retry_of,
            "running": True,
            "status": "准备中",
            "phase": "prepared",
            "start_time": time.time(),
            "returncode": None,
            "error": "",
            "scheduler_reload_pending": False,
            "scheduler_reload_error": "",
            "process": None,
            "updated_at": now_str(),
        }
        ONLINE_INSTALL_RUNNING[install_id] = info
        try:
            persist_online_install(info)
        except InstallHistoryLimitError as e:
            ONLINE_INSTALL_RUNNING.pop(install_id, None)
            if request.path.startswith("/api/"):
                return jsonify(
                    ok=False,
                    request_id=request_id or retry_of or install_id,
                    msg=str(e),
                    retry_allowed=False,
                    history_url=url_for("online_scripts.online_install_history"),
                ), 409
            actions = (
                f'<a class="btn btn-blue" href="/online-scripts/log/{h(retry_of)}">查看原安装</a>'
                if retry_of else
                f'<a class="btn btn-blue" href="/online-scripts/install-select/{h(script_id)}">重新选择安装选项</a>'
            )
            body = page_header_card(
                "在线脚本安装历史容量已满",
                help_html=f"{h(e)} 已有回执未修改，未启动下载安装。",
                actions_html=(
                    '<a class="btn btn-gray" href="/online-scripts/install-history">查看安装历史</a>'
                    + actions
                ),
            )
            return layout("在线脚本安装未启动", "online_scripts", body), 409
        except Exception as e:
            try:
                persisted = _load_persisted_online_install(install_id)
                published = bool(persisted) and _install_snapshot(persisted) == _install_snapshot(info)
            except Exception:
                published = False
            if not published:
                info.update(
                    running=False,
                    status="启动失败",
                    phase="startup_receipt_failed",
                    error=(
                        f"保存安装状态失败（启动回执）：{e}。安装进程未启动；修复存储后可沿原请求重试。"
                    ),
                    updated_at=now_str(),
                )
                failure_receipt_published = True
                try:
                    persist_online_install(info)
                except Exception as storage_error:
                    try:
                        persisted_failure = _load_persisted_online_install(install_id)
                        failure_receipt_published = (
                            bool(persisted_failure)
                            and _install_snapshot(persisted_failure) == _install_snapshot(info)
                        )
                    except Exception:
                        failure_receipt_published = False
                    if not failure_receipt_published:
                        info["_startup_receipt_confirmed"] = False
                        info["error"] += f"；启动失败回执未能确认保存：{storage_error}"
                if not failure_receipt_published:
                    return _install_start_error(
                        install_id,
                        info["error"],
                        log_available=False,
                        script_id=script_id,
                    )
                return _install_start_error(
                    install_id,
                    info["error"],
                    recovery_action=url_for("online_scripts.online_scripts_install_start_retry", install_id=install_id),
                )

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
                phase="startup_failed",
                error=f"启动安装线程失败：{e}",
                updated_at=now_str(),
            )
            try:
                persist_online_install(info)
            except Exception as storage_error:
                try:
                    persisted_failure = _load_persisted_online_install(install_id)
                    failure_receipt_published = (
                        bool(persisted_failure)
                        and _install_snapshot(persisted_failure) == _install_snapshot(info)
                    )
                except Exception:
                    failure_receipt_published = False
                if not failure_receipt_published:
                    info.update(
                        phase="startup_receipt_failed",
                        error=(
                            f"启动安装线程失败：{e}；保存启动失败状态失败：{storage_error}。"
                            "安装进程未启动，修复存储后可沿原请求重试。"
                        ),
                        updated_at=now_str(),
                    )
                    try:
                        persist_online_install(info)
                    except Exception as receipt_error:
                        try:
                            persisted_failure = _load_persisted_online_install(install_id)
                            failure_receipt_published = (
                                bool(persisted_failure)
                                and _install_snapshot(persisted_failure) == _install_snapshot(info)
                            )
                        except Exception:
                            failure_receipt_published = False
                        if not failure_receipt_published:
                            info["_startup_receipt_confirmed"] = False
                            info["error"] += f"；启动失败回执未能确认保存：{receipt_error}"
            if info.get("phase") == "startup_receipt_failed":
                return _install_start_error(
                    install_id,
                    info["error"],
                    recovery_action=url_for(
                        "online_scripts.online_scripts_install_start_retry", install_id=install_id,
                    ),
                    log_available=info.get("_startup_receipt_confirmed") is not False,
                )
        return _install_start_error(install_id, info["error"])

    return redirect(
        url_for(
            "online_scripts.online_install_log",
            install_id=install_id,
            back=get_back_url("/online-scripts") if retry_of else "/online-scripts",
        )
    )


@bp.route("/online-scripts/install/<script_id>", methods=["POST"])
def online_scripts_install(script_id):
    raw_request_id = request.form.get("request_id", "")
    try:
        request_id = _install_request_id(raw_request_id) if str(raw_request_id).strip() else ""
    except ValueError as exc:
        return _install_retry_error(script_id, "在线脚本安装请求无效", str(exc), 400)

    if request_id:
        try:
            with ONLINE_INSTALL_LOCK:
                live = ONLINE_INSTALL_RUNNING.get(request_id)
                previous = dict(live) if isinstance(live, dict) else _load_persisted_online_install(request_id)
        except Exception as exc:
            return _install_retry_error(request_id, "无法查询原在线安装", f"安装记录无法读取：{exc}", 500)
        if previous and not _install_start_receipt_retryable(previous):
            if not _api_existing_install_request_matches(previous, script_id, request.form):
                return _install_retry_error(
                    request_id, "在线安装请求冲突", "该请求标识已绑定其他安装选项，请重新打开任务选择页。", 409,
                )
            try:
                current = get_online_install_job(request_id)
            except Exception as exc:
                return _install_retry_error(request_id, "无法查询原在线安装", f"安装状态无法核实：{exc}", 500)
            if not current:
                return _install_retry_error(
                    request_id, "在线安装请求已归档", "原安装记录已归档或不存在，不能复用该请求标识。", 409,
                )
            return redirect(url_for("online_scripts.online_install_log", install_id=request_id, back="/online-scripts"))

    try:
        item = get_online_script(script_id)
    except Exception as exc:
        return online_script_cache_error("在线脚本安装未完成", exc)

    if not item:
        abort(404)

    proxy_id = request.form.get("proxy_id", "").strip()
    try:
        validate_proxy_id(proxy_id)
    except ValueError as e:
        return _install_retry_error(
            script_id, "代理选择无效", str(e), 400,
            actions_html=f'<a class="btn btn-orange" href="/online-scripts/install-select/{h(script_id)}">重新选择安装选项</a>',
        )
    except Exception as e:
        return _install_retry_error(
            script_id, "代理配置无法读取", f"读取代理配置失败：{e}", 500,
            actions_html=f'<a class="btn btn-orange" href="/online-scripts/install-select/{h(script_id)}">重新选择安装选项</a>',
        )
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
            validate_online_task_import(item, selected_task_indexes)
        except ValueError as e:
            body = page_header_card(
                "任务选择无效", help_html=f"{h(e)}。请重新选择任务后重试。",
                actions_html=(
                    f'<a class="btn btn-gray" href="/online-scripts/install-select/{h(script_id)}">重新选择任务</a>'
                    '<a class="btn btn-orange" href="/online-scripts/source">检查脚本源数据</a>'
                ),
            )
            return layout("在线脚本安装失败", "online_scripts", body), 400
        except Exception as e:
            body = page_header_card(
                "任务校验失败", help_html=f"{h(e)}。请检查服务状态后重试。",
                actions_html=f'<a class="btn btn-gray" href="/online-scripts/install-select/{h(script_id)}">重新选择任务</a>',
            )
            return layout("在线脚本安装失败", "online_scripts", body), 500

    if request_id:
        try:
            previous = get_online_install_job(request_id)
        except Exception as exc:
            return _install_retry_error(request_id, "无法查询原在线安装", f"安装记录无法读取：{exc}", 500)
        if previous:
            if not _install_request_matches(
                previous, script_id, proxy_id, import_task, enable_task,
                request.form.get("force") == "1", selected_task_indexes,
            ):
                return _install_retry_error(
                    request_id, "在线安装请求冲突", "该请求标识已绑定其他安装选项，请重新打开任务选择页。", 409,
                )
            if not _install_start_receipt_retryable(previous):
                return redirect(url_for("online_scripts.online_install_log", install_id=request_id, back="/online-scripts"))

    with ONLINE_INSTALL_LOCK:
        active_id, _ = active_online_install_for_script(script_id)
        if active_id:
            return redirect(
                url_for(
                    "online_scripts.online_scripts_page",
                    msg="该脚本正在安装中",
                )
            )

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
            request_id=request_id,
        )

    return _start_install_job(
        script_id=script_id,
        item=item,
        proxy_id=proxy_id,
        import_task=import_task,
        force=force,
        enable_task=enable_task,
        selected_task_indexes=selected_task_indexes,
        request_id=request_id,
    )


def _api_values(data, key):
    """Read repeated form fields while keeping JSON lists intact."""
    if hasattr(data, "getlist"):
        return list(data.getlist(key))
    value = data.get(key, [])
    if isinstance(value, list):
        return list(value)
    return [value] if value not in (None, "") else []


def _api_flag(data, key):
    value = data.get(key)
    if value in (None, "", False, 0, "0", "false", "False"):
        return False
    if value in (True, 1, "1", "true", "True"):
        return True
    raise ValueError(f"{key} 必须为布尔值")


def _api_positive_install_index(raw):
    value = str(raw).strip()
    if not value.isdecimal():
        return None
    try:
        index = int(value)
    except (ValueError, OverflowError):
        return None
    return index if index > 0 else None


def _api_existing_install_request_matches(info, script_id, data):
    try:
        proxy_id = str(data.get("proxy_id", "") or "").strip()
        import_task = _api_flag(data, "import_task")
        enable_task = _api_flag(data, "enable_task")
        force = _api_flag(data, "force")
    except ValueError:
        return False

    saved_indexes = info.get("selected_task_indexes") or []
    if (
        not isinstance(saved_indexes, list)
        or any(
            not isinstance(value, str)
            or _api_positive_install_index(value) is None
            or str(_api_positive_install_index(value)) != value
            for value in saved_indexes
        )
        or len(set(saved_indexes)) != len(saved_indexes)
    ):
        return False

    selected = []
    if import_task:
        select_mode = str(data.get("select_mode", "") or "").strip()
        if select_mode == "all":
            excluded = set()
            for value in _api_values(data, "excluded_task_indexes"):
                for raw in str(value or "").replace("，", ",").split(","):
                    raw = raw.strip()
                    if not raw:
                        continue
                    index = _api_positive_install_index(raw)
                    if index is None:
                        return False
                    excluded.add(index)
            total = len(saved_indexes) + len(excluded)
            if any(index > total for index in excluded):
                return False
            selected = [str(index) for index in range(1, total + 1) if index not in excluded]
        elif select_mode:
            return False
        else:
            for raw in _api_values(data, "task_indexes"):
                index = _api_positive_install_index(raw)
                if index is None:
                    return False
                normalized = str(index)
                if normalized not in selected:
                    selected.append(normalized)
        if not selected:
            return False
    elif (
        enable_task
        or _api_values(data, "task_indexes")
        or _api_values(data, "excluded_task_indexes")
        or str(data.get("select_mode", "") or "").strip()
    ):
        return False

    return _install_request_matches(
        info, script_id, proxy_id, import_task, enable_task, force, selected,
    )


def _api_install_replay_response(install_id, info):
    phase, message = _api_install_display_state(info)

    payload = {
        "ok": True,
        "request_id": install_id,
        "install_id": install_id,
        "status": info.get("status"),
        "phase": phase,
        "running": bool(info.get("running")),
        "replayed": True,
        "log_url": url_for("online_scripts.api_online_install_log", install_id=install_id),
    }
    if message:
        payload["msg"] = message
    if info.get("recovery_persistence_error"):
        payload["persistence_error"] = True
    return jsonify(payload), 200


def _api_install_display_state(info):
    from .logs import _install_error, _install_phase

    return _install_phase(info), _install_error(info)


def _api_install_request_conflict(install_id):
    return jsonify(
        ok=False,
        request_id=install_id,
        install_id=install_id,
        msg="该请求标识已绑定其他安装选项，请查询原安装记录或重新选择。",
        log_url=url_for("online_scripts.api_online_install_log", install_id=install_id),
    ), 409


def _api_install_start_result(result, install_id):
    """Turn the HTML launch helper's failure tuple into a stable JSON result."""
    if isinstance(result, tuple) and len(result) == 2:
        _body, status = result
        if getattr(_body, "is_json", False):
            return result
        try:
            info = get_online_install_job(install_id) or {}
        except Exception as exc:
            return jsonify(
                ok=False,
                request_id=install_id,
                install_id=install_id,
                msg=f"安装状态无法读取：{exc}",
                retry_allowed=False,
            ), 500
        phase, message = _api_install_display_state(info)
        message = message or "在线脚本安装未启动，请查看安装记录后重试"
        payload = dict(
            ok=False,
            request_id=install_id,
            status=info.get("status") or "启动失败",
            phase=phase,
            running=bool(info.get("running")),
            msg=message,
            retry_allowed=status >= 500,
        )
        if info.get("_startup_receipt_confirmed") is False:
            payload.update(accepted=False, retry_allowed=True)
        else:
            payload.update(
                install_id=install_id,
                log_url=url_for("online_scripts.api_online_install_log", install_id=install_id),
            )
        return jsonify(payload), status
    return result


def _api_install_rejected(request_id, message, status_code, *, retry_allowed=False, **details):
    payload = {
        "ok": False,
        "request_id": request_id,
        "msg": str(message),
        "retry_allowed": retry_allowed,
        **details,
    }
    try:
        previous = get_online_install_job(request_id) if request_id else None
    except Exception as exc:
        payload["msg"] += f"；原请求状态无法核实：{exc}"
    else:
        if previous:
            payload["install_id"] = request_id
            payload["log_url"] = url_for("online_scripts.api_online_install_log", install_id=request_id)
        else:
            payload["accepted"] = False
    return jsonify(payload), status_code


@bp.route("/api/online-scripts/install/<script_id>", methods=["POST"])
def api_online_scripts_install(script_id):
    if request.is_json:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(ok=False, msg="请求 JSON 必须为对象", retry_allowed=False), 400
    else:
        data = request.form
    raw_request_id = data.get("request_id", "")
    try:
        # Always return a stable ID, including for the first request without one.
        request_id = _install_request_id(raw_request_id)
    except ValueError as exc:
        return jsonify(ok=False, msg=str(exc), retry_allowed=False), 400
    try:
        with ONLINE_INSTALL_LOCK:
            live = ONLINE_INSTALL_RUNNING.get(request_id)
            previous = dict(live) if isinstance(live, dict) else _load_persisted_online_install(request_id)
    except Exception as exc:
        return jsonify(ok=False, request_id=request_id, msg=f"安装记录无法读取：{exc}"), 500
    if previous and not _install_start_receipt_retryable(previous):
        if _api_existing_install_request_matches(previous, script_id, data):
            try:
                current = get_online_install_job(request_id)
            except Exception as exc:
                return jsonify(
                    ok=False, request_id=request_id,
                    msg=f"原安装状态无法核实：{exc}",
                    log_url=url_for("online_scripts.api_online_install_log", install_id=request_id),
                ), 500
            if not current:
                return jsonify(
                    ok=False, request_id=request_id,
                    msg="原安装记录已归档或不存在，不能复用该请求标识。",
                ), 409
            return _api_install_replay_response(request_id, current)
        if previous.get("script_id") != script_id:
            return _api_install_request_conflict(request_id)

    try:
        item = get_online_script(script_id)
    except Exception as exc:
        if previous and not _install_start_receipt_retryable(previous):
            return _api_install_request_conflict(request_id)
        return _api_install_rejected(
            request_id, f"在线脚本源缓存无法读取，未覆盖原文件：{exc}", 500,
        )
    if not item:
        if previous and not _install_start_receipt_retryable(previous):
            return _api_install_request_conflict(request_id)
        return _api_install_rejected(
            request_id, "脚本已不在本地缓存中，请同步在线脚本源后重试", 404,
        )

    proxy_id = str(data.get("proxy_id", "") or "").strip()
    try:
        validate_proxy_id(proxy_id)
        import_task = _api_flag(data, "import_task")
        enable_task = _api_flag(data, "enable_task")
        force = _api_flag(data, "force")
    except ValueError as exc:
        return _api_install_rejected(request_id, str(exc), 400, retry_allowed=True)
    except Exception as exc:
        return _api_install_rejected(request_id, f"读取代理配置失败：{exc}", 500)

    selected_task_indexes = []
    if import_task:
        try:
            select_mode = str(data.get("select_mode", "") or "").strip()
            if select_mode == "all":
                excluded_values = []
                for value in _api_values(data, "excluded_task_indexes"):
                    excluded_values.extend(str(value or "").replace("，", ",").split(","))
                excluded_values = [value.strip() for value in excluded_values if value.strip()]
                excluded = set()
                if excluded_values:
                    excluded = {int(value) for value in validated_install_task_indexes(item, excluded_values)}
                selected_task_indexes = [
                    str(index) for index in range(1, len(online_script_task_crons(item)) + 1)
                    if index not in excluded
                ]
            elif select_mode:
                raise ValueError("任务选择模式无效")
            else:
                selected_task_indexes = [str(value) for value in _api_values(data, "task_indexes")]
            selected_task_indexes = validated_install_task_indexes(item, selected_task_indexes)
            validate_online_task_import(item, selected_task_indexes)
        except ValueError as exc:
            return _api_install_rejected(
                request_id, f"{exc}。请重新选择任务后重试", 400,
            )
        except Exception as exc:
            return _api_install_rejected(request_id, f"任务校验失败：{exc}", 500)
    else:
        if enable_task or _api_values(data, "task_indexes") or _api_values(data, "excluded_task_indexes"):
            return _api_install_rejected(request_id, "未启用任务导入时不能提交任务选择", 400)
        if str(data.get("select_mode", "") or "").strip():
            return _api_install_rejected(request_id, "未启用任务导入时不能提交任务选择模式", 400)
        enable_task = False

    try:
        previous = get_online_install_job(request_id)
    except Exception as exc:
        return jsonify(ok=False, request_id=request_id, msg=f"安装记录无法读取：{exc}"), 500
    if previous:
        if not _install_request_matches(
            previous, script_id, proxy_id, import_task, enable_task, force, selected_task_indexes,
        ):
            return jsonify(ok=False, request_id=request_id,
                           msg="安装请求已绑定其他选项，请重新打开安装页面"), 409
        if not _install_start_receipt_retryable(previous):
            return _api_install_replay_response(request_id, previous)

    try:
        target = online_script_target(item)
    except Exception as exc:
        return _api_install_rejected(request_id, f"脚本目标路径非法：{exc}", 400)
    if target.exists() and not force:
        return _api_install_rejected(
            request_id, "目标已存在，请确认覆盖后重试", 409,
            retry_allowed=True, confirm_required=True, target=str(target),
        )

    active_id, active_info = active_online_install_for_script(script_id)
    if active_id:
        return jsonify(
            ok=False, request_id=request_id, install_id=active_id,
            status=(active_info or {}).get("status") or "运行中", running=True,
            msg="该脚本正在安装中，请先查看当前安装记录",
            log_url=url_for("online_scripts.api_online_install_log", install_id=active_id),
        ), 409

    result = _start_install_job(
        script_id=script_id, item=item, proxy_id=proxy_id, import_task=import_task,
        force=force, enable_task=enable_task, selected_task_indexes=selected_task_indexes,
        request_id=request_id,
    )
    result = _api_install_start_result(result, request_id)
    if isinstance(result, tuple):
        return result
    info = get_online_install_job(request_id)
    if not info:
        return jsonify(ok=False, request_id=request_id, install_id=request_id,
                       msg="安装已提交但状态无法读取，请查询安装日志后重试",
                       retry_allowed=False), 500
    phase, message = _api_install_display_state(info)
    payload = {
        "ok": True,
        "request_id": request_id,
        "install_id": request_id,
        "status": info.get("status", "准备中"),
        "running": bool(info.get("running")),
        "phase": phase,
        "log_url": url_for("online_scripts.api_online_install_log", install_id=request_id),
    }
    if message:
        payload["msg"] = message
    return jsonify(payload), 200


def _retry_unstarted_install_receipt(install_id):
    try:
        info = get_online_install_job(install_id)
    except Exception as exc:
        return _install_retry_error(
            install_id, "无法恢复在线安装", f"安装记录无法读取：{exc}", 500,
        )
    if not info:
        return _install_retry_error(install_id, "无法恢复在线安装", "安装记录不存在。", 404)
    if not _install_start_receipt_retryable(info):
        return _install_retry_error(
            install_id, "无法恢复在线安装", "该安装不是尚未启动的回执故障，请查询原安装状态。", 409,
        )

    script_id = str(info.get("script_id") or "").strip()
    try:
        item = get_online_script(script_id)
    except Exception as exc:
        return _install_retry_error(
            install_id, "无法恢复在线安装", f"在线脚本缓存无法读取：{exc}", 500,
        )
    if not item:
        return _install_retry_error(
            install_id, "无法恢复在线安装", "脚本已不在缓存中，请同步脚本源后重新选择。", 409,
        )

    try:
        proxy_id = str(info.get("proxy_id") or "")
        validate_proxy_id(proxy_id)
        selected = validated_install_retry_task_indexes(item, info) if info.get("import_task") else []
        if info.get("import_task"):
            validate_online_task_import(item, selected)
    except Exception as exc:
        return _install_retry_error(
            install_id, "无法恢复在线安装", f"原安装选项已失效：{exc}", 409,
        )
    try:
        target = online_script_target(item)
    except Exception as exc:
        return _install_retry_error(
            install_id, "无法恢复在线安装", f"脚本目标路径非法：{exc}", 400,
        )
    if target.exists() and not info.get("force"):
        return _install_confirmation(
            script_id, item, target, proxy_id, bool(info.get("import_task")),
            bool(info.get("enable_task")), selected, request_id=install_id,
        )
    return _start_install_job(
        script_id=script_id,
        item=item,
        proxy_id=proxy_id,
        import_task=bool(info.get("import_task")),
        force=bool(info.get("force")),
        enable_task=bool(info.get("enable_task")),
        selected_task_indexes=selected,
        request_id=install_id,
    )


@bp.route("/online-scripts/install-start-retry/<install_id>", methods=["POST"])
def online_scripts_install_start_retry(install_id):
    return _retry_unstarted_install_receipt(install_id)


def _install_confirmation(
    script_id, item, target, proxy_id, import_task, enable_task, selected_task_indexes,
    select_mode="", excluded_task_indexes="", action="", request_id="",
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
        {f'<input type="hidden" name="request_id" value="{h(request_id)}">' if request_id else ''}
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


def _install_start_error(install_id, error, recovery_action="", *, log_available=True, script_id=""):
    recovery_form = ""
    if recovery_action:
        recovery_form = (
            '<form class="inline-form" method="post" '
            f'action="{h(recovery_action)}">'
            '<button class="btn btn-primary" type="submit">沿原请求重试启动</button></form>'
        )
    log_link = (
        f'<a class="btn btn-gray" href="/online-scripts/log/{h(install_id)}">查看安装记录</a>'
        if log_available else ""
    )
    selection_link = (
        f'<a class="btn btn-orange" href="/online-scripts/install-select/{h(script_id)}">重新选择安装选项</a>'
        if script_id else ""
    )
    body = page_header_card(
        "在线脚本安装启动失败",
        help_html=h(error),
        actions_html=recovery_form + log_link + selection_link,
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


def _install_scheduler_reload_form(install_id):
    return (
        '<form class="inline-form" method="post" '
        f'action="/online-scripts/install-reload/{h(quote(install_id, safe=""))}">'
        '<button class="btn btn-primary" type="submit">重新加载调度器</button></form>'
    )


def _install_task_confirmation_form(install_id):
    return (
        '<form class="inline-form" method="post" '
        f'action="/online-scripts/install-task-confirm/{h(quote(install_id, safe=""))}">'
        '<button class="btn btn-primary" type="submit">核实任务导入结果</button></form>'
    )


def _install_reload_receipt_matches(expected, actual):
    return isinstance(actual, dict) and _install_snapshot(expected) == _install_snapshot(actual)


def _install_reload_state_url(install_id):
    return url_for("online_scripts.api_online_install_log", install_id=install_id, state_only="1")


def _install_reload_claim_conflict(install_id, actual):
    payload = {
        "ok": False,
        "request_id": install_id,
        "receipt_confirmation_required": True,
        "state_url": _install_reload_state_url(install_id),
        "msg": "原安装调度器恢复回执已变化，请查询原安装状态后重试。",
    }
    if (
        isinstance(actual, dict)
        and actual.get("id") == install_id
        and actual.get("scheduler_reload_pending")
        and not actual.get("running")
    ):
        payload.update(
            phase="scheduler_reload_pending",
            scheduler_reload_required=True,
            reload_url=url_for("online_scripts.api_online_scripts_install_reload", install_id=install_id),
        )
    return payload, 409


def _unconfirmed_install_tasks(info, message):
    info["task_recovery"] = dict(info["task_recovery"], pending=True)
    info.update(running=False, status="结果未知", phase="task_import_confirmation_pending", returncode=None,
                scheduler_reload_pending=False, error=message, updated_at=now_str())
    _persist_install_state(info)
    return {
        "ok": False, "request_id": info["id"], "status": info["status"], "phase": info["phase"],
        "msg": info["error"], "receipt_confirmation_required": True,
        "state_url": url_for("online_scripts.api_online_install_log", install_id=info["id"], state_only="1"),
    }, 503


def _save_install_task_result(info):
    info["task_recovery"] = dict(info["task_recovery"], pending=False)
    try:
        persist_online_install(info)
    except Exception:
        with ONLINE_INSTALL_LOCK:
            actual = _load_persisted_online_install(info["id"])
        fields = ("running", "status", "phase", "scheduler_reload_pending", "task_recovery")
        if not actual or any(actual.get(key) != info.get(key) for key in fields):
            raise


def _retry_install_tasks(install_id):
    """Retry only the task import after a script download already succeeded."""
    with ONLINE_INSTALL_LOCK:
        try:
            info = get_online_install_job(install_id)
        except Exception as exc:
            return {"ok": False, "request_id": install_id, "msg": f"安装记录无法读取：{exc}"}, 500
        if not info:
            return {"ok": False, "request_id": install_id, "msg": "安装记录不存在。"}, 404
        if info.get("running") or install_id in ONLINE_INSTALL_RETRYING:
            return {"ok": False, "request_id": install_id, "msg": "安装或恢复仍在进行，请稍后重试。"}, 409
        if (info.get("task_recovery") or {}).get("pending"):
            return {"ok": False, "request_id": install_id, "receipt_confirmation_required": True,
                    "msg": "原任务导入结果尚未确认，请先核实原结果，未重复导入任务。"}, 409
        if not info.get("import_task") or info.get("phase") != "importing_tasks":
            return {"ok": False, "request_id": install_id, "msg": "当前安装没有可单独恢复的任务导入阶段。"}, 409
        from .logs import _install_log_file
        _, log_failure = _install_log_file(info)
        if (
            not isinstance(info.get("log_file"), str) or not info["log_file"].strip()
            or (log_failure and log_failure[2] != 404)
        ):
            return {
                "ok": False, "request_id": install_id,
                "msg": "安装日志路径不可用，未导入任何任务。请修复原日志路径或权限后查询原安装结果。",
            }, 409
        consistency = verify_install_outputs(info)
        if consistency.get("target") != "unchanged":
            return {
                "ok": False,
                "request_id": install_id,
                "msg": "脚本目标已被后来修改或无法确认，请重新选择安装选项，未覆盖现有文件。",
                "consistency": consistency,
            }, 409
        try:
            item = get_online_script(str(info.get("script_id") or ""))
            selected = validated_install_retry_task_indexes(item, info) if item else None
        except Exception as exc:
            return {"ok": False, "request_id": install_id, "msg": f"原任务选择已失效：{exc}"}, 409
        if not item or not selected:
            return {"ok": False, "request_id": install_id, "msg": "脚本源或原任务选择已失效，请重新选择。"}, 409
        updated = dict(info)
        updated.update(running=True, status="运行中", phase="importing_tasks", error="", updated_at=now_str())
        try:
            with storage_transaction():
                baseline = task_import_file_signature()
            updated["task_recovery"] = {
                "id": uuid.uuid4().hex, "pending": True,
                "task_baseline_captured": True, "task_baseline": baseline,
            }
            persist_online_install(updated)
        except Exception as exc:
            return {"ok": False, "request_id": install_id, "msg": f"任务恢复未启动，安装回执无法保存：{exc}"}, 500
        ONLINE_INSTALL_RUNNING[install_id] = updated
        ONLINE_INSTALL_RETRYING.add(install_id)

    try:
        def before_publish(publication):
            recovery = dict(updated["task_recovery"], publication=publication)
            updated["task_recovery"] = recovery
            persist_online_install_task_recovery(install_id, recovery)

        try:
            import_task_if_needed(
                item,
                log_file=updated.get("log_file"),
                enable_task=bool(updated.get("enable_task")),
                selected_task_indexes=selected,
                raise_on_reload_error=True,
                before_publish=before_publish,
            )
        except TaskSchedulerReloadError as exc:
            snapshot_error = ""
            try:
                snapshot_saved = capture_install_outputs(updated, updated.get("target_path"))
                if not snapshot_saved:
                    snapshot_error = updated.get("persistence_error") or "任务产物快照未能保存"
            except Exception as snapshot_exc:
                snapshot_error = f"任务产物快照无法读取：{snapshot_exc}"
            scheduler_error = f"任务已保存，但调度器重载失败：{exc}"
            if snapshot_error:
                scheduler_error += f"；{snapshot_error}"
            updated.update(
                running=False,
                status="部分完成",
                phase="scheduler_reload_pending",
                returncode=0,
                scheduler_reload_pending=True,
                scheduler_reload_error=scheduler_error,
                error=scheduler_error,
                updated_at=now_str(),
            )
            try:
                _save_install_task_result(updated)
            except Exception as persistence_exc:
                return _unconfirmed_install_tasks(updated, f"{scheduler_error}；部分完成回执无法确认：{persistence_exc}")
            return {
                "ok": False,
                "request_id": install_id,
                "status": updated["status"],
                "phase": updated["phase"],
                "scheduler_reload_required": True,
                "reload_url": url_for("online_scripts.api_online_scripts_install_reload", install_id=install_id),
                "msg": updated["scheduler_reload_error"],
            }, 503
        capture_install_outputs(updated, updated.get("target_path"))
        updated.update(
            running=False,
            status="已完成",
            phase="completed",
            returncode=0,
            error="",
            scheduler_reload_pending=False,
            scheduler_reload_error="",
            updated_at=now_str(),
        )
        try:
            _save_install_task_result(updated)
        except Exception as exc:
            return _unconfirmed_install_tasks(updated, f"任务已导入，但完成回执无法确认：{exc}。请核实原任务导入结果，勿重复安装或导入。")
        return {
            "ok": True,
            "request_id": install_id,
            "status": "已完成",
            "phase": "completed",
            "msg": "任务导入已恢复，未重复下载安装",
        }, 200
    except Exception as exc:
        return _unconfirmed_install_tasks(updated, f"任务导入恢复结果无法确认：{exc}。请核实原任务导入结果。")
    finally:
        with ONLINE_INSTALL_LOCK:
            ONLINE_INSTALL_RETRYING.discard(install_id)


@bp.route("/online-scripts/install-task-retry/<install_id>", methods=["POST"])
def online_scripts_install_task_retry(install_id):
    payload, status = _retry_install_tasks(install_id)
    if payload.get("ok"):
        return redirect(url_for("online_scripts.online_install_log", install_id=install_id))
    actions = _install_scheduler_reload_form(install_id) if payload.get("scheduler_reload_required") else ""
    if payload.get("receipt_confirmation_required"):
        actions = _install_task_confirmation_form(install_id)
    return _install_retry_error(install_id, "任务导入恢复失败", payload.get("msg") or "任务导入恢复失败", status, actions_html=actions)


@bp.route("/api/online-scripts/install-task-retry/<install_id>", methods=["POST"])
def api_online_scripts_install_task_retry(install_id):
    if not re.fullmatch(r"[a-f0-9]{32}", install_id or ""):
        return jsonify(ok=False, request_id=install_id, msg="在线脚本安装请求标识无效"), 400
    payload, status = _retry_install_tasks(install_id)
    return jsonify(payload), status


def _confirm_install_tasks(install_id):
    identity = {"request_id": install_id}
    if not re.fullmatch(r"[a-f0-9]{32}", install_id or ""):
        return {**identity, "ok": False, "msg": "在线脚本安装请求标识无效"}, 400
    with ONLINE_INSTALL_LOCK:
        try:
            info = get_online_install_job(install_id)
        except Exception as exc:
            return {**identity, "ok": False, "msg": f"安装回执无法读取：{exc}"}, 503
        if not info:
            return {**identity, "ok": False, "msg": "安装记录不存在"}, 404
        recovery = info.get("task_recovery") or {}
        if info.get("running") or install_id in ONLINE_INSTALL_RETRYING:
            return {**identity, "ok": False, "msg": "安装或恢复仍在进行，请稍后查询原结果。"}, 409
        if not recovery.get("id"):
            return {**identity, "ok": False, "msg": "当前安装没有待核实的任务恢复回执。"}, 409
        if not recovery.get("pending"):
            return {**identity, "ok": True, "replayed": True, "status": info.get("status"),
                    "scheduler_reload_required": bool(info.get("scheduler_reload_pending")),
                    "msg": "原任务导入结果已核实，未重复导入。"}, 200
        ONLINE_INSTALL_RETRYING.add(install_id)
    try:
        with TASKS_MUTATION_LOCK, storage_transaction():
            actual = _load_persisted_online_install(install_id)
            if not actual or (actual.get("task_recovery") or {}).get("id") != recovery["id"]:
                raise RuntimeError("原任务恢复回执已变化，请重新查询原安装结果")
            confirmation, signature = task_import_publication_state({"script_id": actual["script_id"], **actual["task_recovery"]})
            published = confirmation == "published"
            recovery = dict(actual["task_recovery"], pending=False, confirmation=confirmation, confirmation_source=signature)
            message = ("已核实任务已写入，仅需重新加载调度器，无需重复导入。" if published
                       else "已核实任务未写入，可沿原安装继续恢复任务导入。")
            try:
                updated = persist_online_install_task_recovery(
                    install_id, recovery, running=False, status="部分完成" if published else "失败",
                    phase="scheduler_reload_pending" if published else "importing_tasks",
                    returncode=0 if published else None, scheduler_reload_pending=published,
                    scheduler_reload_error=message if published else "", error=message, updated_at=now_str(),
                )
            except Exception:
                updated = _load_persisted_online_install(install_id)
                if not updated or updated.get("task_recovery") != recovery:
                    raise
        with ONLINE_INSTALL_LOCK:
            live = ONLINE_INSTALL_RUNNING.get(install_id)
            if isinstance(live, dict):
                live.update(updated)
        return {**identity, "ok": True, "confirmation": confirmation, "scheduler_reload_required": published,
                "status": updated["status"], "msg": message}, 200
    except Exception as exc:
        return {**identity, "ok": False, "receipt_confirmation_required": True,
                "msg": f"原任务导入结果无法确认，未修改任务或调度器：{exc}"}, 503
    finally:
        with ONLINE_INSTALL_LOCK:
            ONLINE_INSTALL_RETRYING.discard(install_id)


@bp.route("/api/online-scripts/install-task-confirm/<install_id>", methods=["POST"])
def api_online_scripts_install_task_confirm(install_id):
    payload, status = _confirm_install_tasks(install_id)
    return jsonify(payload), status


@bp.route("/online-scripts/install-task-confirm/<install_id>", methods=["POST"])
def online_scripts_install_task_confirm(install_id):
    payload, status = _confirm_install_tasks(install_id)
    if payload.get("ok"):
        return redirect(url_for("online_scripts.online_install_log", install_id=install_id))
    return _install_retry_error(install_id, "无法核实任务导入结果", payload["msg"], status,
                                actions_html=_install_task_confirmation_form(install_id))


def _reload_install_scheduler(install_id):
    """Reload saved task imports without repeating the installation side effects."""
    with ONLINE_INSTALL_LOCK:
        try:
            info = get_online_install_job(install_id)
        except Exception as exc:
            return {
                "ok": False,
                "request_id": install_id,
                "msg": f"安装记录无法读取，未覆盖原文件：{exc}",
            }, 500
        if not info:
            return {"ok": False, "request_id": install_id, "msg": "安装记录不存在。"}, 404
        if info.get("running") or install_id in ONLINE_INSTALL_RETRYING:
            return {"ok": False, "request_id": install_id, "msg": "安装或恢复仍在进行，请稍后重试。"}, 409
        if (info.get("task_recovery") or {}).get("pending"):
            return {"ok": False, "request_id": install_id, "receipt_confirmation_required": True,
                    "msg": "原任务导入回执尚未确认，请先核实原结果。"}, 409
        if not info.get("scheduler_reload_pending"):
            return {"ok": False, "request_id": install_id, "msg": "该安装没有待恢复的调度器重载。"}, 409
        try:
            persisted = _load_persisted_online_install(install_id)
        except Exception as exc:
            return {
                "ok": False,
                "request_id": install_id,
                "receipt_confirmation_required": True,
                "state_url": _install_reload_state_url(install_id),
                "msg": f"原安装回执无法核实，未重载调度器：{exc}",
            }, 503
        if (
            not _install_reload_receipt_matches(info, persisted)
            or not isinstance(persisted, dict)
            or not persisted.get("scheduler_reload_pending")
        ):
            if persisted and persisted.get("id") == install_id:
                live = ONLINE_INSTALL_RUNNING.get(install_id)
                if isinstance(live, dict):
                    live.update(persisted)
            return _install_reload_claim_conflict(install_id, persisted)
        ONLINE_INSTALL_RETRYING.add(install_id)

    try:
        receipt_conflict_detected = False
        receipt_conflict_actual = None
        receipt_error = ""
        retryable_receipt = None
        updated = None
        try:
            with TASKS_MUTATION_LOCK:
                with storage_transaction():
                    actual = _load_persisted_online_install(install_id)
                if (
                    not _install_reload_receipt_matches(info, actual)
                    or not isinstance(actual, dict)
                    or not actual.get("scheduler_reload_pending")
                ):
                    receipt_conflict_detected = True
                    receipt_conflict_actual = actual
                else:
                    recovery = actual.get("task_recovery") or {}
                    if recovery.get("publication"):
                        confirmation, _signature = task_import_publication_state(
                            {"script_id": actual["script_id"], **recovery}
                        )
                        if confirmation != "published":
                            raise RuntimeError("原任务导入尚未写入，请先核实原结果")
                    reload_scheduler()

                    updated = dict(actual)
                    updated.update(
                        phase="completed" if updated.get("status") == "部分完成" else updated.get("phase") or "unknown",
                        scheduler_reload_pending=False,
                        scheduler_reload_error="",
                        updated_at=now_str(),
                    )
                    if updated.get("status") == "部分完成":
                        updated.update(status="已完成", error="", persistence_error="")
                    try:
                        updated = persist_online_install_scheduler_reload(install_id, actual.get("task_recovery"), updated)
                    except Exception as exc:
                        receipt_error = str(exc)
                        try:
                            persisted_after = _load_persisted_online_install(install_id)
                        except Exception as read_exc:
                            persisted_after = None
                            receipt_error += f"；重读原安装回执失败：{read_exc}"
                        if _install_reload_receipt_matches(updated, persisted_after):
                            updated = persisted_after
                            receipt_error = ""
                        else:
                            retryable_receipt = (
                                persisted_after
                                if isinstance(persisted_after, dict)
                                and persisted_after.get("id") == install_id
                                and persisted_after.get("task_recovery") == actual.get("task_recovery")
                                and persisted_after.get("scheduler_reload_pending")
                                and not persisted_after.get("running")
                                else None
                            )
                            if not retryable_receipt and isinstance(persisted_after, dict):
                                receipt_conflict_detected = True
                                receipt_conflict_actual = persisted_after
                                receipt_error = ""
        except Exception as exc:
            return {
                "ok": False,
                "request_id": install_id,
                "phase": "scheduler_reload_pending",
                "msg": f"任务保持已保存，调度器重载失败：{exc}",
                "scheduler_reload_required": True,
                "reload_url": url_for("online_scripts.api_online_scripts_install_reload", install_id=install_id),
                "state_url": _install_reload_state_url(install_id),
            }, 503

        if receipt_conflict_detected:
            with ONLINE_INSTALL_LOCK:
                live = ONLINE_INSTALL_RUNNING.get(install_id)
                if isinstance(live, dict) and receipt_conflict_actual and receipt_conflict_actual.get("id") == install_id:
                    live.update(receipt_conflict_actual)
            return _install_reload_claim_conflict(install_id, receipt_conflict_actual)

        if receipt_error:
            with ONLINE_INSTALL_LOCK:
                live = ONLINE_INSTALL_RUNNING.get(install_id)
                if isinstance(live, dict) and retryable_receipt:
                    live.update(retryable_receipt)
            payload = {
                "ok": False,
                "request_id": install_id,
                "phase": "scheduler_reload_pending",
                "receipt_confirmation_required": True,
                "state_url": _install_reload_state_url(install_id),
                "msg": f"调度器已重新加载，但恢复回执无法确认：{receipt_error}。请查询原安装状态。",
            }
            if retryable_receipt:
                payload.update(
                    scheduler_reload_required=True,
                    reload_url=url_for("online_scripts.api_online_scripts_install_reload", install_id=install_id),
                )
            return payload, 503

        with ONLINE_INSTALL_LOCK:
            live = ONLINE_INSTALL_RUNNING.get(install_id)
            if isinstance(live, dict):
                live.update(updated)
        return {
            "ok": True,
            "request_id": install_id,
            "phase": updated.get("phase") or "completed",
            "status": updated.get("status") or "已完成",
            "receipt_confirmed": True,
            "msg": "调度器已恢复并确认保存，未重复安装或导入任务",
        }, 200
    finally:
        with ONLINE_INSTALL_LOCK:
            ONLINE_INSTALL_RETRYING.discard(install_id)


@bp.route("/online-scripts/install-reload/<install_id>", methods=["POST"])
def online_scripts_install_reload(install_id):
    payload, status = _reload_install_scheduler(install_id)
    if not payload.get("ok"):
        actions = _install_scheduler_reload_form(install_id) if payload.get("scheduler_reload_required") else ""
        return _install_retry_error(
            install_id,
            "任务调度尚未恢复" if status == 503 else "无法恢复任务调度",
            payload.get("msg") or "任务调度恢复失败",
            status,
            actions_html=actions,
        )
    return redirect(url_for("online_scripts.online_install_log", install_id=install_id))


@bp.route("/api/online-scripts/install-reload/<install_id>", methods=["POST"])
def api_online_scripts_install_reload(install_id):
    if not re.fullmatch(r"[a-f0-9]{32}", install_id or ""):
        return jsonify(ok=False, request_id=install_id, msg="在线脚本安装请求标识无效"), 400
    payload, status = _reload_install_scheduler(install_id)
    return jsonify(payload), status


@bp.route("/online-scripts/install-retry/<install_id>", methods=["POST"])
def online_scripts_install_retry(install_id):
    try:
        info = get_online_install_job(install_id)
    except Exception as exc:
        return _install_retry_error(
            install_id,
            "无法重试在线安装",
            f"安装记录无法读取，未覆盖原文件：{exc}。修复记录文件后重试。",
            500,
        )
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

    if info.get("scheduler_reload_pending"):
        return _install_retry_error(
            install_id, "请先恢复任务调度", "任务已保存，请先重新加载调度器，再检查安装结果。", 409,
            actions_html=_install_scheduler_reload_form(install_id),
        )

    if info.get("status") not in ("结果未知", "失败", "超时", "已停止", "启动失败"):
        return _install_retry_error(
            install_id,
            "无法重试在线安装",
            f"当前状态为 {info.get('status') or '未知'}，仅失败或结果未知的安装允许重试。",
            409,
        )

    with ONLINE_INSTALL_LOCK:
        try:
            info = get_online_install_job(install_id)
        except Exception as exc:
            return _install_retry_error(
                install_id,
                "无法重试在线安装",
                f"安装记录无法读取，未覆盖原文件：{exc}。修复记录文件后重试。",
                500,
            )
        if not info:
            return _install_retry_error(install_id, "无法重试在线安装", "安装记录不存在。", 404)
        if (info.get("task_recovery") or {}).get("pending"):
            return _install_retry_error(
                install_id, "请先核实任务导入结果", "原任务导入结果尚未确认，未重新下载或导入。", 409,
                actions_html=_install_task_confirmation_form(install_id),
            )
        relation = online_install_retry_relation(info)
        if relation.get("retry_relation_error"):
            return _install_retry_error(
                install_id, "无法重试在线安装", relation["retry_relation_error"], 409,
            )
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
        try:
            item = get_online_script(script_id)
        except Exception as exc:
            return _install_retry_error(
                install_id,
                "无法重试在线安装",
                f"在线脚本源缓存无法读取，未覆盖原文件：{exc}。请检查源数据后重试。",
                500,
            )
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
        except (TypeError, ValueError) as e:
            return _install_retry_error(
                install_id,
                "无法重试在线安装",
                f"原安装记录的任务选择已失效：{e}。请重新选择任务后重试。",
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
        info = get_online_install_job(install_id)
        if not ok and isinstance(info, dict) and info.get("stop_receipt_confirmation_required"):
            return False, (
                f"{msg}；停止失败状态尚未确认保存，请查询原安装状态后修复存储并重试停止。"
            ), 503
        code = 200 if ok else (404 if info is None else 409)
        return ok, msg, code
    except Exception as e:
        return False, f"停止请求失败：{e}", 500


@bp.route("/online-scripts/install-stop/<install_id>", methods=["POST"])
def online_scripts_install_stop(install_id):
    ok, msg, code = _stop_install_result(install_id)
    back = get_back_url("/online-scripts")
    log_url = url_for("online_scripts.online_install_log", install_id=install_id, back=back)
    if not ok:
        body = page_header_card(
            "无法停止在线安装",
            help_html=h(msg),
            actions_html=(
                f'<a class="btn btn-primary" href="{h(log_url)}">查询原安装状态</a>'
                f'<a class="btn btn-gray" href="{h(back)}">返回</a>'
            ),
        )
        return layout("无法停止在线安装", "online_scripts", body), code

    if "停止请求日志写入失败：" in msg:
        body = page_header_card(
            "停止请求已提交",
            help_html=h(msg),
            actions_html=(
                f'<a class="btn btn-primary" href="{h(log_url)}">查询原安装状态</a>'
                f'<a class="btn btn-gray" href="{h(back)}">返回</a>'
            ),
        )
        return layout("停止请求已提交", "online_scripts", body), 200

    return redirect(log_url)


@bp.route("/api/online-scripts/install-stop/<install_id>", methods=["POST"])
def api_online_scripts_install_stop(install_id):
    ok, msg, code = _stop_install_result(install_id)
    try:
        info = get_online_install_job(install_id)
    except Exception:
        info = None
    payload = {
        "ok": ok, "msg": msg, "install_id": install_id,
        "running": bool(info.get("running")) if info else None,
        "status": (info or {}).get("status") or ("记录不存在" if code == 404 else "记录不可读"),
        "state_url": url_for("online_scripts.api_online_install_log", install_id=install_id, state_only=1),
    }
    if info and info.get("stop_receipt_confirmation_required"):
        payload.update(
            receipt_confirmation_required=True,
            stop_receipt_confirmation_required=True,
            persistence_error=True,
        )
    warning_marker = "停止请求日志写入失败："
    if warning_marker in msg:
        payload["warning"] = msg[msg.index(warning_marker):]
    return jsonify(payload), code
