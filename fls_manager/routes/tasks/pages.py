import uuid
from math import ceil
from urllib.parse import quote, urlparse

from flask import request, redirect, abort

from . import bp
from .forms import (
    task_form,
    parse_notify_from_form,
    parse_random_delay_from_form,
    parse_retry_from_form,
)
from .helpers import (
    parse_task_env_from_form,
    tasks_page_links,
    filter_tasks_for_page,
    sort_tasks_for_display,
    task_config_safe_path,
)
from ...models import (
    COLLECTION_MUTATION_LOCK,
    TaskSchedulerReloadError,
    load_tasks,
    save_tasks,
    mutate_tasks,
    load_collections,
    get_collection,
    get_proxy,
    normalize_task,
)
from ...notify import get_notify_item
from ...utils import h, now_str
from ...ui.layout import layout
from ...ui.components import message_card, page_header, section, data_toolbar, empty_state
from ...ui.tables import tasks_table
from ...scheduler import reload_scheduler, cron_to_trigger


SORT_OPTIONS = [
    ("default", "默认"),
    ("recent_run", "最近运行"),
    ("last_run", "最后运行"),
    ("name", "任务名"),
    ("created", "创建时间"),
]

TASK_PAGE_COLLECTION_LIMIT = 3
TASK_EDIT_FIELDS = (
    "name", "remark", "command", "cron", "config_path", "collection_id", "enabled", "env",
    "proxy_id", "notify", "random_delay", "retry",
)


class TaskCreationConflict(ValueError):
    pass


def _validate_task_form(task):
    if not task.get("name"):
        raise ValueError("任务名不能为空")
    if not task.get("command"):
        raise ValueError("命令不能为空")
    if "\x00" in task["name"] or "\x00" in task["command"]:
        raise ValueError("任务名称或命令包含 NUL 字符")
    if task.get("cron"):
        try:
            cron_to_trigger(task["cron"])
        except ValueError as exc:
            raise ValueError(f"Cron 不合法：{exc}") from exc
    if task.get("config_path"):
        task_config_safe_path(task["config_path"])
    for key, value in task.get("env", {}).items():
        if "=" in key or "\x00" in key or "\x00" in value:
            raise ValueError("任务变量无效：变量名不允许等号或 NUL，变量值不允许 NUL")
    keys = [value.strip() for value in request.form.getlist("env_key")]
    values = request.form.getlist("env_value")
    nonempty_keys = [key for key in keys if key]
    if len(nonempty_keys) != len(set(nonempty_keys)) or len(keys) != len(values):
        raise ValueError("任务变量行重复或不完整，请修正后保存")
    if any(not key and value for key, value in zip(keys, values)):
        raise ValueError("任务变量名不能为空，请修正后保存")
    if task.get("proxy_id") and not get_proxy(task["proxy_id"]):
        raise ValueError("所选代理不存在或已禁用，请重新选择")
    notify_mode = request.form.get("notify_mode", "default").strip()
    if notify_mode not in ("none", "default", "custom"):
        raise ValueError("通知模式无效，请重新选择")
    if request.form.get("random_delay_mode", "none").strip() not in ("none", "default", "custom"):
        raise ValueError("随机延迟模式无效，请重新选择")
    if notify_mode == "custom":
        ids = request.form.getlist("notify_ids")
        if not ids or any(not item_id for item_id in ids):
            raise ValueError("请选择至少一个通知渠道，或选择不通知")
        for item_id in ids:
            item = get_notify_item(item_id)
            if not item or not item.get("enabled", True):
                raise ValueError("所选通知渠道不存在或已禁用，请重新选择")


def _clean_task_back_url(value, default="/tasks"):
    value = str(value or "").strip()

    if not value or value.startswith("//"):
        return default

    parsed = urlparse(value)

    if parsed.scheme or parsed.netloc:
        return default

    if not value.startswith("/"):
        return default

    return value


def _default_task_back_url(task=None):
    collection_id = str((task or {}).get("collection_id") or "").strip()

    if collection_id:
        return f"/collections#collection-{collection_id}"

    return "/tasks"


