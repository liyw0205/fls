import os
import sys
import shutil
import tarfile
import zipfile
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime

from flask import Blueprint, request, Response, redirect, url_for

from ..paths import BASE_DIR, DATA_DIR, SCRIPT_DIR, LOG_DIR
from ..ui.layout import layout
from ..utils import h, now_str
from ..scheduler import reload_scheduler

bp = Blueprint("backup", __name__)


def safe_extract_tar(tar, path):
    base = Path(path).resolve()

    for member in tar.getmembers():
        target = (base / member.name).resolve()
        if target != base and not str(target).startswith(str(base) + os.sep):
            raise RuntimeError("备份文件包含非法路径")

    tar.extractall(path)


def safe_extract_zip(zip_obj, path):
    base = Path(path).resolve()

    for member in zip_obj.infolist():
        target = (base / member.filename).resolve()
        if target != base and not str(target).startswith(str(base) + os.sep):
            raise RuntimeError("备份文件包含非法路径")

    zip_obj.extractall(path)


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
                timeout=1800
            )

            footer = "\n===== 依赖恢复结束：{}，退出码：{} =====\n".format(
                now_str(),
                proc.returncode
            )
            log_fp.write(footer.encode("utf-8"))

            return proc.returncode == 0, str(log_file)

        except Exception as e:
            log_fp.write(("\n依赖恢复异常: {}\n".format(e)).encode("utf-8"))
            return False, str(log_file)


@bp.route("/backup")
def backup_page():
    body = """
<div class="card">
    <div class="card-title">导出面板数据</div>
    <div class="help">
        会导出 data、scripts 目录中的任务配置、代理配置、全局变量和脚本。<br>
        同时会自动导出当前 Python 依赖列表 dependencies.txt。
    </div>
    <br>
    <a class="btn btn-primary" href="/backup/export">导出备份</a>
</div>

<div class="card">
    <div class="card-title">导入面板数据</div>
    <form method="post" action="/backup/import" enctype="multipart/form-data">
        <input type="file" name="file" accept=".tar.gz,.tgz,.tar,.zip">
        <br><br>
        <label>
            <input type="checkbox" name="restore_deps" value="1" style="width:auto;">
            如果备份中包含依赖列表，同时恢复 Python 依赖
        </label>
        <div class="help">
            导入会覆盖当前 data 和 scripts。<br>
            支持 .tar.gz / .tgz / .tar / .zip。
        </div>
        <br>
        <button class="btn btn-orange" type="submit" onclick="return confirm('导入会覆盖当前 data 和 scripts，确定继续吗？')">导入备份</button>
    </form>
</div>
"""
    return layout("备份恢复", "backup", body)


@bp.route("/backup/export")
def backup_export():
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz")
    tmp.close()

    deps_tmp = None

    try:
        deps_tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        deps_tmp.close()

        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "freeze"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=120
            )

            deps_text = result.stdout or ""
            if result.returncode != 0:
                deps_text = "# pip freeze 执行失败，以下为输出：\n" + deps_text

            Path(deps_tmp.name).write_text(deps_text, encoding="utf-8")

        except Exception as e:
            Path(deps_tmp.name).write_text(
                "# pip freeze 执行失败：{}\n".format(e),
                encoding="utf-8"
            )

        with tarfile.open(tmp.name, "w:gz") as tar:
            if DATA_DIR.exists():
                tar.add(DATA_DIR, arcname="data")

            if SCRIPT_DIR.exists():
                tar.add(SCRIPT_DIR, arcname="scripts")

            if deps_tmp and Path(deps_tmp.name).exists():
                tar.add(deps_tmp.name, arcname="dependencies.txt")
                tar.add(deps_tmp.name, arcname="data/dependencies.txt")

    finally:
        if deps_tmp:
            try:
                os.remove(deps_tmp.name)
            except Exception:
                pass

    filename = f"fls-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"

    def generate():
        try:
            with open(tmp.name, "rb") as f:
                while True:
                    data = f.read(1024 * 1024)
                    if not data:
                        break
                    yield data
        finally:
            try:
                os.remove(tmp.name)
            except Exception:
                pass

    return Response(
        generate(),
        mimetype="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@bp.route("/backup/import", methods=["POST"])
def backup_import():
    f = request.files.get("file")

    if not f:
        return "未上传文件", 400

    restore_deps = request.form.get("restore_deps") == "1"
    tmp_dir = tempfile.mkdtemp()

    try:
        original = os.path.basename(f.filename or "backup")
        backup_file = Path(tmp_dir) / original
        f.save(str(backup_file))

        extract_dir = Path(tmp_dir) / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)

        extract_archive(backup_file, extract_dir)

        root = find_backup_root(extract_dir)
        if not root:
            raise RuntimeError("未在备份中找到 data 或 scripts 目录")

        dep_file = find_dependency_file(extract_dir, root)

        data_src = root / "data"
        scripts_src = root / "scripts"

        if data_src.exists():
            if DATA_DIR.exists():
                shutil.rmtree(DATA_DIR)
            shutil.copytree(data_src, DATA_DIR)

        if scripts_src.exists():
            if SCRIPT_DIR.exists():
                shutil.rmtree(SCRIPT_DIR)
            shutil.copytree(scripts_src, SCRIPT_DIR)

        deps_msg = ""
        deps_log = ""

        if restore_deps:
            if dep_file:
                ok, deps_log = install_dependencies(dep_file)
                deps_msg = "依赖恢复成功" if ok else "依赖恢复失败，请查看日志"
            else:
                deps_msg = "已勾选恢复依赖，但备份中没有 dependencies.txt / requirements.txt"

        reload_scheduler()

        if restore_deps:
            body = f"""
<div class="card">
    <div class="card-title">备份导入完成</div>
    <div class="help">
        data / scripts 已恢复。<br>
        依赖恢复：{h(deps_msg)}<br>
        日志：{h(deps_log or "-")}
    </div>
    <br>
    <a class="btn btn-primary" href="/backup">返回备份恢复</a>
    <a class="btn btn-gray" href="/logs">查看日志</a>
</div>
"""
            return layout("备份导入完成", "backup", body)

        return redirect(url_for("backup.backup_page"))

    except Exception as e:
        return f"备份导入失败：{h(e)}", 400

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
