import time
import re
import shutil
import subprocess
import threading
import uuid
from .models import load_proxies
from .paths import PROXY_DIAGNOSTICS_FILE
from .storage import read_json, write_json
from .utils import h, LazyModule, now_str

requests = LazyModule("requests")


GITHUB_QUALITY_RAW_URL = "https://github.com/liyw0205/fls-scripts/raw/refs/heads/main/index.json"
GITHUB_QUALITY_REPO_URL = "https://github.com/liyw0205/fls-scripts.git"

_GITHUB_PROXY_HEALTH_CACHE = {}
_GITHUB_PROXY_HEALTH_TTL = 60

PROXY_DIAGNOSTIC_LIMIT = 200
PROXY_DIAGNOSTIC_CONTENT_LIMIT = 12000
PROXY_DIAGNOSTIC_URL_LIMIT = 500
PROXY_DIAGNOSTIC_ITEM_LIMIT = 2000
PROXY_DIAGNOSTIC_STATUSES = ("running", "succeeded", "failed", "unknown")
PROXY_DIAGNOSTIC_KINDS = ("test", "quality")
PROXY_DIAGNOSTICS_LOCK = threading.RLock()
PROXY_ACTIVE_DIAGNOSTICS = set()


def _diagnostic_text(value, default=""):
    if value is None:
        return default
    return str(value)


def _restart_recovery_message(previous):
    recovery = "面板重启后无法确认代理检测结果，请检查代理并重新检测"
    suffix = f"；{recovery}"
    previous = _diagnostic_text(previous).strip()
    if not previous:
        return recovery
    return previous[:max(0, PROXY_DIAGNOSTIC_CONTENT_LIMIT - len(suffix))] + suffix


def _diagnostic_items(raw):
    if not isinstance(raw, list):
        return [], True

    items = []
    changed = False
    for item in raw[:20]:
        if not isinstance(item, dict):
            changed = True
            continue
        normalized = dict(item)
        for key, limit in (
            ("url", PROXY_DIAGNOSTIC_URL_LIMIT),
            ("status_code", 100),
            ("elapsed", PROXY_DIAGNOSTIC_ITEM_LIMIT),
        ):
            value = normalized.get(key)
            if isinstance(value, str) and len(value) > limit:
                normalized[key] = value[:limit]
                changed = True
        if normalized != item:
            changed = True
        items.append(normalized)
    if len(raw) > 20:
        changed = True
    return items, changed


def _diagnostic_rows(raw):
    rows = raw if isinstance(raw, list) else []
    normalized = []
    changed = not isinstance(raw, list)

    for row in rows:
        if not isinstance(row, dict):
            changed = True
            continue

        diagnostic_id = _diagnostic_text(row.get("id")).strip()
        if not diagnostic_id:
            changed = True
            continue

        status = _diagnostic_text(row.get("status"), "unknown").strip()
        if status not in PROXY_DIAGNOSTIC_STATUSES:
            status = "unknown"
            changed = True

        if status == "running" and diagnostic_id not in PROXY_ACTIVE_DIAGNOSTICS:
            status = "unknown"
            previous = _diagnostic_text(row.get("error") or row.get("message")).strip()
            row = dict(row)
            row["error"] = _restart_recovery_message(previous)
            changed = True

        kind = _diagnostic_text(row.get("kind"), "quality").strip()
        if kind not in PROXY_DIAGNOSTIC_KINDS:
            kind = "quality"
            changed = True

        urls = row.get("urls")
        if not isinstance(urls, list):
            urls = []
            changed = True
        urls = [_diagnostic_text(url)[:PROXY_DIAGNOSTIC_URL_LIMIT] for url in urls[:20]]

        items, items_changed = _diagnostic_items(row.get("items"))
        changed = changed or items_changed

        try:
            attempt = max(1, int(row.get("attempt", 1) or 1))
        except Exception:
            attempt = 1
            changed = True

        error = _diagnostic_text(row.get("error"))[:PROXY_DIAGNOSTIC_CONTENT_LIMIT]
        message = _diagnostic_text(row.get("message"))[:PROXY_DIAGNOSTIC_CONTENT_LIMIT]
        normalized_row = {
            "id": diagnostic_id,
            "proxy_id": _diagnostic_text(row.get("proxy_id")).strip(),
            "proxy_name": _diagnostic_text(row.get("proxy_name")).strip(),
            "proxy_type": _diagnostic_text(row.get("proxy_type")).strip(),
            "kind": kind,
            "urls": urls,
            "items": items,
            "status": status,
            "attempt": attempt,
            "error": error,
            "message": message,
            "created_at": _diagnostic_text(row.get("created_at")),
            "updated_at": _diagnostic_text(row.get("updated_at")),
            "retry_of": _diagnostic_text(row.get("retry_of")),
        }
        if normalized_row != row:
            changed = True
        normalized.append(normalized_row)

    limited = normalized[:PROXY_DIAGNOSTIC_LIMIT]
    if limited != normalized:
        changed = True
    return limited, changed


