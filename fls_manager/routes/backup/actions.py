import json
import re

from . import _common as backup
from ...storage import storage_transaction, write_json


def submit_backup_deletion(data):
    def result(message, status, **extra):
        return {"ok": status == 200, "msg": message, **extra}, status

    if not isinstance(data, dict):
        return result("请求必须是对象", 400)
    filename, version = data.get("filename"), data.get("version")
    try:
        backup.validate_backup_filename(filename)
    except ValueError as exc:
        return result(str(exc), 400)
    if not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version):
        return result("缺少有效文件版本，请刷新备份列表后重新选择", 400)

    try:
        with backup.BACKUP_JOB_LOCK, backup.backup_directory_fd() as directory:
            try:
                current = backup.backup_entry_stat(directory, filename)
            except FileNotFoundError:
                return result("原备份已不存在，未重复删除", 200, deleted=True)
            if not backup.stat.S_ISREG(current.st_mode):
                return result("备份文件不是常规文件，未删除", 400, deleted=False)
            if backup.backup_file_version(current) != version:
                return result("备份文件已变化，未删除。请刷新列表，核对后重新选择", 409, deleted=False)
            try:
                backup.backup_entry_unlink(directory, filename)
            except OSError as exc:
                try:
                    actual = backup.backup_entry_stat(directory, filename)
                except FileNotFoundError:
                    return result("原备份已删除，删除确认异常", 200, deleted=True)
                except OSError:
                    return result("删除结果无法确认，请刷新列表核对，不要直接重复删除", 503, deleted=None)
                if backup.backup_file_version(actual) == version:
                    return result(f"备份未删除，请检查目录权限后重试：{exc}", 500, deleted=False)
                return result("原文件已变化，删除结果无法确认，请刷新列表核对", 503, deleted=None)
            return result("已删除", 200, deleted=True)
    except ValueError as exc:
        return result(str(exc), 400, deleted=False)
    except OSError:
        return result("备份目录无法读取，请修复目录权限后刷新列表", 500, deleted=None)


def submit_backup_cancellation(job_id):
    def result(message, status, *, accepted=False, **extra):
        return {"ok": status == 200, "msg": message, "job_id": job_id,
                "accepted": accepted, **extra}, status

    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        return result("备份作业标识无效", 400)
    with backup.BACKUP_JOB_LOCK, storage_transaction():
        info = backup.BACKUP_JOBS.get(job_id)
        live = isinstance(info, dict)
        if not live:
            try:
                info = backup._load_persisted_backup_job(job_id)
            except Exception:
                return result("备份作业记录无法读取，请修复存储后查询", 500, accepted=None)
        if not info:
            return result("任务不存在", 404)
        if not info.get("running") and info.get("status") == "已取消":
            return result("备份已取消，未重复执行", 200, accepted=True, job=dict(info))
        if info.get("status") == "结果未知" or info.get("running") and (
            not live or backup.BACKUP_CANCEL_EVENTS.get(job_id) is None
        ):
            return result("备份任务状态未知，面板重启后无法取消", 409, accepted=None, job=dict(info))
        if not info.get("running"):
            if info.get("cancel_requested"):
                return result("原取消请求已接受，实际结果：" + str(info.get("status")), 200, accepted=True, job=dict(info))
            return result("备份任务已结束", 409, job=dict(info))
        event = backup.BACKUP_CANCEL_EVENTS[job_id]
        if info.get("cancel_requested") and event.is_set():
            return result("已请求取消，请查询实际终态", 200, accepted=True, job=dict(info))
        try:
            previous = backup._load_persisted_backup_job(job_id)
        except Exception:
            return result("备份作业记录无法读取，未提交取消，请修复存储后重试", 500, retry_allowed=True)
        if info.get("id") != job_id or previous and backup._backup_job_snapshot(previous) != backup._backup_job_snapshot(info):
            return result("备份作业记录已变化，未提交取消，请检查原作业", 409, accepted=None, job=dict(info))

        proposed = dict(info, status="正在取消", updated_at=backup.now_str(),
                        cancel_requested=True, cancel_requested_at=backup.now_str())
        status, message, saved = 200, "已请求取消", True
        try:
            backup.persist_backup_job(proposed)
        except Exception as exc:
            try:
                actual = backup._load_persisted_backup_job(job_id)
            except Exception:
                # The user requested cancellation; signal the live worker even when
                # durability cannot be verified, and report that uncertainty.
                saved = None
                message = f"已通知运行中的备份取消，但无法确认取消记录是否保存，请查询实际终态：{exc}"
                proposed["cancel_error"] = message
            else:
                if actual == backup._backup_job_snapshot(proposed):
                    message = f"取消请求已保存并接受，保存确认异常，请查询实际终态：{exc}"
                elif actual == previous:
                    return result(f"取消请求未保存，备份仍继续运行：{exc}", 500,
                                  cancel_saved=False, retry_allowed=True, job=dict(info))
                else:
                    return result("取消记录保存结果与原作业不一致，请检查实际状态", 503,
                                  accepted=None, job=dict(info))
            status = 503
        info.update(proposed)
        event.set()
        return result(message, status, accepted=True, cancel_saved=saved, job=dict(info))


