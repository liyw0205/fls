import copy
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from itertools import islice
from urllib.parse import urljoin, urlsplit

from .models import TASKS_MUTATION_LOCK, load_proxies
from .paths import DATA_DIR, PROXY_DIAGNOSTICS_FILE
from .proxy import (GITHUB_QUALITY_RAW_URL, GITHUB_QUALITY_REPO_URL,
                    build_proxy_url, github_proxy_url_from_proxy, requests_proxy_dict_from_proxy)
from .proxy_actions import _fingerprint, _native_values, _resolve_native_values, proxy_values, proxy_version
from .storage import storage_transaction, write_json
from .utils import LazyModule, now_str

requests = LazyModule("requests")
CHECK_DIR = DATA_DIR / "proxy_checks"
INDEX_FILE = DATA_DIR / "proxy_checks_index.json"
LOCK = threading.RLock()
ACTIVE = set()
SLOTS = threading.BoundedSemaphore(3)
MAX_PENDING = 12
HISTORY_LIMIT = 200
RECEIPT_LIMIT = 10000
STATES = ("queued", "running", "succeeded", "partial", "failed", "unknown")
ERRORS = ("", "timeout", "network", "invalid_proxy", "http_status", "body_empty", "git_missing",
          "git_failed", "output_limit", "start_failed", "interrupted", "legacy_failure")
DEFAULT_TARGETS = ["https://www.baidu.com", "https://www.github.com", "https://raw.githubusercontent.com"]


class CheckError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{32}", value) is not None


def target_url(value):
    if not isinstance(value, str) or not value or len(value) > 1024 or any(c.isspace() or ord(c) < 32 for c in value):
        raise CheckError("检测目标地址格式或长度无效")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment and parsed.port != 0
                 and "\\" not in value)
    except ValueError:
        valid = False
    if not valid:
        raise CheckError("检测目标只支持无凭据、查询串或片段的 HTTP/HTTPS 地址")
    return value


def _read_json(path, limit):
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = handle.read(limit + 1)
    except FileNotFoundError:
        return None
    except UnicodeError as exc:
        raise RuntimeError("检测记录无法读取") from exc
    if len(raw) > limit:
        raise RuntimeError("检测记录超过限制")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("检测记录无法读取") from exc


def _read(check_id, key):
    if not valid_id(check_id):
        raise CheckError("检测标识无效")
    row = _read_json(CHECK_DIR / (check_id + ".json"), 64 * 1024)
    if row is None:
        return None
    if (not isinstance(row, dict) or row.get("id") != check_id or row.get("status") not in STATES
        or row.get("kind") not in ("test", "quality") or row.get("source") not in ("saved", "draft")
        or not isinstance(row.get("signature"), str)
        or not hmac.compare_digest(row["signature"], _fingerprint({k: v for k, v in row.items() if k != "signature"}, key))):
        raise RuntimeError("检测记录无法验证")
    return row


def _save(row, key):
    data = {k: v for k, v in row.items() if k != "signature"}
    data["signature"] = _fingerprint(data, key)
    write_json(CHECK_DIR / (row["id"] + ".json"), data)


def _index():
    ids = _read_json(INDEX_FILE, 16 * 1024)
    if ids is None:
        return []
    if not isinstance(ids, list) or len(ids) > HISTORY_LIMIT or any(not valid_id(value) for value in ids) or len(set(ids)) != len(ids):
        raise RuntimeError("检测历史索引损坏")
    return ids


def _proxies():
    with TASKS_MUTATION_LOCK, storage_transaction():
        return {proxy["id"]: proxy for proxy in load_proxies(read_only=True)}


def _public(row, proxies):
    result = copy.deepcopy(row)
    for key in ("signature", "intent"):
        result.pop(key, None)
    if result["status"] in ("queued", "running") and result["id"] not in ACTIVE:
        result.update(status="unknown", error_code="interrupted")
        for item in result["items"]:
            if item["status"] in ("pending", "running"):
                item["status"] = "unknown"
    proxy = proxies.get(result["proxy_id"])
    result.update(proxy_exists=proxy is not None,
                  current_matches=proxy is not None and result["version"] == proxy_version(proxy),
                  retry_allowed=result["source"] != "draft" and proxy is not None
                  and bool(proxy.get("enabled")) and result["status"] not in ("queued", "running")
                  and bool(result.get("targets")),
                  completed=sum(item["status"] in ("succeeded", "failed") for item in result["items"]),
                  total=len(result["items"]))
    return result