def load_proxy_diagnostics(limit=None, status="", proxy_id=""):
    with PROXY_DIAGNOSTICS_LOCK:
        raw = read_json(PROXY_DIAGNOSTICS_FILE, [])
        rows, changed = _diagnostic_rows(raw)
        if changed:
            write_json(PROXY_DIAGNOSTICS_FILE, rows)

        status = _diagnostic_text(status).strip()
        proxy_id = _diagnostic_text(proxy_id).strip()
        if status in PROXY_DIAGNOSTIC_STATUSES:
            rows = [row for row in rows if row.get("status") == status]
        if proxy_id:
            rows = [row for row in rows if row.get("proxy_id") == proxy_id]

        if limit is None:
            return rows
        try:
            limit = int(limit)
        except Exception:
            limit = 50
        return rows[:max(1, min(100, limit))]


def get_proxy_diagnostic(diagnostic_id):
    diagnostic_id = _diagnostic_text(diagnostic_id).strip()
    if not diagnostic_id:
        return None
    for row in load_proxy_diagnostics():
        if row.get("id") == diagnostic_id:
            return row
    return None


def create_proxy_diagnostic(proxy, kind, urls=None, retry_of=""):
    proxy = proxy if isinstance(proxy, dict) else {}
    kind = kind if kind in PROXY_DIAGNOSTIC_KINDS else "quality"
    urls = [_diagnostic_text(url)[:PROXY_DIAGNOSTIC_URL_LIMIT] for url in (urls or [])[:20]]
    now = now_str()
    row = {
        "id": uuid.uuid4().hex,
        "proxy_id": _diagnostic_text(proxy.get("id")).strip(),
        "proxy_name": _diagnostic_text(proxy.get("name"), "未命名代理").strip() or "未命名代理",
        "proxy_type": _diagnostic_text(proxy.get("type")).strip(),
        "kind": kind,
        "urls": urls,
        "items": [],
        "status": "running",
        "attempt": 1,
        "error": "",
        "message": "检测中",
        "created_at": now,
        "updated_at": now,
        "retry_of": _diagnostic_text(retry_of).strip(),
    }
    with PROXY_DIAGNOSTICS_LOCK:
        rows, _changed = _diagnostic_rows(read_json(PROXY_DIAGNOSTICS_FILE, []))
        rows.insert(0, row)
        write_json(PROXY_DIAGNOSTICS_FILE, rows[:PROXY_DIAGNOSTIC_LIMIT])
        PROXY_ACTIVE_DIAGNOSTICS.add(row["id"])
    return row["id"]


