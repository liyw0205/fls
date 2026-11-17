from ._common import *


@bp.route("/api/backup/create", methods=["POST"])
def api_backup_create():
    if request.is_json or "request_id" in request.form:
        from .actions import submit_backup_creation
        data = request.get_json(silent=True) if request.is_json else dict(request.form, items=request.form.getlist("items"))
        payload, status = submit_backup_creation(data)
        return jsonify(payload), status
    selected = request.form.getlist("items")
    if any(item not in ("data", "scripts") for item in selected) or len(selected) != len(set(selected)):
        return jsonify({"ok": False, "msg": "备份内容选择无效"}), 400
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
    try:
        info = get_backup_job(job_id)
    except Exception:
        return jsonify({"ok": False, "msg": "备份作业记录无法读取，请修复存储后重新查询"}), 500

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
    from .actions import submit_backup_cancellation
    payload, status = submit_backup_cancellation(job_id)
    return jsonify(payload), status

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
