import copy
import ctypes
import errno
import hashlib
import json
import os
import re
import shlex
import stat
from pathlib import Path

from .paths import SCRIPT_DIR, SCRIPT_RENAME_FILE
from .script_files import script_file_target, ScriptFileConflict
from .storage import storage_transaction, write_json
from .utils import now_str

RENAME_TERMINAL = ("succeeded", "rolled_back")


def load_script_renames():
    with storage_transaction():
        try:
            data = json.loads(SCRIPT_RENAME_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except ValueError as e:
            raise RuntimeError("重命名恢复记录读取失败，请修复记录后重试") from e
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise RuntimeError("重命名恢复记录无效，请修复记录后重试")
        return data


def get_script_rename(operation_id):
    return next((row for row in load_script_renames() if row.get("id") == operation_id), None)


def save_script_rename(record):
    with storage_transaction():
        rows = load_script_renames()
        remaining = [row for row in rows if row.get("id") != record["id"]]
        pending = [row for row in remaining if row.get("status") not in RENAME_TERMINAL]
        finished = [row for row in remaining if row.get("status") in RENAME_TERMINAL]
        record["updated_at"] = now_str()
        write_json(SCRIPT_RENAME_FILE, [copy.deepcopy(record), *pending, *finished[:199]])


def pending_script_rename(task_id=None, path=None, exclude_id=None, task=None):
    for row in load_script_renames():
        if row.get("status") in RENAME_TERMINAL or row.get("id") == exclude_id:
            continue
        if task_id is not None and any(update["id"] == task_id for update in row.get("updates", [])):
            return row
        if task is not None:
            source = SCRIPT_DIR.resolve() / row["source"]
            destination = SCRIPT_DIR.resolve() / row["destination"]
            if any(_mentions_path(str(task.get("command") or ""), root) for root in (source, destination)) or any(
                _mapped_path(task.get("config_path", ""), root, other) is not None
                for root, other in ((source, destination), (destination, source))
            ):
                return row
        if path is not None:
            for rel in (row["source"], row["destination"]):
                root = SCRIPT_DIR.resolve() / rel
                if path == root or root in path.parents or path in root.parents:
                    return row
    return None


def validate_new_name(name):
    if (
        not name or name in (".", "..") or "/" in name or "\\" in name
        or any(ord(char) < 32 for char in name) or name.endswith((".", " "))
        or ":" in name
    ):
        raise ValueError("新名称非法，必须为单个有效文件名")


def rename_identity(target):
    metadata = target.lstat()
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
        raise ValueError("只允许重命名普通文件或目录")
    return [metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)]


def _mapped_path(value, source, destination):
    if not value:
        return None
    raw = Path(value)
    target = raw if raw.is_absolute() else SCRIPT_DIR / raw
    target = target.resolve()
    if target != source and source not in target.parents:
        return None
    mapped = destination / target.relative_to(source) if target != source else destination
    return str(mapped) if raw.is_absolute() else str(mapped.relative_to(SCRIPT_DIR.resolve()))


def _mentions_path(command, source):
    rel = str(source.relative_to(SCRIPT_DIR.resolve()))
    variants = {rel, "./" + rel, str(source), source.name}
    return any(re.search(r"(?<![\w./-])" + re.escape(value) + r"(?=$|[/\s'\";&|()])", command) for value in variants)


def suggested_command(command, source, destination):
    changed = []
    for line in command.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("task "):
            try:
                parts = shlex.split(stripped)
                mapped = _mapped_path(parts[1], source, destination)
            except (ValueError, IndexError):
                mapped = None
            if mapped:
                parts[1] = mapped
                newline = "\r\n" if line.endswith("\r\n") else ("\n" if line.endswith("\n") else "")
                indent = line[:len(line) - len(line.lstrip())]
                line = indent + shlex.join(parts) + newline
        changed.append(line)
    return "".join(changed)


def rename_preview(source, destination):
    from .models import load_tasks

    references = []
    for task in load_tasks():
        config = _mapped_path(task.get("config_path", ""), source, destination)
        command = str(task.get("command") or "")
        proposed = suggested_command(command, source, destination)
        if config is None and proposed == command and not _mentions_path(command, source):
            continue
        references.append({
            "id": task["id"], "name": task["name"],
            "before": {"command": command, "config_path": task.get("config_path", "")},
            "after": {"command": proposed, "config_path": config if config is not None else task.get("config_path", "")},
            "manual": _mentions_path(proposed, source),
        })
    metadata = source.stat()
    signature = [str(source), rename_identity(source), metadata.st_mtime_ns, metadata.st_size,
                 [{"id": row["id"], "before": row["before"]} for row in references]]
    revision = hashlib.sha256(json.dumps(signature, sort_keys=True).encode("utf-8")).hexdigest()
    return references, revision


