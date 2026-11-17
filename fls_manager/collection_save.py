import json
import re
import uuid

from .collection_delete import collection_version
from .models import COLLECTION_MUTATION_LOCK
from .paths import DATA_DIR
from .storage import write_json
from .utils import now_str


def save_collection(data, *, load_collections, save_collections, unique_collection_name):
    if not isinstance(data, dict):
        return {"ok": False, "msg": "请求必须是对象", "saved": False, "resolved": True}, 400
    request_id = data.get("request_id", uuid.uuid4().hex)
    collection_id = data.get("collection_id", "")
    try:
        invalid_field = "request_id"
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ValueError("提交标识无效，请刷新后重试")
        invalid_field = "collection_id"
        if not isinstance(collection_id, str) or "\x00" in collection_id:
            raise ValueError("合集选择无效，未保存")
        for field in ("name", "remark"):
            invalid_field = field
            if not isinstance(data.get(field, ""), str) or "\x00" in data.get(field, ""):
                raise ValueError("合集名称或备注无效，未保存")
        invalid_field = "collection_version"
        version = data.get("collection_version")
        if "collection_version" in data and (not isinstance(version, str) or not re.fullmatch(r"[a-f0-9]{64}", version)):
            raise ValueError("合集版本无效，未保存")
        invalid_field = "confirm_retry"
        confirm = data.get("confirm_retry", False)
        if not (isinstance(confirm, bool) or isinstance(confirm, str) and confirm in ("0", "1")):
            raise ValueError("恢复确认无效，未保存")
    except ValueError as exc:
        return {"ok": False, "msg": str(exc), "saved": False, "resolved": True,
                "request_id": request_id, "invalid_field": invalid_field}, 400

    values = {"name": data.get("name", "").strip(), "remark": data.get("remark", "").strip(),
              "collection_id": collection_id, "collection_version": version}
    path = DATA_DIR / "collection_save_requests" / (request_id + ".json")
    with COLLECTION_MUTATION_LOCK:
        record = None
        target_id = collection_id

        def result(msg, status, saved=False, **extra):
            return {"ok": status == 200, "msg": msg, "saved": saved, "resolved": saved is not None,
                    "request_id": request_id, "collection_id": target_id, **extra}, status

        def complete(item, status=200, message="合集已保存"):
            record["state"] = "completed"
            try:
                write_json(path, record)
            except Exception as exc:
                return result(f"合集已保存，结果记录失败：{exc}", 503, True, collection=item)
            return result(message, status, True, collection=item)

        try:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                pass
            else:
                if (not isinstance(record, dict) or record.get("request_id") != request_id
                    or not isinstance(record.get("input"), dict) or not isinstance(record.get("desired"), dict)
                    or not isinstance(record["desired"].get("id"), str) or not record["desired"]["id"]
                    or record.get("state") not in ("prepared", "failed", "completed")
                    or "before" not in record or record["before"] is not None and not isinstance(record["before"], dict)
                    or any(not isinstance(record["desired"].get(key), str) or "\x00" in record["desired"][key]
                           for key in ("name", "remark", "created_at", "updated_at"))
                    or any(not isinstance(record["input"].get(key), str) or "\x00" in record["input"][key]
                           for key in ("name", "remark", "collection_id"))
                    or (record["input"]["collection_id"] and (not record["before"]
                        or record["before"].get("id") != record["desired"]["id"]
                        or record["input"]["collection_id"] != record["desired"]["id"]))
                    or (not record["input"]["collection_id"] and (record["before"] is not None
                        or not re.fullmatch(r"[a-f0-9]{32}", record["desired"]["id"])))):
                    raise RuntimeError("合集保存记录损坏，未重复保存，请修复记录后重试")
                target_id = record["desired"]["id"]
                if record["input"].get("collection_id") != collection_id:
                    return result("提交标识已用于其他合集，未保存", 409, None)
                if record["input"] != values and record["state"] != "failed":
                    return result("提交标识已用于其他内容，请先查询原提交结果", 409, None)
                if record["state"] == "completed":
                    return result("合集已保存，未重复执行", 200, True, collection=record["desired"])

            collections = load_collections()
            current = next((item for item in collections if item["id"] == target_id), None)
            if record:
                if current == record["desired"]:
                    return complete(current)
                if current != record["before"]:
                    return result("合集已被修改或删除，未覆盖当前内容，请核对后合并", 409,
                                  None if not collection_id else False, current=current, conflict=True)
                if record["state"] == "prepared" and confirm not in (True, "1"):
                    return result("上次保存中断，当前未发现目标内容；请确认后重试原提交", 409, None, confirm_required=True)
            else:
                if collection_id and current is None:
                    return result("合集不存在，草稿未保存", 404)
                if collection_id and version is not None and collection_version(current) != version:
                    return result("合集已被修改，未覆盖当前内容，请核对后合并", 409, current=current, conflict=True)
                target_id = collection_id or uuid.uuid4().hex

            desired = dict(current or {"id": target_id, "created_at": now_str()})
            desired.update(name=unique_collection_name(values["name"], exclude_id=collection_id),
                           remark=values["remark"], updated_at=now_str())
            # Keep the original target across retries, including an interrupted creation.
            record = {"request_id": request_id, "input": values, "before": current,
                      "desired": desired, "state": "prepared"}
            try:
                write_json(path, record)
            except Exception as exc:
                return result(f"合集保存请求写入失败，内容未更改：{exc}", 500, confirm_required=True)
            updated = [desired if item["id"] == target_id else item for item in collections]
            if current is None:
                updated.append(desired)
            try:
                save_collections(updated)
            except Exception as exc:
                actual = next((item for item in load_collections() if item["id"] == target_id), None)
                if actual == desired:
                    return complete(actual, 503, f"合集已保存，保存确认失败：{exc}")
                if actual == current:
                    record["state"] = "failed"
                    try:
                        write_json(path, record)
                    except Exception as record_exc:
                        return result(f"合集未保存，恢复记录写入失败：{record_exc}；原错误：{exc}", 500,
                                      confirm_required=True)
                    prefix = "合集更新失败，原有内容未更改" if collection_id else "合集创建失败，未保存"
                    return result(f"{prefix}：{exc}", 500)
                return result(f"合集保存结果未确认，当前内容已变化：{exc}", 500, None, current=actual)
            return complete(desired)
        except Exception as exc:
            return result(f"合集保存未确认，未继续执行：{exc}", 500, None)
