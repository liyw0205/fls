import json
import re

from . import _common as backup
from ...storage import storage_transaction, write_json


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
