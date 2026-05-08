import html
import re
from datetime import datetime
from urllib.parse import urlparse

try:
    from flask import request
except Exception:
    request = None


def get_back_url(default="/"):
    """
    获取安全返回地址。

    优先级：
    1. URL 参数 back
    2. request.referrer，同源时转为 path?query
    3. default

    安全限制：
    - 只允许站内相对路径；
    - 禁止 //example.com 这种协议相对地址；
    - 禁止 http://evil.com 这种外部地址。
    """
    if request is None:
        return default

    def clean(value):
        value = str(value or "").strip()

        if not value:
            return ""

        # 禁止协议相对 URL
        if value.startswith("//"):
            return ""

        parsed = urlparse(value)

        # 绝对 URL：只允许同源
        if parsed.scheme or parsed.netloc:
            try:
                if parsed.netloc != request.host:
                    return ""
                path = parsed.path or "/"
                if parsed.query:
                    path += "?" + parsed.query
                return path
            except Exception:
                return ""

        # 相对路径：必须以 / 开头
        if not value.startswith("/"):
            return ""

        return value

    back = clean(request.args.get("back", ""))
    if back:
        return back

    ref = clean(request.referrer or "")
    if ref:
        return ref

    return default

def h(v):
    return html.escape(str(v if v is not None else ""), quote=True)

def now_str():
    try:
        from .config import panel_now
        return panel_now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def safe_name(name):
    name = str(name or "").strip()
    name = re.sub(r"[\\/:*?\"<>|]+", "_", name)
    name = re.sub(r"\s+", " ", name)
    return name[:80] or "未命名任务"

def parse_env_text(text):
    env = {}
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env

def env_to_text(env):
    if not env:
        return ""
    return "\n".join([f'{k}="{str(v)}"' for k, v in env.items()])