def validate_rename_updates(references, form, source, destination):
    if sorted(form.getlist("task_id")) != sorted(row["id"] for row in references):
        raise ValueError("任务选择不完整或包含非法任务，未更改文件或任务")
    updates = []
    for reference in references:
        task_id = reference["id"]
        command = form.get(f"command_{task_id}", "").strip()
        config = form.get(f"config_{task_id}", "").strip()
        if not command or "\x00" in command:
            raise ValueError("任务命令为空或包含 NUL，未更改文件或任务")
        if _mentions_path(command, source):
            raise ValueError(f"任务 {reference['name']} 的命令仍引用旧路径，请确认命令后重试")
        if config:
            config_target = script_file_target(config)
            if config_target == source or source in config_target.parents:
                raise ValueError("任务配置仍引用旧路径，未更改文件或任务")
        # Validate task lines against the original files before moving anything.
        for line in command.splitlines():
            if line.strip().startswith("task "):
                from .command import parse_task_line_to_cmd
                parts = shlex.split(line.strip())
                old_path = _mapped_path(parts[1], destination, source)
                if old_path:
                    parts[1] = old_path
                parse_task_line_to_cmd(shlex.join(parts))
        updates.append({"id": task_id, "before": reference["before"], "after": {"command": command, "config_path": config}})
    return updates


def _check_not_running(record):
    from .task_runner import is_running
    from .routes.scripts.debug import SCRIPT_DEBUG_LOCK, SCRIPT_DEBUG_RUNNING

    if any(is_running(update["id"]) for update in record["updates"]):
        raise ScriptFileConflict("相关任务仍在运行，请先停止后恢复重命名")
    source = script_file_target(record["source"])
    destination = script_file_target(record["destination"])
    with SCRIPT_DEBUG_LOCK:
        for info in SCRIPT_DEBUG_RUNNING.values():
            path = Path(info.get("script") or "").resolve()
            if info.get("running") and any(path == root or root in path.parents for root in (source, destination)):
                raise ScriptFileConflict("相关脚本仍在调试，请先停止调试后重试")


def _move_no_replace(source, destination):
    if destination.exists() or destination.is_symlink():
        raise ScriptFileConflict("目标已存在，未覆盖目标")
    if source.is_file():
        moved = False
        link = getattr(os, "link", None)
        if callable(link):
            try:
                link(source, destination)
            except OSError as exc:
                moved = _move_file_without_hard_links(source, destination, exc)
        else:
            # Android's Python may omit os.link while libc still supports hard links.
            libc = ctypes.CDLL(None, use_errno=True)
            link = getattr(libc, "link", None)
            try:
                if link is None:
                    raise OSError(errno.ENOSYS, "hard links are unavailable", str(destination))
                link.argtypes = (ctypes.c_char_p, ctypes.c_char_p)
                link.restype = ctypes.c_int
                if link(os.fsencode(source), os.fsencode(destination)) != 0:
                    code = ctypes.get_errno()
                    raise OSError(code, os.strerror(code), str(destination))
            except OSError as exc:
                moved = _move_file_without_hard_links(source, destination, exc)
        if not moved:
            source.unlink()
    else:
        if os.name == "posix":
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = getattr(libc, "renameat2", None)
            if renameat2 is not None:
                result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
                if result == 0:
                    return
                code = ctypes.get_errno()
                if code not in (errno.ENOSYS, errno.EINVAL):
                    raise OSError(code, os.strerror(code), str(destination))
        source.rename(destination)