def finish_proxy_diagnostic(diagnostic_id, status, items=None, error="", message=""):
    diagnostic_id = _diagnostic_text(diagnostic_id).strip()
    status = status if status in PROXY_DIAGNOSTIC_STATUSES else "failed"
    items, _items_changed = _diagnostic_items(items)
    with PROXY_DIAGNOSTICS_LOCK:
        rows, _changed = _diagnostic_rows(read_json(PROXY_DIAGNOSTICS_FILE, []))
        target = next((row for row in rows if row.get("id") == diagnostic_id), None)
        if target is None:
            PROXY_ACTIVE_DIAGNOSTICS.discard(diagnostic_id)
            return False
        target["status"] = status
        target["items"] = items
        target["error"] = _diagnostic_text(error)[:PROXY_DIAGNOSTIC_CONTENT_LIMIT]
        target["message"] = _diagnostic_text(message)[:PROXY_DIAGNOSTIC_CONTENT_LIMIT]
        target["updated_at"] = now_str()
        try:
            write_json(PROXY_DIAGNOSTICS_FILE, rows[:PROXY_DIAGNOSTIC_LIMIT])
        finally:
            PROXY_ACTIVE_DIAGNOSTICS.discard(diagnostic_id)
    return True


def _claim_proxy_diagnostic(diagnostic_id):
    diagnostic_id = _diagnostic_text(diagnostic_id).strip()
    with PROXY_DIAGNOSTICS_LOCK:
        rows, _changed = _diagnostic_rows(read_json(PROXY_DIAGNOSTICS_FILE, []))
        target = next((row for row in rows if row.get("id") == diagnostic_id), None)
        if target is None:
            return None, "代理诊断不存在"
        if target.get("status") == "running":
            return None, "代理诊断正在检测"
        if target.get("status") == "succeeded":
            return None, "代理诊断已成功，无需重试"
        target["status"] = "running"
        target["message"] = "重新检测中"
        target["error"] = ""
        target["attempt"] = max(1, int(target.get("attempt", 1) or 1)) + 1
        target["updated_at"] = now_str()
        write_json(PROXY_DIAGNOSTICS_FILE, rows[:PROXY_DIAGNOSTIC_LIMIT])
        PROXY_ACTIVE_DIAGNOSTICS.add(diagnostic_id)
        return dict(target), ""


def retry_proxy_diagnostic(diagnostic_id):
    row, error = _claim_proxy_diagnostic(diagnostic_id)
    if row is None:
        return False, error, None
    proxy = get_proxy_for_test(row.get("proxy_id"))
    if proxy is None:
        message = "代理不存在或已删除"
        finish_proxy_diagnostic(row["id"], "failed", row.get("items"), message, message)
        return False, message, get_proxy_diagnostic(row["id"])
    if not proxy.get("enabled", True):
        message = "代理已停用"
        finish_proxy_diagnostic(row["id"], "failed", row.get("items"), message, message)
        return False, message, get_proxy_diagnostic(row["id"])
    try:
        if row.get("kind") == "test":
            result = test_proxy_object(proxy, (row.get("urls") or ["https://www.baidu.com"])[0])
            items = [{"url": (row.get("urls") or ["https://www.baidu.com"])[0], "ok": True, "status_code": result.get("status_code", "-"), "elapsed": f"{result.get('elapsed_ms', '-')} ms"}]
        else:
            items = quality_proxy_object(proxy, row.get("urls"))
        ok = bool(items) and all(bool(item.get("ok")) for item in items)
        status = "succeeded" if ok else "failed"
        message = "检测完成" if ok else "部分检测失败"
        finish_proxy_diagnostic(row["id"], status, items, "" if ok else message, message)
    except Exception as exc:
        message = str(exc)
        finish_proxy_diagnostic(row["id"], "failed", [], message, message)
        return False, message, get_proxy_diagnostic(row["id"])
    return status == "succeeded", message, get_proxy_diagnostic(row["id"])


def get_proxy(proxy_id):
    if not proxy_id:
        return None

    for proxy in load_proxies():
        if proxy.get("id") == proxy_id:
            if proxy.get("enabled", True) is False:
                return None
            return proxy

    return None


def get_proxy_for_test(proxy_id):
    if not proxy_id:
        return None

    for proxy in load_proxies():
        if proxy.get("id") == proxy_id:
            return proxy

    return None


