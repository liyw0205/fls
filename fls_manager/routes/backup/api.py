from ._common import *


@bp.route("/api/backup/create", methods=["POST"])
def api_backup_create():
    items = parse_items_from_form("items")

    if not items:
        return jsonify({
            "ok": False,
            "msg": "请至少选择一个备份内容",
        }), 400

    job_id = start_backup_job(items)

    return jsonify({
        "ok": True,
        "job_id": job_id,
    })

@bp.route("/api/backup/job/<job_id>")
def api_backup_job(job_id):
    info = BACKUP_JOBS.get(job_id)

    if not info:
        return jsonify({
            "ok": False,
            "msg": "任务不存在",
        }), 404

    return jsonify({
        "ok": True,
        **info,
    })

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
