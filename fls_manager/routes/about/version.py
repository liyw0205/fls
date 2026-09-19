import re

from flask import request, redirect, url_for, jsonify

from . import bp
from .helpers import (
    git_available,
    is_git_repo,
    start_about_job,
    refresh_log_worker,
    update_version_worker,
    get_version_info,
)
from .state import set_update_log_state, update_log_state
from ...ui.layout import layout
from ...ui.components import page_header_card
from ...utils import h
from ...paths import BASE_DIR


@bp.route("/about/refresh-log", methods=["POST"])
def about_refresh_log():
    if not git_available():
        body = page_header_card(
            "刷新失败",
            help_html='<span style="color:#dc2626;">系统未安装 git。下一步：安装 git 后返回关于页重试。</span>',
            actions_html='<a class="btn btn-gray" href="/about">返回关于页</a>',
        )
        return layout("刷新失败", "about", body)

    if not is_git_repo():
        body = page_header_card(
            "刷新失败",
            help_html=(
                '<span style="color:#dc2626;">'
                f"当前目录不是 Git 仓库：{h(BASE_DIR)}。下一步：切换到 Git 仓库后重试"
                "</span>"
            ),
            actions_html='<a class="btn btn-gray" href="/about">返回关于页</a>',
        )
        return layout("刷新失败", "about", body)

    # Keep the manual endpoint's explicit job creation behavior. The automatic
    # first-open path uses start_refresh_log_job() to coalesce concurrent visits.
    set_update_log_state(checking=True, error="")
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


@bp.route("/api/about/update-info")
def about_update_info():
    wait_for_completion = request.args.get("wait") == "1"
    state = update_log_state(
        wait_for_completion=wait_for_completion,
        timeout=10 if wait_for_completion else 0,
    )
    include_logs = request.args.get("logs") == "1"
    logs = []

    if include_logs and not state.get("checking"):
        logs = get_version_info().get("logs") or []

    return jsonify({
        "ok": not bool(state.get("error")),
        "checking": bool(state.get("checking")),
        "available": bool(state.get("available")),
        "version": str(state.get("version") or ""),
        "current_version": str(state.get("current_version") or ""),
        "checked_at": str(state.get("checked_at") or ""),
        "error": str(state.get("error") or ""),
        "logs": logs,
    })


@bp.route("/about/update-version", methods=["POST"])
def about_update_version():
    version = request.form.get("version", "").strip()

    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", version):
        body = page_header_card(
            "更新失败",
            help_html=(
                '<span style="color:#dc2626;">'
                f"版本号非法：{h(version)}。下一步：填写 7-40 位提交哈希后重试"
                "</span>"
            ),
            actions_html='<a class="btn btn-gray" href="/about">返回关于页</a>',
        )
        return layout("更新失败", "about", body)

    if not git_available():
        body = page_header_card(
            "更新失败",
            help_html='<span style="color:#dc2626;">系统未安装 git。下一步：安装 git 后返回关于页重试。</span>',
            actions_html='<a class="btn btn-gray" href="/about">返回关于页</a>',
        )
        return layout("更新失败", "about", body)

    if not is_git_repo():
        body = page_header_card(
            "更新失败",
            help_html=(
                '<span style="color:#dc2626;">'
                f"当前目录不是 Git 仓库：{h(BASE_DIR)}。下一步：切换到 Git 仓库后重试"
                "</span>"
            ),
            actions_html='<a class="btn btn-gray" href="/about">返回关于页</a>',
        )
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
