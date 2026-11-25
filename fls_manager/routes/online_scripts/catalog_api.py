import ipaddress
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from ._common import *
from ...models import load_proxies
from ...sensitive import is_sensitive_key, mask_sensitive_text, mask_sensitive_url
from ...online_scripts.tasks import online_script_task_crons, online_task_cron_vars, validate_online_task_import


DOC_MAX_BYTES = 1024 * 1024
DOC_REDIRECT_LIMIT = 4
CATALOG_DEFAULT_LIMIT = 20
CATALOG_MAX_LIMIT = 50
TASK_DEFAULT_LIMIT = 20
TASK_MAX_LIMIT = 100


def _no_store_json(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _positive_query_int(name, default, maximum):
    raw = request.args.get(name, str(default))
    if not re.fullmatch(r"[0-9]+", str(raw)):
        raise ValueError(f"{name} 必须为正整数")
    value = int(raw)
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} 必须在 1 到 {maximum} 之间")
    return value


def _safe_source_url(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
            return ""
        hostname = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        netloc = f"{hostname}:{parts.port}" if parts.port else hostname
        safe = urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, parts.fragment))
        return mask_sensitive_url(safe)[:2000]
    except (TypeError, ValueError):
        return ""


def _task_dto(task, index, script, *, include_validation=False):
    env = online_task_cron_vars(task)
    dto = {
        "index": index,
        "name": str(task.get("name") or f"任务{index}")[:300],
        "cron": str(task.get("cron") or "")[:300],
        "command": mask_sensitive_text(task.get("command") or "")[:4000],
        "remark": mask_sensitive_text(task.get("remark") or "")[:2000],
        "config_path": str(task.get("config_path") or "")[:1000],
        "enabled": bool(task.get("enabled", True)),
        "variable_names": sorted(str(key)[:200] for key in env),
        "variable_count": len(env),
        "sensitive_variables": sorted(str(key)[:200] for key in env if is_sensitive_key(key)),
    }
    if include_validation:
        try:
            validate_online_task_import(script, [index])
            dto.update(valid=True, validation_error="")
        except Exception as exc:
            dto.update(valid=False, validation_error=mask_sensitive_text(exc)[:500])
    return dto


def _script_dto(item, *, include_task_details=False):
    tasks = online_script_task_crons(item)
    description = str(item.get("description") or item.get("remark") or "")
    tags = item.get("tags")
    dto = {
        "id": str(item.get("id") or "")[:200],
        "name": str(item.get("name") or "")[:500],
        "type": str(item.get("type") or "")[:20],
        "link_name": str(item.get("link_name") or "")[:500],
        "source_url": _safe_source_url(item.get("link")),
        "doc_url": _safe_source_url(item.get("doc_link")),
        "description": mask_sensitive_text(description)[:4000],
        "version": str(item.get("version") or "")[:200],
        "author": str(item.get("author") or "")[:300],
        "category": str(item.get("category") or "")[:300],
        "tags": [str(tag)[:100] for tag in tags[:30] if isinstance(tag, (str, int, float))]
        if isinstance(tags, list) else [],
        "has_install": script_has_install(item),
        "has_doc": bool(str(item.get("doc_link") or "").strip()),
        "has_tasks": bool(tasks),
        "task_count": len(tasks),
        "has_task_source": bool(str(item.get("task_link") or "").strip()),
    }
    if include_task_details:
        dto["tasks"] = [_task_dto(task, index, item) for index, task in enumerate(tasks, 1)]
    return dto


def _external_https_url(value):
    raw = str(value or "").strip()
    try:
        parts = urlsplit(raw)
        host = parts.hostname
        if (
            parts.scheme.lower() != "https"
            or not host
            or parts.username is not None
            or parts.password is not None
            or any(ord(char) < 32 for char in raw)
        ):
            return ""
        if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(".localhost"):
            return ""
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            return ""
        port = parts.port
        hostname = f"[{host}]" if ":" in host else host
        netloc = hostname if port in (None, 443) else f"{hostname}:{port}"
        return urlunsplit(("https", netloc, parts.path, parts.query, ""))
    except (TypeError, ValueError):
        return ""


