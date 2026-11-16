import hashlib
import json
import os
import stat
import uuid
from pathlib import Path, PureWindowsPath

from .paths import SCRIPT_DIR


class ScriptFileConflict(ValueError):
    pass


class ScriptFileWriteError(RuntimeError):
    def __init__(self, message, saved=False):
        super().__init__(message)
        self.saved = saved


def script_file_target(rel):
    rel = str(rel or "").strip()
    if (
        not rel or "\x00" in rel or rel.startswith(("/", "\\"))
        or PureWindowsPath(rel).drive or ".." in rel.replace("\\", "/").split("/")
    ):
        raise ValueError("必须使用 scripts 目录内的相对路径")
    base = SCRIPT_DIR.resolve()
    target = base / rel
    for parent in (target, *target.parents):
        if parent == base:
            break
        if parent.is_symlink():
            raise ValueError("文件操作不允许符号链接，请打开实际文件路径")
    target = target.resolve()
    if target == base or base not in target.parents:
        raise ValueError("路径必须指向 scripts 目录内的文件或子目录")
    return target


def script_file_snapshot(target, rel):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(target, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("不是普通文本文件")
        fp = os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise
    with fp:
        data = fp.read()
    signature = [str(rel), str(target), metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns,
                 metadata.st_mode, metadata.st_uid, metadata.st_gid]
    revision = hashlib.sha256(json.dumps(signature).encode("utf-8") + b"\x00" + data).hexdigest()
    return data.decode("utf-8"), revision, metadata


def publish_script_file(target, rel, content, revision=None, metadata=None):
    if script_file_target(rel) != target:
        raise ScriptFileConflict("文件路径已变更，请重新读取后重试")
    temp_file = target.with_name(f".fls-script-{uuid.uuid4().hex}.tmp")
    created = False
    saved = False
    failure = None
    try:
        fd = os.open(temp_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        created = True
        try:
            fp = os.fdopen(fd, "wb")
        except Exception:
            os.close(fd)
            raise
        with fp:
            fp.write(content.encode("utf-8"))
            fp.flush()
            if metadata:
                current = os.fstat(fp.fileno())
                if hasattr(os, "fchown") and (current.st_uid, current.st_gid) != (metadata.st_uid, metadata.st_gid):
                    os.fchown(fp.fileno(), metadata.st_uid, metadata.st_gid)
                temp_file.chmod(stat.S_IMODE(metadata.st_mode))
            os.fsync(fp.fileno())
        if script_file_target(rel) != target:
            raise ScriptFileConflict("文件路径在保存期间已变更，未覆盖目标")
        if revision is None:
            os.link(temp_file, target)
        else:
            if script_file_snapshot(target, rel)[1] != revision:
                raise ScriptFileConflict("文件在保存期间已变更，未覆盖新内容，请合并草稿后重试")
            os.replace(temp_file, target)
        saved = True
    except Exception as e:
        failure = e
        raise
    finally:
        if created:
            try:
                temp_file.unlink(missing_ok=True)
            except OSError as e:
                detail = f"清理临时文件失败：{e}"
                if failure:
                    detail = f"{failure}；{detail}"
                raise ScriptFileWriteError(detail, saved=saved) from e