def _move_file_without_hard_links(source, destination, cause):
    unsupported = {errno.ENOSYS, errno.EPERM, errno.EACCES}
    unsupported.update(
        value for value in (getattr(errno, "ENOTSUP", None), getattr(errno, "EOPNOTSUPP", None))
        if value is not None
    )
    if cause.errno not in unsupported:
        raise cause

    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        renameat2.restype = ctypes.c_int
        result = renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
        if result == 0:
            return True
        code = ctypes.get_errno()
        if code not in unsupported | {errno.EINVAL}:
            raise OSError(code, os.strerror(code), str(destination))

    # Last-resort filesystems without hard links or renameat2: reserve a new name,
    # then replace only that reservation with the original inode.
    mode = stat.S_IMODE(source.stat().st_mode)
    fd = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    reservation = os.fstat(fd)
    os.close(fd)
    try:
        current = os.stat(destination, follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (reservation.st_dev, reservation.st_ino):
            raise ScriptFileConflict("目标已被其他文件占用，未覆盖目标")
        os.replace(source, destination)
    except Exception:
        try:
            current = os.stat(destination, follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (reservation.st_dev, reservation.st_ino):
                os.unlink(destination)
        except OSError:
            pass
        raise
    return True


def _validate_task_states(record, tasks, source, destination, rollback):
    tasks = {task["id"]: task for task in tasks}
    expected_ids = {update["id"] for update in record["updates"]}
    invalid_root, mapped_root = (destination, source) if rollback else (source, destination)
    for task_id, task in tasks.items():
        if task_id not in expected_ids and (
            _mentions_path(str(task.get("command") or ""), invalid_root)
            or _mapped_path(task.get("config_path", ""), invalid_root, mapped_root) is not None
        ):
            raise ScriptFileConflict("出现新的待移动路径任务引用，请先修正新增任务后恢复")
    for update in record["updates"]:
        current = tasks.get(update["id"])
        values = {key: current.get(key, "") for key in ("command", "config_path")} if current else None
        if values not in (update["before"], update["after"]):
            raise ScriptFileConflict("相关任务已删除或引用发生冲突，请检查任务后恢复，不会覆盖任务修改")


def resume_script_rename(record, rollback=False):
    from .models import TASKS_MUTATION_LOCK, load_tasks, mutate_tasks
    from .scheduler import reload_scheduler

    with TASKS_MUTATION_LOCK:
        if record["status"] in RENAME_TERMINAL:
            if rollback != (record["status"] == "rolled_back"):
                raise ScriptFileConflict("该操作已结束，不允许反向重复执行")
            return record
        _check_not_running(record)
        source = script_file_target(record["source"])
        destination = script_file_target(record["destination"])
        old_exists = source.exists()
        new_exists = destination.exists()
        for path in (source, destination):
            if path.exists() and rename_identity(path) != record["identity"]:
                raise ScriptFileConflict("源或目标已被其他文件替换，未更改文件，请先检查实际状态")
        if not old_exists and not new_exists:
            raise ScriptFileConflict("源和目标均不存在，请恢复文件后重试")
        _validate_task_states(record, load_tasks(), source, destination, rollback)
        desired = source if rollback else destination
        other = destination if rollback else source
        if not desired.exists():
            _move_no_replace(other, desired)
        elif other.exists():
            # A crash between link and unlink leaves both names on the same inode.
            if not desired.is_file() or not other.is_file():
                raise ScriptFileConflict("两个目录均存在，请先检查目录状态")
            other.unlink()
        record["status"] = "moved"
        save_script_rename(record)

        def update_tasks(current):
            _validate_task_states(record, current, source, destination, rollback)
            changed = False
            for update in record["updates"]:
                task = next((task for task in current if task["id"] == update["id"]), None)
                if task is None:
                    raise ScriptFileConflict("相关任务已删除，请检查任务后恢复")
                values = update["before"] if rollback else update["after"]
                if any(task.get(key, "") != value for key, value in values.items()):
                    task.update(values)
                    task["updated_at"] = now_str()
                    changed = True
            return changed

        mutate_tasks(update_tasks)
        record["status"] = "tasks_saved"
        save_script_rename(record)
        try:
            reload_scheduler()
        except Exception as e:
            record["error"] = f"文件和任务引用已更新，但调度器重载失败：{e}"
            save_script_rename(record)
            raise RuntimeError(record["error"]) from e
        record["status"] = "rolled_back" if rollback else "succeeded"
        record["error"] = ""
        save_script_rename(record)
        return record


def begin_script_rename(source, destination, operation_id, updates):
    record = {"id": operation_id, "source": str(source.relative_to(SCRIPT_DIR.resolve())),
              "destination": str(destination.relative_to(SCRIPT_DIR.resolve())), "identity": rename_identity(source),
              "updates": updates, "status": "prepared", "error": "", "created_at": now_str()}
    _check_not_running(record)
    if pending_script_rename(path=source) or pending_script_rename(path=destination):
        raise ScriptFileConflict("路径存在待恢复重命名，请先完成恢复")
    save_script_rename(record)
    return resume_script_rename(record)