def build_proxy_url(proxy):
    if not proxy:
        return ""

    ptype = proxy.get("type", "http")

    if ptype == "github":
        return str(proxy.get("url", "") or "").strip().rstrip("/")

    host = str(proxy.get("host", "") or "").strip()
    port = str(proxy.get("port", "") or "").strip()
    username = str(proxy.get("username", "") or "").strip()
    password = str(proxy.get("password", "") or "").strip()

    if not host or not port:
        return ""

    auth = ""

    if username:
        auth = username

        if password:
            auth += f":{password}"

        auth += "@"

    return f"{ptype}://{auth}{host}:{port}"


def requests_proxy_dict_from_proxy(proxy):
    if not proxy or proxy.get("type") == "github":
        return None

    proxy_url = build_proxy_url(proxy)

    if not proxy_url:
        return None

    return {
        "http": proxy_url,
        "https": proxy_url,
    }


def requests_proxy_dict(proxy_id):
    return requests_proxy_dict_from_proxy(get_proxy(proxy_id))


def apply_proxy_env(env, proxy_id):
    proxy = get_proxy(proxy_id)

    if not proxy or proxy.get("type") == "github":
        return env

    proxy_url = build_proxy_url(proxy)

    if not proxy_url:
        return env

    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url
    env["http_proxy"] = proxy_url
    env["https_proxy"] = proxy_url

    if proxy_url.startswith("socks"):
        env["ALL_PROXY"] = proxy_url
        env["all_proxy"] = proxy_url

    return env


def is_github_url(url):
    url = str(url or "")

    return (
        "github.com" in url
        or "raw.githubusercontent.com" in url
        or "objects.githubusercontent.com" in url
    )


def github_proxy_ping_object(proxy, timeout=5):
    """
    GitHub 代理测试：只 ping 代理地址本身。

    规则：
    - 有 HTTP 响应且状态码 < 500，认为代理服务活着；
    - 5xx 或请求异常，认为不可用。
    """
    prefix = build_proxy_url(proxy)

    if not prefix:
        raise RuntimeError("GitHub 代理地址为空")

    start = time.time()

    r = requests.get(
        prefix,
        timeout=timeout,
        headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
        allow_redirects=True,
    )

    ms = int((time.time() - start) * 1000)

    ok = 100 <= int(r.status_code) < 500

    if not ok:
        raise RuntimeError(f"GitHub 代理 Ping 失败，HTTP {r.status_code}")

    return {
        "status_code": r.status_code,
        "elapsed_ms": ms,
    }


def github_proxy_available(proxy, use_cache=True):
    """
    实际使用 GitHub 代理前先测试。

    返回：
    - True：代理可用；
    - False：代理不可用，应回退原始 GitHub 地址。
    """
    if not proxy or proxy.get("type") != "github":
        return False

    prefix = build_proxy_url(proxy)

    if not prefix:
        return False

    cache_key = proxy.get("id") or prefix
    now = time.time()

    if use_cache:
        cached = _GITHUB_PROXY_HEALTH_CACHE.get(cache_key)

        if cached:
            ts, ok = cached

            if now - ts <= _GITHUB_PROXY_HEALTH_TTL:
                return bool(ok)

    try:
        github_proxy_ping_object(proxy, timeout=5)
        ok = True
    except Exception:
        ok = False

    _GITHUB_PROXY_HEALTH_CACHE[cache_key] = (now, ok)

    return ok


def github_proxy_url_from_proxy(url, proxy, verify=True):
    """
    GitHub URL 拼接代理。

    verify=True 时：
    - 使用前先测试 GitHub 代理；
    - 测试不通过，直接返回原始 URL。
    """
    url = str(url or "").strip()

    if not proxy:
        return url

    if proxy.get("type") != "github":
        return url

    if not is_github_url(url):
        return url

    if verify and not github_proxy_available(proxy, use_cache=True):
        return url

    prefix = build_proxy_url(proxy)

    if not prefix:
        return url

    return prefix.rstrip("/") + "/" + url


def github_proxy_url(url, proxy_id, verify=True):
    return github_proxy_url_from_proxy(url, get_proxy(proxy_id), verify=verify)