def _task_form_back_url(task=None):
    default = _default_task_back_url(task)
    raw = request.form.get("back") if request.method == "POST" else request.args.get("back")

    return _clean_task_back_url(raw, default)


def _render_collection_cards(collections, all_tasks):
    cards = ""

    task_count_map = {}

    for task in all_tasks:
        cid = str(task.get("collection_id") or "").strip()
        if not cid:
            continue
        task_count_map[cid] = task_count_map.get(cid, 0) + 1

    for c in collections:
        cid = c.get("id", "")
        cname = str(c.get("name", "") or "").strip() or "未命名合集"
        cremark = str(c.get("remark", "") or "").strip()
        task_count = task_count_map.get(cid, 0)

        cards += f"""
<div class="fls-summary-item task-collection-preview" id="collection-{h(cid)}">
    <div class="task-collection-copy">
    <div class="fls-summary-label">{h(cname)}</div>
    <div class="help">{h(cremark or "暂无备注")}</div>
    <div>
        任务：<b>{task_count}</b>
    </div>
    </div>
    <div class="action-row">
        <a class="btn btn-blue" href="/collections#collection-{h(cid)}">查看</a>
        <a class="btn btn-orange" href="/collection/edit/{h(cid)}">编辑任务合集</a>
    </div>
</div>
"""

    if not cards:
        cards = empty_state(
            "暂无任务合集。",
            '<a class="btn btn-primary" href="/collection/new">新建任务合集</a>',
        )

    return f'<div class="fls-summary-grid task-collection-previews">{cards}</div>'


def _task_from_post(base=None):
    task = dict(base or {})
    task.update({
        "name": request.form.get("name", "").strip(),
        "remark": request.form.get("remark", "").strip(),
        "command": request.form.get("command", "").strip(),
        "cron": request.form.get("cron", "").strip(),
        "config_path": request.form.get("config_path", "").strip(),
        "collection_id": request.form.get("collection_id", "").strip(),
        "enabled": request.form.get("enabled") == "1",
        "env": parse_task_env_from_form(),
        "proxy_id": request.form.get("proxy_id", "").strip(),
        "notify": parse_notify_from_form(),
        "random_delay": parse_random_delay_from_form(),
        "retry": parse_retry_from_form(),
    })

    return task


def _task_form_error(title, back_url, message, task, status=400):
    create_id = task.get("id", "") if title == "新建任务" else ""
    if create_id:
        task = {key: value for key, value in task.items() if key != "id"}
    body = message_card(message, "error", strong=True) + task_form(
        task,
        back_url=back_url,
        create_id=create_id,
    )
    if create_id and status == 409:
        body = f'<a class="btn btn-blue" href="/task/edit/{h(create_id)}">编辑已保存任务</a>' + body

    return layout(title, "tasks", body), status


def _task_saved_reload_error(task, back_url, error):
    task_id = task["id"]
    body = message_card(
        f"任务已保存，但调度器重载失败：{error}。不要重复新建或保存，请仅重新加载调度器。",
        "error", strong=True,
    ) + f'''
<div class="action-row">
    <form class="inline-form" method="post" action="/scheduler/reload?back={h(quote(back_url, safe=''))}">
        <button class="btn btn-primary" type="submit">重新加载调度器</button>
    </form>
    <a class="btn btn-blue" href="/task/edit/{h(task_id)}?back={h(quote(back_url, safe=''))}">查看已保存任务</a>
    <a class="btn btn-gray" href="{h(back_url)}">返回任务</a>
</div>'''
    return layout("任务已保存，调度待恢复", "tasks", body), 503


