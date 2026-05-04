from .paths import TASK_FILE, GLOBAL_ENV_FILE, PROXY_FILE
from .storage import read_json, write_json

def load_tasks():
    return read_json(TASK_FILE, [])

def save_tasks(tasks):
    write_json(TASK_FILE, tasks)

def get_task(task_id):
    for t in load_tasks():
        if t.get("id") == task_id:
            return t
    return None

def load_global_env():
    return read_json(GLOBAL_ENV_FILE, {})

def save_global_env(env):
    write_json(GLOBAL_ENV_FILE, env)

def load_proxies():
    return read_json(PROXY_FILE, [])

def save_proxies(proxies):
    write_json(PROXY_FILE, proxies)

def get_proxy(proxy_id):
    if not proxy_id:
        return None
    for p in load_proxies():
        if p.get("id") == proxy_id and p.get("enabled", True):
            return p
    return None