def _fetch_document(url, proxy_id):
    target = github_proxy_url(url, proxy_id, verify=True)
    if not _external_https_url(target):
        raise ValueError("文档地址必须是有效的 HTTPS 外部地址")
    proxies = requests_proxy_dict(proxy_id)
    response = None
    try:
        for redirect_count in range(DOC_REDIRECT_LIMIT + 1):
            response = requests.get(
                target,
                timeout=(5, 25),
                stream=True,
                allow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
                proxies=proxies,
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location") or ""
                next_url = _external_https_url(urljoin(target, location))
                response.close()
                response = None
                if not next_url or redirect_count >= DOC_REDIRECT_LIMIT:
                    raise ValueError("文档重定向地址无效或超过限制")
                target = next_url
                continue
            response.raise_for_status()
            length = response.headers.get("Content-Length", "")
            if length.isdecimal() and int(length) > DOC_MAX_BYTES:
                raise OverflowError("文档超过 1 MiB 限制")
            chunks = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > DOC_MAX_BYTES:
                    raise OverflowError("文档超过 1 MiB 限制")
                chunks.append(chunk)
            raw = b"".join(chunks)
            encoding = response.encoding or "utf-8"
            content_type = str(response.headers.get("Content-Type") or "").lower()
            text = raw.decode(encoding, errors="replace")
            if doc_response_is_html(response, text):
                document_format = "html"
            elif (
                doc_url_looks_markdown(url)
                or "markdown" in content_type
                or doc_content_looks_markdown(text)
            ):
                document_format = "markdown"
            elif doc_response_is_text(response):
                document_format = "text"
            else:
                document_format = "unsupported"
                text = ""
            return {
                "format": document_format,
                "content": text,
                "content_type": content_type[:200],
                "size_bytes": len(raw),
                "source_url": _safe_source_url(url),
            }
        raise ValueError("文档重定向次数超限")
    finally:
        if response is not None:
            response.close()


@bp.route("/api/online-scripts/catalog")
def api_online_scripts_catalog():
    try:
        page = _positive_query_int("page", 1, 100000)
        limit = _positive_query_int("limit", CATALOG_DEFAULT_LIMIT, CATALOG_MAX_LIMIT)
    except ValueError as exc:
        return _no_store_json({"ok": False, "msg": str(exc)}, 400)
    query = str(request.args.get("q") or "").strip()
    if len(query) > 200 or any(ord(char) < 32 for char in query):
        return _no_store_json({"ok": False, "msg": "搜索内容无效"}, 400)
    try:
        scripts = load_online_script_cache()
        filtered = filter_online_scripts_for_page(scripts, query)
        total = len(filtered)
        pages = max(1, (total + limit - 1) // limit)
        if page > pages:
            page = pages
        start = (page - 1) * limit
        items = [_script_dto(item) for item in filtered[start:start + limit]]
    except Exception:
        return _no_store_json({"ok": False, "msg": "在线脚本目录无法读取"}, 500)
    return _no_store_json({
        "ok": True,
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": pages,
        "has_more": page < pages,
    })


@bp.route("/api/online-scripts/detail/<script_id>")
def api_online_scripts_detail(script_id):
    try:
        item = get_online_script(script_id)
    except Exception:
        return _no_store_json({"ok": False, "msg": "在线脚本目录无法读取"}, 500)
    if not item:
        return _no_store_json({"ok": False, "msg": "在线脚本不存在"}, 404)
    return _no_store_json({"ok": True, "item": _script_dto(item, include_task_details=True)})


@bp.route("/api/online-scripts/install-options/<script_id>")
def api_online_scripts_install_options(script_id):
    try:
        item = get_online_script(script_id)
    except Exception:
        return _no_store_json({"ok": False, "msg": "在线脚本目录无法读取"}, 500)
    if not item:
        return _no_store_json({"ok": False, "msg": "在线脚本不存在"}, 404)
    try:
        page = _positive_query_int("page", 1, 100000)
        limit = _positive_query_int("limit", TASK_DEFAULT_LIMIT, TASK_MAX_LIMIT)
    except ValueError as exc:
        return _no_store_json({"ok": False, "msg": str(exc)}, 400)
    query = str(request.args.get("q") or "").strip().lower()
    if len(query) > 200 or any(ord(char) < 32 for char in query):
        return _no_store_json({"ok": False, "msg": "任务搜索内容无效"}, 400)
    tasks = online_script_task_crons(item)
    indexed = list(enumerate(tasks, 1))
    if query:
        indexed = [
            (index, task) for index, task in indexed
            if query in "\n".join((
                str(index), str(task.get("name") or ""), str(task.get("cron") or ""),
                mask_sensitive_text(task.get("command") or ""),
                mask_sensitive_text(task.get("remark") or ""),
                str(task.get("config_path") or ""),
                "\n".join(str(key) for key in online_task_cron_vars(task)),
            )).lower()
        ]
    total = len(indexed)
    pages = max(1, (total + limit - 1) // limit)
    if page > pages:
        page = pages
    start = (page - 1) * limit
    try:
        proxies = [
            {"id": str(proxy.get("id") or ""), "name": str(proxy.get("name") or "未命名代理"),
             "type": str(proxy.get("type") or "")}
            for proxy in load_proxies()
            if proxy.get("enabled", True) and proxy.get("id")
        ]
    except Exception:
        return _no_store_json({"ok": False, "msg": "代理配置无法读取"}, 500)
    return _no_store_json({
        "ok": True,
        "script_id": script_id,
        "items": [
            _task_dto(task, index, item, include_validation=True)
            for index, task in indexed[start:start + limit]
        ],
        "total": total,
        "page": page,
        "limit": limit,
        "pages": pages,
        "has_more": page < pages,
        "proxies": proxies,
        "default_import_task": False,
        "default_enable_task": False,
    })


@bp.route("/api/online-scripts/doc/<script_id>")
def api_online_script_doc(script_id):
    try:
        item = get_online_script(script_id)
    except Exception:
        return _no_store_json({"ok": False, "msg": "在线脚本目录无法读取"}, 500)
    if not item:
        return _no_store_json({"ok": False, "msg": "在线脚本不存在"}, 404)
    source_url = str(item.get("doc_link") or "").strip()
    safe_url = _external_https_url(source_url)
    if not safe_url:
        return _no_store_json({"ok": False, "msg": "文档地址必须是 HTTPS 外部地址"}, 400)
    proxy_id = str(request.args.get("proxy_id") or "").strip()
    if proxy_id:
        try:
            validate_proxy_id(proxy_id)
        except ValueError as exc:
            return _no_store_json({"ok": False, "msg": str(exc)}, 400)
        except Exception:
            return _no_store_json({"ok": False, "msg": "代理配置无法读取"}, 500)
    try:
        document = _fetch_document(safe_url, proxy_id)
    except OverflowError as exc:
        return _no_store_json({"ok": False, "msg": str(exc)}, 413)
    except ValueError as exc:
        return _no_store_json({"ok": False, "msg": str(exc)}, 400)
    except Exception:
        return _no_store_json({"ok": False, "msg": "文档获取失败，请检查文档地址和网络后重试"}, 502)
    return _no_store_json({"ok": True, **document})
