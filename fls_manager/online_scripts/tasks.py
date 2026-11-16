import uuid
from pathlib import PureWindowsPath

from ..models import mutate_tasks, TaskSchedulerReloadError
from ..paths import SCRIPT_DIR
from ..scheduler import reload_scheduler, cron_to_trigger
from ..utils import now_str
from .logs import append_log

def normalize_online_task_crons(data):
    """
    标准化 task_cron / task_link 返回的任务列表。

    支持：
    1. 单个 dict
    2. list[dict]
    3. {"tasks": list[dict]}
    4. {"task_cron": list[dict] 或 dict}
    """
    raw = data

    if isinstance(data, dict):
        if isinstance(data.get("tasks"), list):
            raw = data.get("tasks")
        elif "task_cron" in data:
            raw = data.get("task_cron")

    if isinstance(raw, dict):
        raw = [raw]

    if not isinstance(raw, list):
        return []

    result = []

    for item in raw:
        if not isinstance(item, dict):
            continue

        name = str(item.get("name") or "").strip()
        cron = str(item.get("cron") or "").strip()
        command = str(item.get("command") or "").strip()

        if not name or not command:
            continue

        task = {
            "name": name,
            "cron": cron,
            "command": command,
        }

        for key in ("var", "vars", "env"):
            if key in item:
                task["var"] = item.get(key)
                break

        if "remark" in item:
            task["remark"] = str(item.get("remark") or "").strip()

        if "config_path" in item:
            task["config_path"] = str(item.get("config_path") or "").strip()

        if "enabled" in item:
            task["enabled"] = bool(item.get("enabled"))

        result.append(task)

    return result


def online_script_task_crons(item):
    return normalize_online_task_crons(item.get("task_cron"))


def online_task_cron_vars(task_cron):
    raw = None

    if isinstance(task_cron, dict):
        if "var" in task_cron:
            raw = task_cron.get("var")
        elif "vars" in task_cron:
            raw = task_cron.get("vars")
        elif "env" in task_cron:
            raw = task_cron.get("env")

    env = {}

    if isinstance(raw, dict):
        for k, v in raw.items():
            key = str(k or "").strip()
            if key:
                env[key] = "" if v is None else str(v)
        return env

    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                for k, v in item.items():
                    key = str(k or "").strip()
                    if key:
                        env[key] = "" if v is None else str(v)
                continue

            text = str(item or "").strip()
            if not text:
                continue

            if ":" in text:
                key, value = text.split(":", 1)
            elif "=" in text:
                key, value = text.split("=", 1)
            else:
                continue

            key = key.strip()
            value = value.strip()

            if key:
                env[key] = value

    return env


def guess_task_command(item):
    task_crons = online_script_task_crons(item)

    if task_crons:
        command = str(task_crons[0].get("command", "") or "").strip()
        if command:
            return command

    script_type = item.get("type")
    link_name = str(item.get("link_name") or "").strip().strip("/")

    if script_type == "raw":
        return f"task {link_name}"

    if script_type == "repo":
        return f"cd {link_name} && npm start"

    return ""


class OnlineTaskValidationError(ValueError):
    pass


def validate_online_task_import(item, selected_task_indexes=None):
    task_crons = online_script_task_crons(item)
    if not task_crons:
        raise OnlineTaskValidationError("脚本源未提供可导入任务，请检查 task_cron / task_link")
    if not str(item.get("id") or "").strip():
        raise OnlineTaskValidationError("脚本 ID 为空，无法标识已导入任务")

    if selected_task_indexes is None:
        selected_set = set(range(1, len(task_crons) + 1))
    else:
        if not isinstance(selected_task_indexes, (list, tuple, set)) or not selected_task_indexes:
            raise OnlineTaskValidationError("任务选择为空或已失效")
        selected_set = set()
        for value in selected_task_indexes:
            raw = str(value).strip()
            if not raw.isdecimal() or not 1 <= int(raw) <= len(task_crons):
                raise OnlineTaskValidationError("任务序号无效，请重新选择任务")
            selected_set.add(int(raw))

    selected = [(idx, task) for idx, task in enumerate(task_crons, 1) if idx in selected_set]
    errors = []
    for idx, task in selected:
        label = f"第 {idx} 个任务（{task['name']}）"
        if "\x00" in task["name"] or "\x00" in task["command"]:
            errors.append(f"{label}名称或命令包含 NUL 字符")
        if task["cron"]:
            try:
                cron_to_trigger(task["cron"])
            except ValueError as e:
                errors.append(f"{label}Cron 不合法：{e}")
        for key, value in online_task_cron_vars(task).items():
            if "=" in key or "\x00" in key:
                errors.append(f"{label}变量名无效，不允许等号或 NUL 字符")
            if "\x00" in value:
                errors.append(f"{label}变量值包含 NUL 字符")
        config_path = task.get("config_path") or ""
        if config_path:
            try:
                if (
                    "\x00" in config_path or config_path.startswith(("/", "\\"))
                    or PureWindowsPath(config_path).drive
                    or ".." in config_path.replace("\\", "/").split("/")
                ):
                    raise ValueError("必须使用 scripts 目录内的相对文件路径")
                target = (SCRIPT_DIR / config_path).resolve()
                base = SCRIPT_DIR.resolve()
                if target == base or base not in target.parents:
                    raise ValueError("配置路径超出 scripts 目录")
            except (ValueError, OSError, RuntimeError) as e:
                errors.append(f"{label}配置路径无效：{e}")
    if errors:
        raise OnlineTaskValidationError("\n".join(errors) + "\n未导入任何任务，请修正脚本源或取消选择无效任务。")
    return selected


