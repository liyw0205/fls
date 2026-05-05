import time
import re
import shutil
import subprocess
import requests

from .models import load_proxies
from .utils import h


GITHUB_QUALITY_RAW_URL = "https://github.com/liyw0205/fls-scripts/raw/refs/heads/main/index.json"
GITHUB_QUALITY_REPO_URL = "https://github.com/liyw0205/fls-scripts.git"

_GITHUB_PROXY_HEALTH_CACHE = {}
_GITHUB_PROXY_HEALTH_TTL = 60


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