def submit_backup_creation(data):
    def result(message, status, **extra):
        return {"ok": status == 200, "msg": message, "resolved": True, **extra}, status

    if not isinstance(data, dict):
        return result("请求必须是对象", 400)
    items = data.get("items")
    if (not isinstance(items, list) or not items or any(not isinstance(item, str) or item not in ("data", "scripts") for item in items)
        or len(items) != len(set(items))):
        return result("请选择有效且不重复的备份内容", 400)
    items = [item for item in ("data", "scripts") if item in items]
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
        return result("备份请求标识无效，请重新打开备份页面", 400)
    confirm = data.get("confirm_retry", False)
    if not (type(confirm) is bool or isinstance(confirm, str) and confirm in ("0", "1")):
        return result("备份重试确认无效", 400)
    path = backup.DATA_DIR / "backup_requests" / (request_id + ".json")

    with backup.BACKUP_JOB_LOCK, storage_transaction():
        record = None
        try:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            else:
                if (not isinstance(record, dict) or record.get("request_id") != request_id
                    or record.get("state") not in ("prepared", "launching", "failed", "submitted")
                    or record.get("items") not in (["data"], ["scripts"], ["data", "scripts"])):
                    raise RuntimeError("备份请求记录损坏")
                if record["items"] != items:
                    return result("该请求已用于其他备份范围，请先查询原请求", 409, resolved=False)
            info = backup.get_backup_job(request_id)
            if info:
                if not record or info.get("items") != items:
                    return result("备份作业与请求不一致，未重复创建", 409)
                if record["state"] != "submitted":
                    record["state"] = "submitted"
                    try:
                        write_json(path, record)
                    except Exception:
                        return result("原备份已存在，提交回执无法更新，请继续查看原作业", 503, job_id=request_id, job=info)
                return result("已找到原备份作业，未重复创建", 200, job_id=request_id, job=info)
            if record and record["state"] in ("launching", "submitted"):
                return result("原备份已进入启动阶段，但作业记录不存在，请检查备份列表后明确创建新备份", 409)
            if record and record["state"] == "prepared" and confirm not in (True, "1"):
                return result("上次请求在启动前中断，请确认后继续原备份", 409, resolved=False, confirm_required=True)
            with backup.BACKUP_ACTIVITY_CONDITION:
                if backup.RESTORE_ACTIVE:
                    return result("正在恢复备份，请恢复结束后继续原请求", 409, resolved=False)
            record = {"request_id": request_id, "items": items, "state": "prepared"}
            write_json(path, record)
            record["state"] = "launching"
            write_json(path, record)
            try:
                backup.start_backup_job(items, job_id=request_id)
            except Exception:
                info = backup.get_backup_job(request_id)
                record["state"] = "submitted" if info else "failed"
                try:
                    write_json(path, record)
                except Exception:
                    pass
                if info:
                    return result("备份启动未完成，请查看原作业状态", 503, job_id=request_id, job=info)
                return result("备份状态未保存，任务未启动，请修复存储后继续原请求", 500, resolved=False)
            record["state"] = "submitted"
            try:
                write_json(path, record)
            except Exception:
                return result("备份已提交，提交回执保存失败，请继续查看原作业", 503, job_id=request_id)
            return result("备份已提交", 200, job_id=request_id)
        except Exception:
            return result("备份请求或作业记录无法确认，请修复存储后查询原请求", 500, resolved=False)