def _legacy():
    rows = _read_json(PROXY_DIAGNOSTICS_FILE, 4 * 1024 * 1024) or []
    if not isinstance(rows, list):
        raise RuntimeError("旧检测历史损坏")
    result = []
    for old in rows[:HISTORY_LIMIT]:
        if not isinstance(old, dict) or not valid_id(old.get("id")):
            continue
        items, targets = [], []
        kind = old.get("kind") if old.get("kind") in ("test", "quality") else "quality"
        github = old.get("proxy_type") == "github"
        for item in (old.get("items") if isinstance(old.get("items"), list) else [])[:8]:
            if not isinstance(item, dict):
                continue
            url, mode = item.get("url"), "http"
            if github and isinstance(url, str) and url.endswith(GITHUB_QUALITY_RAW_URL):
                url, mode = GITHUB_QUALITY_RAW_URL, "github_raw"
            elif github and isinstance(url, str) and url.endswith(GITHUB_QUALITY_REPO_URL):
                url, mode = GITHUB_QUALITY_REPO_URL, "github_git"
            try:
                url = target_url(url)
            except CheckError:
                url = "历史目标已隐藏"
            raw_code = item.get("status_code")
            code = int(raw_code) if str(raw_code).isdigit() and 100 <= int(raw_code) <= 599 and mode != "github_git" else None
            match = re.fullmatch(r"(\d{1,9}) ms(?: /.*)?", str(item.get("elapsed", "")), re.S)
            ok = item.get("ok") is True
            items.append(dict(target=url, mode=mode, status="succeeded" if ok else "failed", reachable=ok,
                              http_success=200 <= code < 300 if code is not None else None,
                              status_code=code, elapsed_ms=min(86400000, int(match[1])) if match else None,
                              error_code="" if ok else "legacy_failure"))
        try:
            urls = old.get("urls")
            targets = [target_url(url) for url in (urls if isinstance(urls, list) else [])[:5]]
        except CheckError:
            targets = []
        status = old.get("status")
        if status not in ("succeeded", "failed"):
            status = "unknown"
        result.append(dict(id="legacy-" + old["id"], request_id="", source="legacy", proxy_id=str(old.get("proxy_id", ""))[:200],
                           proxy_name=str(old.get("proxy_name", ""))[:200], proxy_type=str(old.get("proxy_type", ""))[:20],
                           version="", kind=kind, targets=targets or (DEFAULT_TARGETS[:1] if github else []), items=items,
                           status=status, error_code="legacy_failure" if status == "failed" else "",
                           created_at=_date(old.get("created_at")), updated_at=_date(old.get("updated_at")),
                           retry_of=""))
    return result


def _date(value):
    return value if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", value) else ""


def get_check(check_id, key):
    proxies = _proxies()
    with LOCK:
        if check_id.startswith("legacy-") and valid_id(check_id[7:]):
            row = next((row for row in _legacy() if row["id"] == check_id), None)
        else:
            row = _read(check_id, key)
        if row is None:
            raise CheckError("检测记录不存在", 404)
        return _public(row, proxies)


