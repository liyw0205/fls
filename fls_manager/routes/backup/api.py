from ._common import *


@bp.route("/api/backup/create", methods=["POST"])
def api_backup_create():
    from .actions import submit_backup_creation
    data = request.get_json(silent=True) if request.is_json else dict(
        request.form, items=request.form.getlist("items")
    )
    payload, status = submit_backup_creation(data)
    return jsonify(payload), status

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
    try:
        items = list_backup_files()
    except Exception:
        return jsonify(ok=False, msg="备份列表无法读取，请检查目录权限后刷新列表"), 500
    return jsonify({
        "ok": True,
        "items": items,
    })

@bp.route("/api/backup/delete", methods=["POST"])
def api_backup_delete():
    from .actions import submit_backup_deletion
    if not request.is_json and any(len(request.form.getlist(key)) != 1 for key in ("filename", "version")):
        return jsonify(ok=False, msg="请选择唯一备份文件及版本"), 400
    data = request.get_json(silent=True) if request.is_json else dict(request.form)
    payload, status = submit_backup_deletion(data)
    return jsonify(payload), status