def github_git_config_args_from_proxy(proxy, verify=True):
    """
    GitHub Git 临时配置代理。

    返回类似：

    [
        "-c",
        "url.https://gh-proxy.com/https://github.com/.insteadOf=https://github.com/"
    ]

    verify=True 时：
    - 使用前先测试 GitHub 代理；
    - 测试不通过，返回空列表。
    """
    if not proxy or proxy.get("type") != "github":
        return []

    if verify and not github_proxy_available(proxy, use_cache=True):
        return []

    prefix = build_proxy_url(proxy)

    if not prefix:
        return []

    prefix = prefix.rstrip("/")

    return [
        "-c",
        f"url.{prefix}/https://github.com/.insteadOf=https://github.com/",
    ]


def github_git_config_args(proxy_id, verify=True):
    return github_git_config_args_from_proxy(get_proxy(proxy_id), verify=verify)


def build_git_command_with_github_proxy(git_bin, proxy_id, args, verify=True):
    """
    构造 Git 命令。

    GitHub 代理可用时：

        git -c url.xxx.insteadOf=... clone https://github.com/a/b.git target

    GitHub 代理不可用时：

        git clone https://github.com/a/b.git target
    """
    return [git_bin] + github_git_config_args(proxy_id, verify=verify) + list(args)


def github_git_proxy_used(proxy_id, verify=True):
    """
    判断当前 proxy_id 是否会使用 GitHub Git 临时配置代理。
    """
    return bool(github_git_config_args(proxy_id, verify=verify))


def proxy_from_form(form):
    return {
        "id": form.get("id", "").strip(),
        "name": form.get("name", "").strip() or "未命名代理",
        "type": form.get("type", "http").strip(),
        "host": form.get("host", "").strip(),
        "port": form.get("port", "").strip(),
        "username": form.get("username", "").strip(),
        "password": form.get("password", "").strip(),
        "url": form.get("url", "").strip(),
        "enabled": form.get("enabled", "1") == "1",
    }


def proxy_select_options(selected_id=""):
    proxies = load_proxies()

    enabled_ids = {
        p.get("id")
        for p in proxies
        if p.get("enabled", True)
    }

    if selected_id not in enabled_ids:
        selected_id = ""

    options = '<option value="">不使用代理</option>'

    for proxy in proxies:
        if not proxy.get("enabled", True):
            continue

        selected = "selected" if proxy.get("id") == selected_id else ""
        name = proxy.get("name") or proxy.get("type")
        ptype = proxy.get("type", "")

        options += (
            f'<option value="{h(proxy.get("id"))}" {selected}>'
            f'{h(name)} [{h(ptype)}]'
            f'</option>'
        )

    return options


def test_proxy_object(proxy, test_url="https://www.baidu.com"):
    """
    代理测试。

    GitHub 代理：
        只 ping 代理地址本身。

    普通代理：
        请求 test_url。
    """
    if proxy.get("type") == "github":
        return github_proxy_ping_object(proxy)

    start = time.time()

    r = requests.get(
        test_url,
        proxies=requests_proxy_dict_from_proxy(proxy),
        timeout=15,
        headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
    )

    ms = int((time.time() - start) * 1000)

    return {
        "status_code": r.status_code,
        "elapsed_ms": ms,
    }


def parse_quality_urls(value=None):
    default_urls = [
        "https://www.baidu.com",
        "https://www.github.com",
        "https://raw.githubusercontent.com",
    ]

    value = str(value or "").strip()

    if not value:
        return default_urls

    items = re.split(r"[\s,，]+", value)
    urls = []

    for item in items:
        item = item.strip()

        if not item:
            continue

        if not item.startswith(("http://", "https://")):
            item = "https://" + item

        if item not in urls:
            urls.append(item)

    return urls or default_urls


