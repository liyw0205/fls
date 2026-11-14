from ._common import *
from ._common import _load_persisted_backup_job


@bp.route("/api/backup/create", methods=["POST"])
def api_backup_create():
    items = parse_items_from_form("items")

    if not items:
        return jsonify({
            "ok": False,
            "msg": "请至少选择一个备份内容",
        }), 400

    try:
        job_id = start_backup_job(items)
    except BackupJobStartError as e:
        result = {"ok": False, "msg": str(e)}
        if e.job_id:
            result["job_id"] = e.job_id
        return jsonify(result), 500
    except Exception as e:
        return jsonify({
            "ok": False,
            "msg": f"备份任务启动失败：{e}",
        }), 500

    return jsonify({
        "ok": True,
        "job_id": job_id,
    })

@bp.route("/api/backup/job/<job_id>")
def api_backup_job(job_id):
    info = get_backup_job(job_id)

    if not info:
        return jsonify({
            "ok": False,
            "msg": "任务不存在",
        }), 404

    return jsonify({
        "ok": True,
        **info,
    })


@bp.route("/api/backup/cancel/<job_id>", methods=["POST"])
def api_backup_cancel(job_id):
    with BACKUP_JOB_LOCK:
        info = BACKUP_JOBS.get(job_id)

        if not isinstance(info, dict):
            persisted = _load_persisted_backup_job(job_id)
            if not persisted:
                return jsonify({"ok": False, "msg": "任务不存在"}), 404
            if persisted.get("status") == "结果未知":
                return jsonify({
                    "ok": False,
                    "msg": "备份任务状态未知，面板重启后无法取消",
                }), 409
            if not persisted.get("running"):
                return jsonify({"ok": False, "msg": "备份任务已结束"}), 409
            return jsonify({
                "ok": False,
                "msg": "备份任务状态未知，面板重启后无法取消",
            }), 409

        cancel_event = BACKUP_CANCEL_EVENTS.get(job_id)
        if info.get("status") == "结果未知":
            return jsonify({
                "ok": False,
                "msg": "备份任务状态未知，面板重启后无法取消",
            }), 409
        if not info.get("running") or cancel_event is None:
            return jsonify({"ok": False, "msg": "备份任务已结束"}), 409

        previous_status = info.get("status")
        previous_updated_at = info.get("updated_at")
        info["status"] = "正在取消"
        info["updated_at"] = now_str()
        try:
            persist_backup_job(info)
        except Exception as e:
            info["status"] = previous_status
            info["updated_at"] = previous_updated_at
            return jsonify({
                "ok": False,
                "msg": f"取消请求未保存，备份仍继续运行：{e}",
            }), 500
        cancel_event.set()
        return jsonify({"ok": True, "msg": "已请求取消"})

@bp.route("/api/backup/list")
def api_backup_list():
    return jsonify({
        "ok": True,
        "items": list_backup_files(),
    })

@bp.route("/api/backup/delete", methods=["POST"])
def api_backup_delete():
    filename = request.form.get("filename", "").strip()

    try:
        target = backup_safe_file(filename)

        if target.exists():
            target.unlink()

        return jsonify({
            "ok": True,
            "msg": "已删除",
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "msg": str(e),
        }), 400
