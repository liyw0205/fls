from .bp import bp
import os
import sys
import time
import uuid
import shutil
import tarfile
import zipfile
import tempfile
import threading
import subprocess
from pathlib import Path
from datetime import datetime

from flask import request, Response, redirect, url_for, jsonify, abort

from ...paths import BASE_DIR, DATA_DIR, SCRIPT_DIR, LOG_DIR
from ...ui.layout import layout
from ...utils import h, now_str, safe_name
from ...scheduler import reload_scheduler

BACKUP_DIR = DATA_DIR / "backups"
BACKUP_JOBS = {}


# ============================================================
# 基础工具
# ============================================================

def backup_dir():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUP_DIR


def backup_safe_file(filename):
    filename = str(filename or "").split("/")[-1].split("\\")[-1].strip()

    if not filename:
        raise ValueError("文件名为空")

    target = (backup_dir() / filename).resolve()
    base = backup_dir().resolve()

    if target != base and not str(target).startswith(str(base) + os.sep):
        raise ValueError("备份文件路径非法")

    return target


def fmt_size(n):
    try:
        n = float(n)
    except Exception:
        return "-"

    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0

    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1

    return f"{n:.1f} {units[i]}"


def backup_type_text(items):
    items = set(items or [])

    if "data" in items and "scripts" in items:
        return "配置 + 脚本"

    if "data" in items:
        return "仅配置"

    if "scripts" in items:
        return "仅脚本"

    return "未选择"


def parse_items_from_form(prefix="items"):
    items = request.form.getlist(prefix)
    result = []

    if "data" in items:
        result.append("data")

    if "scripts" in items:
        result.append("scripts")

    return result