@bp.route("/tasks")
def tasks_page():
    all_tasks = load_tasks()
    collections = load_collections()

    q = request.args.get("q", "").strip()
    sort = request.args.get("sort", "default").strip()
    try:
        page = max(1, int(request.args.get("page", "1") or 1))
    except (TypeError, ValueError):
        page = 1
    per_page = 20

    sort_keys = {x[0] for x in SORT_OPTIONS}
    if sort not in sort_keys:
        sort = "default"

    # 普通任务列表只显示未放入合集的任务。
    uncollected_tasks = [
        t for t in all_tasks
        if not str(t.get("collection_id") or "").strip()
    ]

    filtered_tasks = filter_tasks_for_page(uncollected_tasks, q)
    filtered_tasks = sort_tasks_for_display(filtered_tasks, sort)

    total = len(filtered_tasks)
    pages = max(1, ceil(total / per_page))
    page = min(page, pages)

    start = (page - 1) * per_page
    end = page * per_page
    show_tasks = filtered_tasks[start:end]

    page_links_html = tasks_page_links(q, page, pages, sort)

    # 任务管理页合集
    collections_sorted = sorted(
        collections,
        key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""),
        reverse=True,
    )
    collections_show = collections_sorted[:TASK_PAGE_COLLECTION_LIMIT]
    collection_cards_html = _render_collection_cards(collections_show, all_tasks)

    hidden_count = len(all_tasks) - len(uncollected_tasks)
    pinned_count = sum(1 for t in all_tasks if t.get("pinned"))

    sort_options_html = ""

    for key, text in SORT_OPTIONS:
        s = "selected" if key == sort else ""
        sort_options_html += f'<option value="{h(key)}" {s}>{h(text)}</option>'

    more_collection_tip = ""

    if len(collections_sorted) > TASK_PAGE_COLLECTION_LIMIT:
        more_collection_tip = f"""
<div class="help" style="margin-top:8px;">
    共 {len(collections_sorted)} 个任务合集，完整内容请到
    <a href="/collections">任务合集</a> 查看。
</div>
"""

    header = page_header(
        "任务管理",
        help_html=(
            f"Cron 留空表示手动任务。共 {len(all_tasks)} 个任务，其中 {hidden_count} 个已放入合集，"
            f"当前置顶 {pinned_count} 个。放入合集的任务仍会正常运行。"
        ),
        actions_html=(
            '<a class="btn btn-primary" href="/task/new">新建任务</a>'
            '<a class="btn btn-blue" href="/collection/new">新建合集</a>'
            '<a class="btn btn-gray" href="/collections">任务合集</a>'
        ),
    )
    collection_section = section(
        "任务合集",
        f'<div class="help">完整任务合集请进入“任务合集”。</div>{collection_cards_html}{more_collection_tip}',
        section_id="task-collections",
    )
    search_controls = f"""
    <div class="form-item">
        <label for="task-search">搜索任务</label>
        <input id="task-search" name="q" value="{h(q)}" placeholder="任务名 / 备注 / 命令 / Cron / 配置路径">
    </div>
    <div class="form-item">
        <label for="task-sort">排序方式</label>
        <select id="task-sort" name="sort">{sort_options_html}</select>
    </div>
    <div class="action-row">
        <button class="btn btn-primary" type="submit">查询任务</button>
        <a class="btn btn-gray" href="/tasks">重置筛选</a>
    </div>
"""
    toolbar = data_toolbar(search_controls, toolbar_id="task-filter-toolbar")
    body = f"""
{header}
{collection_section}
<form method="get" aria-label="任务查询">{toolbar}</form>
{section("任务列表", tasks_table(show_tasks), section_id="task-list")}
{page_links_html}
"""
    return layout("任务管理", "tasks", body)


