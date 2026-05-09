import re

from flask import request, redirect, url_for

from . import bp
from .helpers import (
    git_available,
    is_git_repo,
    start_about_job,
    refresh_log_worker,
    update_version_worker,
)
from ...ui.layout import layout
from ...utils import h
from ...paths import BASE_DIR


@bp.route("/about/refresh-log", methods=["POST"])
def about_refresh_log():
    if not git_available():
        body = """
<div class="card">
    <div class="card-title">刷新失败</div>
    <div class="help" style="color:#dc2626;">
        系统未安装 git。
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
</div>
"""
        return layout("刷新失败", "about", body)

    if not is_git_repo():
        body = f"""
<div class="card">
    <div class="card-title">刷新失败</div>
    <div class="help" style="color:#dc2626;">
        当前目录不是 Git 仓库：{h(BASE_DIR)}
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
</div>
"""
        return layout("刷新失败", "about", body)

    job_id = start_about_job(
        action="refresh-log",
        title="刷新更新日志",
        target=refresh_log_worker,
    )

    return redirect(
        url_for(
            "about.about_job_log",
            job_id=job_id,
            back="/about",
        )
    )


@bp.route("/about/update-version", methods=["POST"])
def about_update_version():
    version = request.form.get("version", "").strip()

    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", version):
        body = f"""
<div class="card">
    <div class="card-title">更新失败</div>
    <div class="help" style="color:#dc2626;">
        版本号非法：{h(version)}
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
</div>
"""
        return layout("更新失败", "about", body)

    if not git_available():
        body = """
<div class="card">
    <div class="card-title">更新失败</div>
    <div class="help" style="color:#dc2626;">
        系统未安装 git。
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
</div>
"""
        return layout("更新失败", "about", body)

    if not is_git_repo():
        body = f"""
<div class="card">
    <div class="card-title">更新失败</div>
    <div class="help" style="color:#dc2626;">
        当前目录不是 Git 仓库：{h(BASE_DIR)}
    </div>
    <br>
    <a class="btn btn-gray" href="/about">返回关于页</a>
</div>
"""
        return layout("更新失败", "about", body)

    job_id = start_about_job(
        action="update-version",
        title=f"更新版本 {version[:12]}",
        target=update_version_worker,
        args=(version,),
    )

    return redirect(
        url_for(
            "about.about_job_log",
            job_id=job_id,
            back="/about",
        )
    )