import uuid

from ..models import load_tasks, save_tasks
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


def import_task_if_needed(item, log_file=None, enable_task=False, selected_task_indexes=None):
    """
    导入在线脚本任务。

    selected_task_indexes:
        None 或空：导入全部任务
        set/list[int]：只导入指定序号任务，序号从 1 开始，对应脚本源 task_cron 顺序
    """
    task_crons = online_script_task_crons(item)

    if not task_crons:
        msg = "脚本源未提供 task_cron / task_link 任务，跳过任务导入"
        if log_file:
            append_log(log_file, msg)
        return False, msg

    selected_set = set()

    if selected_task_indexes:
        for x in selected_task_indexes:
            try:
                n = int(x)
                if n > 0:
                    selected_set.add(n)
            except Exception:
                pass

    tasks = load_tasks()
    online_id = item.get("id")

    imported = 0
    skipped = 0
    unselected = 0
    messages = []

    for idx, task_cron in enumerate(task_crons, 1):
        if selected_set and idx not in selected_set:
            unselected += 1
            continue

        name = str(task_cron.get("name") or item.get("name") or "").strip()
        cron_expr = str(task_cron.get("cron") or "").strip()
        command = str(task_cron.get("command") or "").strip() or guess_task_command(item)
        task_env = online_task_cron_vars(task_cron)

        config_path = str(task_cron.get("config_path") or "").strip().strip("/")

        remark = str(
            task_cron.get("remark")
            or f"从在线脚本导入：{item.get('name')}"
        ).strip()

        if not name:
            msg = f"第 {idx} 个任务 task_cron.name 为空，跳过"
            skipped += 1
            messages.append(msg)
            if log_file:
                append_log(log_file, msg)
            continue

        if not command:
            msg = f"第 {idx} 个任务无法推导任务命令，请在 task_cron.command 中显式指定"
            skipped += 1
            messages.append(msg)
            if log_file:
                append_log(log_file, msg)
            continue

        if cron_expr:
            try:
                cron_to_trigger(cron_expr)
            except Exception as e:
                msg = f"第 {idx} 个任务 Cron 不合法：{e}"
                skipped += 1
                messages.append(msg)
                if log_file:
                    append_log(log_file, msg)
                continue

        online_task_key = f"{online_id}:{idx}:{name}"

        exists = False

        for task in tasks:
            if task.get("online_script_task_key") == online_task_key:
                exists = True
                break

            if len(task_crons) == 1 and task.get("online_script_id") == online_id:
                exists = True
                break

        if exists:
            msg = f"任务已导入过，跳过：{name}"
            skipped += 1
            messages.append(msg)
            if log_file:
                append_log(log_file, msg)
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

        msg = (
            f"任务已导入：{name} / "
            f"{cron_expr or '手动'} / "
            f"{command} / "
            f"{status_msg}"
            f"{env_msg}"
            f"{config_msg}"
        )

        messages.append(msg)

        if log_file:
            append_log(log_file, msg)

    if imported > 0:
        save_tasks(tasks)
        reload_scheduler()

    summary = f"任务导入完成：新增 {imported} 个，跳过 {skipped} 个，未选择 {unselected} 个"

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