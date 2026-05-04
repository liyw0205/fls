import time
import re
import requests
from .models import load_proxies, save_proxies, get_proxy as model_get_proxy
from .utils import h, now_str


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


def github_proxy_url_from_proxy(url, proxy):
    if not proxy:
        return url

    if proxy.get("type") != "github":
        return url

    prefix = build_proxy_url(proxy)
    if not prefix:
        return url

    if "github.com" not in url and "raw.githubusercontent.com" not in url:
        return url

    return prefix.rstrip("/") + "/" + url


def github_proxy_url(url, proxy_id):
    return github_proxy_url_from_proxy(url, get_proxy(proxy_id))


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
        options += f'<option value="{h(proxy.get("id"))}" {selected}>{h(name)} [{h(ptype)}]</option>'

    return options


def test_proxy_object(proxy, test_url="https://www.baidu.com"):
    start = time.time()

    if proxy.get("type") == "github":
        real_url = github_proxy_url_from_proxy("https://github.com", proxy)
        r = requests.get(real_url, timeout=15)
    else:
        r = requests.get(
            test_url,
            proxies=requests_proxy_dict_from_proxy(proxy),
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 FLS-Manager"}
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


def quality_proxy_object(proxy, urls=None):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    urls = urls or parse_quality_urls()
    timeout = 8

    def check_one(u):
        start = time.time()

        try:
            real_url = github_proxy_url_from_proxy(u, proxy) if proxy.get("type") == "github" else u

            r = requests.get(
                real_url,
                proxies=requests_proxy_dict_from_proxy(proxy),
                timeout=timeout,
                headers={"User-Agent": "Mozilla/5.0 FLS-Manager"}
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
        futures = {pool.submit(check_one, u): u for u in urls}

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