def list_backup_files():
    backup_dir()

    files = []

    for item in BACKUP_DIR.glob("*.tar.gz"):
        if not item.is_file():
            continue

        try:
            stat = item.stat()
            files.append({
                "name": item.name,
                "size": stat.st_size,
                "size_text": fmt_size(stat.st_size),
                "mtime": stat.st_mtime,
                "mtime_text": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception:
            pass

    files.sort(key=lambda x: x.get("mtime", 0), reverse=True)
    return files


def write_dependencies_file(path):
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=120,
        )

        deps_text = result.stdout or ""

        if result.returncode != 0:
            deps_text = "# pip freeze 执行失败，以下为输出：\n" + deps_text

        Path(path).write_text(deps_text, encoding="utf-8")

    except Exception as e:
        Path(path).write_text(
            "# pip freeze 执行失败：{}\n".format(e),
            encoding="utf-8",
        )


def tar_filter_exclude_backups(tarinfo):
    """
    备份 data 时排除 data/backups，避免递归把备份打进备份里。
    """
    try:
        name = str(tarinfo.name or "").replace("\\", "/")

        if name == "data/backups" or name.startswith("data/backups/"):
            return None
    except Exception:
        pass

    return tarinfo


# ============================================================
# 后台创建备份
# ============================================================

def create_backup_worker(job_id, items):
    info = BACKUP_JOBS.get(job_id)

    if not info:
        return

    items = list(items or [])

    info["running"] = True
    info["status"] = "正在压缩"
    info["error"] = ""
    info["updated_at"] = now_str()

    try:
        if not items:
            raise RuntimeError("未选择备份内容")

        backup_dir()

        type_name = "all"

        if items == ["data"]:
            type_name = "config"
        elif items == ["scripts"]:
            type_name = "scripts"
        elif set(items) == {"data", "scripts"}:
            type_name = "all"

        filename = f"fls-backup-{type_name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{job_id[:8]}.tar.gz"
        target = backup_safe_file(filename)

        deps_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        deps_tmp.close()

        try:
            write_dependencies_file(deps_tmp.name)

            with tarfile.open(target, "w:gz") as tar:
                if "data" in items and DATA_DIR.exists():
                    tar.add(
                        DATA_DIR,
                        arcname="data",
                        filter=tar_filter_exclude_backups,
                    )

                if "scripts" in items and SCRIPT_DIR.exists():
                    tar.add(
                        SCRIPT_DIR,
                        arcname="scripts",
                    )

                if Path(deps_tmp.name).exists():
                    tar.add(deps_tmp.name, arcname="dependencies.txt")

        finally:
            try:
                os.remove(deps_tmp.name)
            except Exception:
                pass

        stat = target.stat()

        info["running"] = False
        info["status"] = "已完成"
        info["filename"] = target.name
        info["size"] = stat.st_size
        info["size_text"] = fmt_size(stat.st_size)
        info["updated_at"] = now_str()

    except Exception as e:
        info["running"] = False
        info["status"] = "失败"
        info["error"] = str(e)
        info["updated_at"] = now_str()


def start_backup_job(items):
    job_id = uuid.uuid4().hex

    BACKUP_JOBS[job_id] = {
        "id": job_id,
        "items": list(items or []),
        "type_text": backup_type_text(items),
        "running": True,
        "status": "准备中",
        "filename": "",
        "size": 0,
        "size_text": "-",
        "error": "",
        "created_at": now_str(),
        "updated_at": now_str(),
    }

    th = threading.Thread(
        target=create_backup_worker,
        args=(job_id, items),
        daemon=True,
        name=f"fls-backup-{job_id[:8]}",
    )
    th.start()

    return job_id


# ============================================================
# 安全解压
# ============================================================

def _archive_member_target(base, name):
    member_name = str(name or "").replace("\\", "/")

    if (
        member_name.startswith("/")
        or member_name.startswith("//")
        or (len(member_name) >= 2 and member_name[1] == ":" and member_name[0].isalpha())
    ):
        raise RuntimeError("备份文件包含非法路径")

    target = (base / member_name).resolve()

    if target != base and not str(target).startswith(str(base) + os.sep):
        raise RuntimeError("备份文件包含非法路径")

    return target


def safe_extract_tar(tar, path):
    base = Path(path).resolve()

    for member in tar.getmembers():
        _archive_member_target(base, member.name)

        if member.issym() or member.islnk():
            raise RuntimeError("备份文件包含不安全链接")

        if not (member.isfile() or member.isdir()):
            raise RuntimeError("备份文件包含不支持的文件类型")

    try:
        tar.extractall(path, filter="data")
    except TypeError as e:
        if "filter" not in str(e):
            raise
        tar.extractall(path)


def safe_extract_zip(zip_obj, path):
    base = Path(path).resolve()

    for member in zip_obj.infolist():
        _archive_member_target(base, member.filename)

    zip_obj.extractall(path)


def extract_archive(file_path, dest_dir):
    file_path = Path(file_path)
    name = file_path.name.lower()
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if name.endswith((".tar.gz", ".tgz", ".tar")):
        mode = "r:gz" if name.endswith((".tar.gz", ".tgz")) else "r:"

        with tarfile.open(file_path, mode) as tar:
            safe_extract_tar(tar, dest_dir)

        return True

    if name.endswith(".zip"):
        with zipfile.ZipFile(file_path, "r") as z:
            safe_extract_zip(z, dest_dir)

        return True

    raise RuntimeError("不支持的备份格式，仅支持 tar.gz / tgz / tar / zip")


def find_backup_root(root):
    root = Path(root)
    candidates = []

    all_dirs = [root]

    try:
        all_dirs += [x for x in root.rglob("*") if x.is_dir()]
    except Exception:
        pass

    for p in all_dirs:
        if (p / "data").exists() or (p / "scripts").exists():
            candidates.append(p)

    if not candidates:
        return None

    candidates.sort(key=lambda x: len(str(x)))
    return candidates[0]


def find_dependency_file(extract_dir, backup_root=None):
    candidates = []
    extract_dir = Path(extract_dir)

    if backup_root:
        backup_root = Path(backup_root)
        candidates.extend([
            backup_root / "dependencies.txt",
            backup_root / "requirements.txt",
            backup_root / "data" / "dependencies.txt",
            backup_root / "data" / "requirements.txt",
        ])

    candidates.extend([
        extract_dir / "dependencies.txt",
        extract_dir / "requirements.txt",
        extract_dir / "data" / "dependencies.txt",
        extract_dir / "data" / "requirements.txt",
    ])

    for item in candidates:
        try:
            if item.exists() and item.is_file() and item.stat().st_size > 0:
                return item
        except Exception:
            pass

    try:
        for item in extract_dir.rglob("*"):
            if item.is_file() and item.name in ("dependencies.txt", "requirements.txt"):
                if item.stat().st_size > 0:
                    return item
    except Exception:
        pass

    return None


def install_dependencies(dep_file):
    dep_file = Path(dep_file)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log_file = LOG_DIR / "backup-restore-deps-{}.log".format(
        datetime.now().strftime("%Y%m%d-%H%M%S")
    )

    with open(log_file, "ab", buffering=0) as log_fp:
        header = (
            "===== 备份恢复依赖 =====\n"
            "时间: {}\n"
            "Python: {}\n"
            "依赖列表: {}\n"
            "命令: {} -m pip install -r {}\n"
            "============================================================\n"
        ).format(now_str(), sys.executable, dep_file, sys.executable, dep_file)

        log_fp.write(header.encode("utf-8"))

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(dep_file)],
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                cwd=str(BASE_DIR),
                env=os.environ.copy(),
                timeout=1800,
            )

            footer = "\n===== 依赖恢复结束：{}，退出码：{} =====\n".format(
                now_str(),
                proc.returncode,
            )

            log_fp.write(footer.encode("utf-8"))

            return proc.returncode == 0, str(log_file)

        except Exception as e:
            log_fp.write(("\n依赖恢复异常: {}\n".format(e)).encode("utf-8"))
            return False, str(log_file)


# ============================================================
# 页面
# ============================================================

def backup_rows_html():
    files = list_backup_files()

    if not files:
        return '<tr><td colspan="4">暂无备份</td></tr>'

    rows = ""

    for item in files:
        name = item.get("name", "")

        rows += f"""
<tr>
    <td>
        <b>{h(name)}</b>
    </td>
    <td>{h(item.get("size_text", "-"))}</td>
    <td>{h(item.get("mtime_text", "-"))}</td>
    <td>
        <a class="btn btn-primary" href="/backup/download/{h(name)}">下载</a>
        <button class="btn btn-red" type="button" onclick="flsDeleteBackup('{h(name)}')">删除</button>
    </td>
</tr>
"""

    return rows


# ============================================================
# API：创建 / 状态 / 列表 / 删除
# ============================================================


# ============================================================
# 下载 / 导入
# ============================================================