def list_checks(key, *, page=1, limit=25, proxy_id="", status="", kind=""):
    if page < 1 or not 1 <= limit <= 100 or status not in ("", *STATES) or kind not in ("", "test", "quality") or len(proxy_id) > 200:
        raise CheckError("检测历史筛选或分页参数无效")
    proxies = _proxies()
    with LOCK:
        rows = [_public(row, proxies) for check_id in _index() if (row := _read(check_id, key)) is not None]
        rows.extend(_public(row, proxies) for row in _legacy())
        rows = [row for row in rows if (not proxy_id or row["proxy_id"] == proxy_id)
                and (not status or row["status"] == status) and (not kind or row["kind"] == kind)]
        rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
        pages = max(1, (len(rows) + limit - 1) // limit)
        page = min(page, pages)
        return dict(items=rows[(page - 1) * limit:page * limit], page=page, pages=pages, total=len(rows))


def start_check(data, key, retry_of=""):
    if not isinstance(data, dict) or not valid_id(data.get("request_id")):
        raise CheckError("检测提交标识无效")
    check_id = data["request_id"]
    if retry_of:
        if set(data) != {"request_id", "version"}:
            raise CheckError("重新检测需要新提交标识和当前代理版本")
    elif set(data) - {"request_id", "kind", "proxy_id", "version", "draft", "targets"} or data.get("kind") not in ("test", "quality"):
        raise CheckError("检测提交字段无效")
    intent = _fingerprint(dict(data=data, retry_of=retry_of), key)
    # Keep the same model -> check -> storage lock order for all start paths.
    with TASKS_MUTATION_LOCK, LOCK:
        previous = _read(check_id, key)
        if previous:
            if not hmac.compare_digest(previous["intent"], intent):
                raise CheckError("同一提交标识不能更改检测内容", 409)
            return _public(previous, _proxies())
        if retry_of:
            old = get_check(retry_of, key)
            if not old["retry_allowed"]:
                raise CheckError("该记录不能直接重新检测，请提供当前代理或草稿", 409)
            data = dict(data, proxy_id=old["proxy_id"], kind=old["kind"], targets=old["targets"])
        proxies = _proxies()
        proxy_id = data.get("proxy_id", "")
        if not isinstance(proxy_id, str) or len(proxy_id) > 200 or any(ord(c) < 32 for c in proxy_id):
            raise CheckError("代理标识无效")
        current = proxies.get(proxy_id)
        if proxy_id and current is None:
            raise CheckError("代理不存在或已删除", 404)
        if proxy_id and data.get("version") != proxy_version(current):
            raise CheckError("代理已变化，请先核对当前配置", 409)
        if not proxy_id and "version" in data:
            raise CheckError("新草稿不能提交已保存代理版本")
        if "draft" in data:
            draft = data["draft"]
            if not isinstance(draft, dict) or set(draft) - {"settings", "credentials"}:
                raise CheckError("检测草稿格式无效")
            settings, credentials = _native_values(dict(action="edit" if proxy_id else "new", **draft))
            proxy = _resolve_native_values(current, settings, credentials)
            source = "draft"
        elif current is not None:
            if not current.get("enabled"):
                raise CheckError("代理已停用，请先启用或检测编辑草稿", 409)
            proxy = proxy_values(current)
            source = "saved"
        else:
            raise CheckError("请选择代理或提供检测草稿")
        urls = data.get("targets", DEFAULT_TARGETS[:1] if data["kind"] == "test" else DEFAULT_TARGETS)
        if not isinstance(urls, list) or not 1 <= len(urls) <= (1 if data["kind"] == "test" else 5):
            raise CheckError("连接检测需要一个目标，质量检测最多五个目标")
        urls = [target_url(value) for value in urls]
        if len(set(urls)) != len(urls):
            raise CheckError("检测目标不能重复")
        if proxy["type"] == "github" and "targets" in data and not retry_of:
            raise CheckError("GitHub 代理使用固定检测目标，不接受自定义目标")
        if len(ACTIVE) >= MAX_PENDING:
            raise CheckError("检测队列已满，请稍后重试", 429)
        if sum(1 for _ in islice(CHECK_DIR.glob("*.json"), RECEIPT_LIMIT)) >= RECEIPT_LIMIT:
            raise CheckError("检测记录已达到存储上限，请联系管理员归档后再检测", 409)
        for active_id in ACTIVE:
            active = _read(active_id, key)
            if proxy_id and active and active["proxy_id"] == proxy_id:
                raise CheckError("该代理已有检测作业，请从历史查看", 409)
        targets = urls
        modes = ["http"] * len(urls)
        if proxy["type"] == "github":
            targets = [proxy["url"]] if data["kind"] == "test" else [GITHUB_QUALITY_RAW_URL, GITHUB_QUALITY_REPO_URL]
            modes = ["github_ping"] if data["kind"] == "test" else ["github_raw", "github_git"]
        now = now_str()
        row = dict(id=check_id, request_id=check_id, source=source, proxy_id=proxy_id,
                   proxy_name=proxy["name"][:200], proxy_type=proxy["type"], version=proxy_version(current) if current else "",
                   kind=data["kind"], targets=urls, status="queued", error_code="", created_at=now, updated_at=now,
                   retry_of=retry_of, intent=intent,
                   items=[dict(target=target, mode=mode, status="pending", reachable=None, http_success=None,
                               status_code=None, elapsed_ms=None, error_code="") for target, mode in zip(targets, modes)])
        ids = _index()
        _save(row, key)
        write_json(INDEX_FILE, ([check_id] + ids)[:HISTORY_LIMIT])
        ACTIVE.add(check_id)
        try:
            _launch(check_id, copy.deepcopy(proxy), key)
        except Exception:
            ACTIVE.discard(check_id)
            row.update(status="failed", error_code="start_failed", updated_at=now_str())
            _save(row, key)
        return _public(_read(check_id, key), proxies)


def _launch(check_id, proxy, key):
    threading.Thread(target=_run, args=(check_id, proxy, key), daemon=True).start()


def _run(check_id, proxy, key):
    try:
        with SLOTS:
            with LOCK:
                row = _read(check_id, key)
                row.update(status="running", updated_at=now_str())
                _save(row, key)
            for index, item in enumerate(row["items"]):
                with LOCK:
                    item["status"] = "running"
                    _save(row, key)
                result = _probe(proxy, item)
                with LOCK:
                    row["items"][index] = dict(item, **result)
                    row["updated_at"] = now_str()
                    _save(row, key)
            success = sum(item["status"] == "succeeded" for item in row["items"])
            with LOCK:
                row.update(status="succeeded" if success == len(row["items"]) else "partial" if success else "failed",
                           updated_at=now_str())
                _save(row, key)
    except Exception:
        # A persisted in-flight record becomes unknown when its worker is gone.
        pass
    finally:
        with LOCK:
            ACTIVE.discard(check_id)


def _probe(proxy, item):
    start = time.monotonic()
    try:
        if item["mode"] == "github_git":
            result = _git_probe(proxy)
        else:
            target = (github_proxy_url_from_proxy(item["target"], proxy, verify=False)
                      if item["mode"] == "github_raw" else item["target"])
            with requests.Session() as session:
                session.trust_env = False
                for redirect in range(4):
                    remaining = 15 - (time.monotonic() - start)
                    if remaining <= 0:
                        raise requests.exceptions.Timeout()
                    response = session.get(target, proxies=requests_proxy_dict_from_proxy(proxy), stream=True,
                                           allow_redirects=False, timeout=(min(4, remaining), min(8, remaining)),
                                           headers={"User-Agent": "FLS-Native-Proxy-Check"})
                    if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("Location") and redirect < 3:
                        try:
                            target = target_url(urljoin(target, response.headers["Location"]))
                        finally:
                            response.close()
                        continue
                    break
                with response:
                    code = int(response.status_code)
                    http_success = 200 <= code < 300
                    ok = code < 500 if item["mode"] == "github_ping" else True
                    error = "" if ok else "http_status"
                    if item["mode"] == "github_raw":
                        count = 0
                        for index, chunk in enumerate(response.iter_content(chunk_size=1)):
                            count += len(chunk.strip())
                            if count > 10 or index >= 4095 or time.monotonic() - start > 15:
                                break
                        ok = code == 200 and count > 10
                        error = "" if ok else "http_status" if code != 200 else "body_empty"
                    result = dict(status="succeeded" if ok else "failed", reachable=True,
                                  http_success=http_success, status_code=code, error_code=error)
    except Exception as exc:
        error = "timeout" if isinstance(exc, requests.exceptions.Timeout) else "network"
        result = dict(status="failed", reachable=False, http_success=None, status_code=None, error_code=error)
    return dict(result, elapsed_ms=min(86400000, int((time.monotonic() - start) * 1000)))


def _git_probe(proxy):
    def result(ok=False, error="git_failed"):
        return dict(status="succeeded" if ok else "failed", reachable=ok, http_success=None,
                    status_code=None, error_code="" if ok else error)
    git = shutil.which("git")
    if not git:
        return result(error="git_missing")
    prefix = build_proxy_url(proxy)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GIT_") and key.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CEILING_DIRECTORIES=str(DATA_DIR))
    command = [git, "-c", "credential.helper=", "-c", "core.askPass=",
               "-c", f"url.{prefix.rstrip('/')}/https://github.com/.insteadOf=https://github.com/",
               "ls-remote", "--exit-code", GITHUB_QUALITY_REPO_URL, "refs/heads/main", "refs/heads/master"]
    CHECK_DIR.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=CHECK_DIR,
                               env=env, start_new_session=os.name != "nt")
    output, overflow = bytearray(), threading.Event()

    def kill():
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass

    def read():
        try:
            while chunk := process.stdout.read(1024):
                if len(output) + len(chunk) > 32768:
                    overflow.set()
                    kill()
                    break
                output.extend(chunk)
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        process.wait(timeout=25)
    except subprocess.TimeoutExpired:
        kill()
        process.wait(timeout=5)
        return result(error="timeout")
    finally:
        reader.join(timeout=1)
        if reader.is_alive():
            kill()
            reader.join(timeout=1)
        process.stdout.close()
    if overflow.is_set():
        return result(error="output_limit")
    ok = process.returncode == 0 and any(re.fullmatch(rb"[a-f0-9]{40,64}\trefs/heads/(main|master)", line) for line in output.splitlines())
    return result(ok)
