import html
import re
from datetime import datetime

def h(v):
    return html.escape(str(v if v is not None else ""), quote=True)

def now_str():
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