@bp.route("/task/new", methods=["GET", "POST"])
def task_new():
    back_url = _task_form_back_url()

    if request.method == "POST":
        task = _task_from_post()
        collection_id = task.get("collection_id", "")
        create_id = request.form.get("create_id", "") or uuid.uuid4().hex
        try:
            if len(create_id) != 32 or uuid.UUID(hex=create_id).hex != create_id:
                raise ValueError("新建任务提交标识无效，请重新打开新建页面")
            task["id"] = create_id
            _validate_task_form(task)
        except ValueError as e:
            return _task_form_error("新建任务", back_url, str(e), task)
        except Exception as e:
            return _task_form_error("新建任务", back_url, f"任务校验失败，未保存：{e}", task, status=500)

        task.update({
            "id": create_id,
            "run_count": 0,
            "pinned": False,
            "created_at": now_str(),
            "updated_at": now_str(),
        })

        def add_task(tasks):
            _validate_task_form(task)
            existing = next((item for item in tasks if item.get("id") == create_id), None)
            if existing:
                expected = normalize_task(task)
                if any(existing.get(key) != expected.get(key) for key in TASK_EDIT_FIELDS):
                    raise TaskCreationConflict("该提交已保存，内容已变更。请返回任务列表编辑已有任务，不会重复新建。")
                return False
            tasks.append(task)
            return True

        with COLLECTION_MUTATION_LOCK:
            if collection_id and not get_collection(collection_id):
                return _task_form_error("新建任务", back_url, "合集不存在", task)
            try:
                _tasks, changed = mutate_tasks(add_task, reload_scheduler)
                if not changed:
                    try:
                        reload_scheduler()
                    except Exception as e:
                        raise TaskSchedulerReloadError(str(e)) from e
            except TaskSchedulerReloadError as e:
                return _task_saved_reload_error(task, back_url, e)
            except TaskCreationConflict as e:
                return _task_form_error("新建任务", back_url, str(e), task, status=409)
            except ValueError as e:
                return _task_form_error("新建任务", back_url, str(e), task)
            except Exception as e:
                return _task_form_error(
                    "新建任务",
                    back_url,
                    f"任务保存失败：{e}",
                    task,
                    status=500,
                )

        return redirect(back_url)

    return layout("新建任务", "tasks", task_form(back_url=back_url))


@bp.route("/task/edit/<task_id>", methods=["GET", "POST"])
def task_edit(task_id):
    try:
        tasks = load_tasks()
    except Exception as e:
        draft = _task_from_post({"id": task_id}) if request.method == "POST" else {"id": task_id}
        return _task_form_error("编辑任务", _task_form_back_url(), f"任务文件读取失败，未保存：{e}。请修复文件或恢复备份后重试。", draft, status=500)
    task = None

    for t in tasks:
        if t.get("id") == task_id:
            task = t
            break

    if not task:
        abort(404)

    back_url = _task_form_back_url(task)

    if request.method == "POST":
        draft = _task_from_post(task)
        collection_id = draft.get("collection_id", "")
        try:
            _validate_task_form(draft)
        except ValueError as e:
            return _task_form_error("编辑任务", back_url, str(e), draft)
        except Exception as e:
            return _task_form_error("编辑任务", back_url, f"任务校验失败，未保存：{e}", draft, status=500)

        def update_task(tasks):
            target = next((item for item in tasks if item.get("id") == task_id), None)
            if target is None:
                return False
            _validate_task_form(draft)

            target.update({
                "name": draft.get("name", ""),
                "remark": draft.get("remark", ""),
                "command": draft.get("command", ""),
                "cron": draft.get("cron", ""),
                "config_path": draft.get("config_path", ""),
                "collection_id": draft.get("collection_id", ""),
                "enabled": draft.get("enabled", True),
                "env": draft.get("env", {}),
                "proxy_id": draft.get("proxy_id", ""),
                "notify": draft.get("notify"),
                "random_delay": draft.get("random_delay"),
                "retry": draft.get("retry"),
            })
            target["updated_at"] = now_str()
            target.setdefault("run_count", 0)
            target.setdefault("pinned", False)
            return True

        with COLLECTION_MUTATION_LOCK:
            if collection_id and not get_collection(collection_id):
                return _task_form_error("编辑任务", back_url, "合集不存在", draft)
            try:
                _tasks, changed = mutate_tasks(update_task, reload_scheduler)
            except TaskSchedulerReloadError as e:
                return _task_saved_reload_error(draft, back_url, e)
            except ValueError as e:
                return _task_form_error("编辑任务", back_url, str(e), draft)
            except Exception as e:
                return _task_form_error(
                    "编辑任务",
                    back_url,
                    f"任务保存失败：{e}",
                    draft,
                    status=500,
                )
        if not changed:
            abort(404)

        return redirect(back_url)

    return layout("编辑任务", "tasks", task_form(task, back_url=back_url))