def import_task_if_needed(
    item, log_file=None, enable_task=False, selected_task_indexes=None, raise_on_reload_error=False,
):
    """
    导入在线脚本任务。

    selected_task_indexes:
        None：导入全部任务
        空或非法序号：拒绝导入
        set/list[int]：只导入指定序号任务，序号从 1 开始，对应脚本源 task_cron 顺序
    """
    # Validate the entire selection before mutating tasks, including duplicates.
    selected = validate_online_task_import(item, selected_task_indexes)
    task_crons = online_script_task_crons(item)

    online_id = item.get("id")
    imported = 0
    skipped = 0
    unselected = len(task_crons) - len(selected)
    messages = []

    def add_selected_tasks(tasks):
        nonlocal imported, skipped

        for idx, task_cron in selected:
            name = task_cron["name"]
            cron_expr = str(task_cron.get("cron") or "").strip()
            command = task_cron["command"]
            task_env = online_task_cron_vars(task_cron)
            config_path = str(task_cron.get("config_path") or "").strip()
            remark = str(
                task_cron.get("remark")
                or f"从在线脚本导入：{item.get('name')}"
            ).strip()

            online_task_key = f"{online_id}:{idx}:{name}"
            exists = any(
                task.get("online_script_task_key") == online_task_key
                or (len(task_crons) == 1 and task.get("online_script_id") == online_id)
                for task in tasks
            )
            if exists:
                msg = f"任务已导入过，跳过：{name}"
                skipped += 1
                messages.append(msg)
                continue

            task = {
                "id": uuid.uuid4().hex,
                "name": name,
                "remark": remark,
                "command": command,
                "cron": cron_expr,
                "enabled": bool(enable_task),
                "env": task_env,
                "proxy_id": "",
                "notify": {"mode": "default", "ids": []},
                "random_delay": {"mode": "none", "seconds": 0},
                "run_count": 0,
                "online_script_id": online_id,
                "online_script_task_key": online_task_key,
                "created_at": now_str(),
                "updated_at": now_str(),
            }

            if config_path:
                task["config_path"] = config_path

            tasks.append(task)
            imported += 1
            env_msg = f" / 变量 {len(task_env)} 个" if task_env else ""
            config_msg = f" / 配置 {config_path}" if config_path else ""
            status_msg = "启用" if enable_task else "禁用"
            messages.append(
                f"任务已导入：{name} / {cron_expr or '手动'} / {command} / {status_msg}"
                f"{env_msg}{config_msg}"
            )

        return imported > 0

    try:
        mutate_tasks(add_selected_tasks, reload_scheduler)
    except TaskSchedulerReloadError as e:
        summary = f"任务已持久化，但调度器重载失败：{e}"
        if log_file:
            append_log(log_file, summary)
        if raise_on_reload_error:
            raise
        return True, summary

    if log_file:
        for msg in messages:
            append_log(log_file, msg)

    summary = f"任务导入完成：新增 {imported} 个，已存在 {skipped} 个，未选择 {unselected} 个"
    if not imported:
        summary = "所选任务均已导入，无需重复导入。" + summary

    if log_file:
        append_log(log_file, summary)

    return imported > 0, summary + "\n" + "\n".join(messages)


def script_has_task(item):
    return len(online_script_task_crons(item)) > 0


def task_vars_summary(task_crons):
    total = 0

    for tc in task_crons:
        total += len(online_task_cron_vars(tc))

    return total