def quality_github_proxy_object(proxy):
    """
    GitHub 代理质量检测。

    固定检测：
        https://github.com/liyw0205/fls-scripts/raw/refs/heads/main/index.json

    检测两种方式：
    1. 拼接方式；
    2. Git 临时配置 insteadOf 方式。
    """
    result = []
    prefix = build_proxy_url(proxy)

    if not prefix:
        return [{
            "url": "GitHub 代理地址",
            "ok": False,
            "status_code": "-",
            "elapsed": "GitHub 代理地址为空",
        }]

    # ============================================================
    # 1. 拼接方式检测 raw 文件
    # ============================================================
    concat_url = github_proxy_url_from_proxy(
        GITHUB_QUALITY_RAW_URL,
        proxy,
        verify=False,
    )

    start = time.time()

    try:
        r = requests.get(
            concat_url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
        )

        ms = int((time.time() - start) * 1000)
        text = r.text or ""

        ok = r.status_code == 200 and len(text.strip()) > 10

        result.append({
            "url": "拼接方式：" + GITHUB_QUALITY_RAW_URL,
            "ok": ok,
            "status_code": r.status_code,
            "elapsed": f"{ms} ms",
        })

    except Exception as e:
        ms = int((time.time() - start) * 1000)

        result.append({
            "url": "拼接方式：" + GITHUB_QUALITY_RAW_URL,
            "ok": False,
            "status_code": "-",
            "elapsed": f"{ms} ms / {e}",
        })

    # ============================================================
    # 2. Git insteadOf 临时配置检测
    # ============================================================
    git_bin = shutil.which("git")

    if not git_bin:
        result.append({
            "url": "Git 临时配置方式：" + GITHUB_QUALITY_REPO_URL,
            "ok": False,
            "status_code": "-",
            "elapsed": "未安装 git",
        })

        return result

    start = time.time()

    try:
        cmd = [
            git_bin,
            "-c",
            f"url.{prefix.rstrip('/')}/https://github.com/.insteadOf=https://github.com/",
            "ls-remote",
            "--heads",
            GITHUB_QUALITY_REPO_URL,
        ]

        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=25,
        )

        ms = int((time.time() - start) * 1000)
        output = r.stdout or ""

        ok = r.returncode == 0 and "refs/heads" in output

        result.append({
            "url": "Git 临时配置方式：" + GITHUB_QUALITY_REPO_URL,
            "ok": ok,
            "status_code": r.returncode,
            "elapsed": f"{ms} ms" if ok else f"{ms} ms / {output[:300]}",
        })

    except Exception as e:
        ms = int((time.time() - start) * 1000)

        result.append({
            "url": "Git 临时配置方式：" + GITHUB_QUALITY_REPO_URL,
            "ok": False,
            "status_code": "-",
            "elapsed": f"{ms} ms / {e}",
        })

    return result


def quality_proxy_object(proxy, urls=None):
    """
    代理质量检测。

    GitHub 代理：
        固定检测 fls-scripts index.json；
        同时检测 URL 拼接和 Git insteadOf 两种方式。

    普通代理：
        使用指定 URL 列表或默认 URL 列表。
    """
    if proxy.get("type") == "github":
        return quality_github_proxy_object(proxy)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    urls = urls or parse_quality_urls()
    timeout = 8

    def check_one(u):
        start = time.time()

        try:
            r = requests.get(
                u,
                proxies=requests_proxy_dict_from_proxy(proxy),
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 FLS-Manager"},
            )

            ms = int((time.time() - start) * 1000)

            return {
                "url": u,
                "ok": True,
                "status_code": r.status_code,
                "elapsed": f"{ms} ms",
            }

        except Exception as e:
            ms = int((time.time() - start) * 1000)

            return {
                "url": u,
                "ok": False,
                "status_code": "-",
                "elapsed": f"{ms} ms / {e}",
            }

    result_map = {}

    with ThreadPoolExecutor(max_workers=min(len(urls), 8) or 1) as pool:
        futures = {
            pool.submit(check_one, u): u
            for u in urls
        }

        for future in as_completed(futures):
            u = futures[future]

            try:
                result_map[u] = future.result()
            except Exception as e:
                result_map[u] = {
                    "url": u,
                    "ok": False,
                    "status_code": "-",
                    "elapsed": str(e),
                }

    return [
        result_map.get(u, {
            "url": u,
            "ok": False,
            "status_code": "-",
            "elapsed": "未知错误",
        })
        for u in urls
    ]
