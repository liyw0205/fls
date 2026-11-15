import threading
import uuid
import json
import os
import sqlite3
from pathlib import Path

from .paths import (
    TASK_FILE,
    TASK_HISTORY_FILE,
    TASK_HISTORY_INDEX_FILE,
    GLOBAL_ENV_FILE,
    PROXY_FILE,
    COLLECTION_FILE,
)
from .storage import read_json, write_json, mutate_json, storage_transaction
from .utils import now_str

MAX_TASK_RETRY_ATTEMPTS = 5
MIN_TASK_RETRY_INTERVAL_SECONDS = 5
MAX_TASK_RETRY_INTERVAL_SECONDS = 3600
DEFAULT_TASK_RETRY_INTERVAL_SECONDS = 60
TASK_NOTIFY_MODES = ("none", "default", "custom")
TASK_RANDOM_DELAY_MODES = ("none", "default", "custom")
PROXY_TYPES = ("http", "https", "socks4", "socks5", "socks5h", "github")
TASK_HISTORY_IN_FLIGHT_STATUSES = frozenset(("starting", "delaying", "running"))
TASK_HISTORY_INDEX_LOCK = threading.RLock()
TASK_HISTORY_INDEX_SCHEMA = 3
MAX_HISTORY_SEARCH_NGRAM_CANDIDATES = 64


def _history_index_disabled():
    value = os.environ.get("FLS_DISABLE_HISTORY_INDEX", "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _as_text(value, default=""):
    if value is None:
        return default

    return str(value).strip()


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value

    if isinstance(value, (int, float)):
        return bool(value)

    text = str(value or "").strip().lower()

    if text in ("1", "true", "yes", "on", "enabled", "enable"):
        return True

    if text in ("0", "false", "no", "off", "disabled", "disable", ""):
        return False

    return bool(default)


def _as_int(value, default=0, min_value=None, max_value=None):
    try:
        result = int(value)
    except Exception:
        result = int(default)

    if min_value is not None:
        result = max(int(min_value), result)

    if max_value is not None:
        result = min(int(max_value), result)

    return result


def _id_list(value):
    result = []
    seen = set()

    if not isinstance(value, (list, tuple, set)):
        return result

    for item in value:
        item_id = _as_text(item)

        if not item_id or item_id in seen:
            continue

        seen.add(item_id)
        result.append(item_id)

    return result


def normalize_env_map(env):
    if not isinstance(env, dict):
        return {}

    result = {}

    for key, value in env.items():
        key = _as_text(key)

        if not key:
            continue

        result[key] = "" if value is None else str(value)

    return result


def normalize_task_notify(task):
    task = task if isinstance(task, dict) else {}
    notify = task.get("notify")

    if isinstance(notify, dict):
        mode = _as_text(notify.get("mode", "none"), "none")

        if mode in TASK_NOTIFY_MODES:
            ids = _id_list(notify.get("ids")) if mode == "custom" else []

            return {
                "mode": mode,
                "ids": ids,
            }

    old_ids = _id_list(task.get("notify_ids"))

    if old_ids:
        if "__none__" in old_ids:
            return {
                "mode": "none",
                "ids": [],
            }

        if "__default__" in old_ids:
            return {
                "mode": "default",
                "ids": [],
            }

        return {
            "mode": "custom",
            "ids": old_ids,
        }

    # Missing legacy notify data used to mean no notification at runtime.
    return {
        "mode": "none",
        "ids": [],
    }


def normalize_task_random_delay(task):
    task = task if isinstance(task, dict) else {}
    delay = task.get("random_delay")

    if isinstance(delay, dict):
        mode = _as_text(delay.get("mode", "none"), "none")

        if mode in TASK_RANDOM_DELAY_MODES:
            seconds = (
                _as_int(delay.get("seconds", 0), 0, 0, 120)
                if mode == "custom"
                else 0
            )

            return {
                "mode": mode,
                "seconds": seconds,
            }

    return {
        "mode": "none",
        "seconds": 0,
    }


def normalize_task_retry(task):
    task = task if isinstance(task, dict) else {}
    retry = task.get("retry")

    attempts_value = 0
    interval_value = DEFAULT_TASK_RETRY_INTERVAL_SECONDS

    if isinstance(retry, dict):
        attempts_value = retry.get("attempts", 0)
        interval_value = retry.get(
            "interval_seconds",
            DEFAULT_TASK_RETRY_INTERVAL_SECONDS,
        )
    elif "retry_count" in task:
        attempts_value = task.get("retry_count", 0)

    return {
        "attempts": _as_int(
            attempts_value,
            0,
            0,
            MAX_TASK_RETRY_ATTEMPTS,
        ),
        "interval_seconds": _as_int(
            interval_value,
            DEFAULT_TASK_RETRY_INTERVAL_SECONDS,
            MIN_TASK_RETRY_INTERVAL_SECONDS,
            MAX_TASK_RETRY_INTERVAL_SECONDS,
        ),
    }


def normalize_task(task):
    if not isinstance(task, dict):
        return None

    item = dict(task)

    task_id = _as_text(item.get("id"))
    if not task_id:
        task_id = uuid.uuid4().hex

    command = _as_text(item.get("command"))
    name = _as_text(item.get("name")) or command or "未命名任务"

    item["id"] = task_id
    item["name"] = name
    item["remark"] = _as_text(item.get("remark"))
    item["command"] = command
    item["cron"] = _as_text(item.get("cron"))
    item["config_path"] = _as_text(item.get("config_path"))
    item["collection_id"] = _as_text(item.get("collection_id"))
    item["enabled"] = _as_bool(item.get("enabled", True), True)
    item["env"] = normalize_env_map(item.get("env"))
    item["proxy_id"] = _as_text(item.get("proxy_id"))
    item["notify"] = normalize_task_notify(item)
    item.pop("notify_ids", None)
    item["random_delay"] = normalize_task_random_delay(item)
    item["retry"] = normalize_task_retry(item)
    item.pop("retry_count", None)
    item["run_count"] = _as_int(item.get("run_count", 0), 0, 0)
    item["pinned"] = _as_bool(item.get("pinned", False), False)
    item["created_at"] = _as_text(item.get("created_at"))
    item["updated_at"] = _as_text(item.get("updated_at"))

    if "last_run_at" in item:
        item["last_run_at"] = _as_text(item.get("last_run_at"))

    return item


def normalize_proxy(proxy):
    if not isinstance(proxy, dict):
        return None

    item = dict(proxy)
    proxy_id = _as_text(item.get("id")) or uuid.uuid4().hex
    proxy_type = _as_text(item.get("type", "http"), "http").lower()

    if proxy_type not in PROXY_TYPES:
        proxy_type = "http"

    item["id"] = proxy_id
    item["name"] = _as_text(item.get("name")) or "未命名代理"
    item["type"] = proxy_type
    item["host"] = _as_text(item.get("host"))
    item["port"] = _as_text(item.get("port"))
    item["username"] = _as_text(item.get("username"))
    item["password"] = _as_text(item.get("password"))
    item["url"] = _as_text(item.get("url"))
    item["enabled"] = _as_bool(item.get("enabled", True), True)
    item["created_at"] = _as_text(item.get("created_at"))
    item["updated_at"] = _as_text(item.get("updated_at"))

    return item


def normalize_collection(collection):
    if not isinstance(collection, dict):
        return None

    item = dict(collection)
    collection_id = _as_text(item.get("id"))

    if not collection_id:
        return None

    item["id"] = collection_id
    item["name"] = _as_text(item.get("name")) or "未命名合集"
    item["remark"] = _as_text(item.get("remark"))
    item["created_at"] = _as_text(item.get("created_at"))
    item["updated_at"] = _as_text(item.get("updated_at"))

    return item


def _normalize_list(data, normalizer):
    if not isinstance(data, list):
        return [], True

    result = []
    changed = False

    for item in data:
        normalized = normalizer(item)

        if normalized is None:
            changed = True
            continue

        if normalized != item:
            changed = True

        result.append(normalized)

    return result, changed


TASK_HISTORY_LIMIT = 500
TASKS_MUTATION_LOCK = threading.RLock()
COLLECTION_MUTATION_LOCK = threading.RLock()


class TaskSchedulerReloadError(RuntimeError):
    pass


def load_tasks():
    result = {}

    def normalize(data):
        tasks, changed = _normalize_list(data, normalize_task)
        result["tasks"] = tasks
        return tasks if changed else None

    with TASKS_MUTATION_LOCK:
        mutate_json(TASK_FILE, [], normalize)

    return result.get("tasks", [])


def save_tasks(tasks):
    with TASKS_MUTATION_LOCK:
        normalized, _ = _normalize_list(tasks or [], normalize_task)
        write_json(TASK_FILE, normalized)


def mutate_tasks(update_fn, reload_scheduler_fn=None, after_write=None):
    """Atomically update tasks and keep related scheduler changes in the same order."""
    result = {}

    with TASKS_MUTATION_LOCK:
        def update(data):
            tasks, normalized = _normalize_list(data, normalize_task)
            changed = bool(update_fn(tasks))
            result["tasks"] = tasks
            result["changed"] = changed
            return tasks if changed or normalized else None

        mutate_json(TASK_FILE, [], update)

        if result.get("changed") and after_write:
            after_write()

        if result.get("changed") and reload_scheduler_fn:
            try:
                reload_scheduler_fn()
            except Exception as exc:
                raise TaskSchedulerReloadError(str(exc)) from exc

    return result.get("tasks", []), bool(result.get("changed"))


def get_task(task_id):
    for t in load_tasks():
        if t.get("id") == task_id:
            return t
    return None


def _history_source_signature():
    try:
        stat = Path(TASK_HISTORY_FILE).stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        return "missing"


def _history_search_text(item):
    return "\n".join(
        str(item.get(field) or "").lower()
        for field in ("task_name", "command", "message", "source")
    )


def _history_like_pattern(query):
    escaped = str(query or "").lower().replace("\\", "\\\\")
    escaped = escaped.replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _history_search_ngrams(query):
    """Return unique lowercase trigrams used to narrow substring searches."""
    text = str(query or "").lower()
    if len(text) < 3:
        return []
    return sorted({text[index:index + 3] for index in range(len(text) - 2)})


def _history_index_schema(conn):
    conn.executescript(
        """
        CREATE TABLE history_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE history_rows (
            seq INTEGER PRIMARY KEY,
            id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            status TEXT NOT NULL,
            search_text TEXT NOT NULL,
            row_json TEXT NOT NULL
        );
        CREATE INDEX history_rows_status_seq
            ON history_rows(status, seq);
        CREATE INDEX history_rows_task_seq
            ON history_rows(task_id, seq);
        CREATE TABLE history_ngrams (
            ngram TEXT NOT NULL,
            seq INTEGER NOT NULL,
            PRIMARY KEY (ngram, seq)
        );
        CREATE INDEX history_ngrams_ngram_seq
            ON history_ngrams(ngram, seq);
        CREATE TABLE history_ngram_stats (
            ngram TEXT PRIMARY KEY,
            row_count INTEGER NOT NULL
        );
        """
    )


def _build_task_history_index(signature):
    """Build a derived SQLite read index without changing the JSON source."""
    data = read_json(TASK_HISTORY_FILE, [])
    rows = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
    index_path = Path(TASK_HISTORY_INDEX_FILE)
    temp_path = index_path.with_name(
        f"{index_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )

    try:
        conn = sqlite3.connect(str(temp_path), timeout=30)
        try:
            _history_index_schema(conn)
            conn.executemany(
                "INSERT INTO history_rows(seq, id, task_id, status, search_text, row_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    (
                        index,
                        str(item.get("id") or ""),
                        str(item.get("task_id") or ""),
                        str(item.get("status") or ""),
                        _history_search_text(item),
                        json.dumps(item, ensure_ascii=False, separators=(",", ":")),
                    )
                    for index, item in enumerate(rows)
                ),
            )
            conn.executemany(
                "INSERT INTO history_ngrams(ngram, seq) VALUES (?, ?)",
                (
                    (ngram, index)
                    for index, item in enumerate(rows)
                    for ngram in _history_search_ngrams(_history_search_text(item))
                ),
            )
            conn.execute(
                "INSERT INTO history_ngram_stats(ngram, row_count) "
                "SELECT ngram, COUNT(*) FROM history_ngrams GROUP BY ngram"
            )
            conn.execute(
                "INSERT INTO history_meta(key, value) VALUES('schema', ?)",
                (str(TASK_HISTORY_INDEX_SCHEMA),),
            )
            conn.execute(
                "INSERT INTO history_meta(key, value) VALUES('source_signature', ?)",
                (signature,),
            )
            conn.commit()
        finally:
            conn.close()

        index_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.replace(index_path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _open_current_task_history_index():
    if _history_index_disabled():
        return None

    signature = _history_source_signature()
    index_path = Path(TASK_HISTORY_INDEX_FILE)
    with TASK_HISTORY_INDEX_LOCK:
        current = False
        if index_path.exists():
            try:
                with sqlite3.connect(str(index_path), timeout=30) as probe:
                    meta = dict(probe.execute("SELECT key, value FROM history_meta").fetchall())
                    objects = {
                        name
                        for (name,) in probe.execute(
                            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
                        )
                    }
                current = (
                    meta.get("schema") == str(TASK_HISTORY_INDEX_SCHEMA)
                    and meta.get("source_signature") == signature
                    and {
                        "history_meta",
                        "history_rows",
                        "history_rows_status_seq",
                        "history_rows_task_seq",
                        "history_ngrams",
                        "history_ngrams_ngram_seq",
                        "history_ngram_stats",
                    }.issubset(objects)
                )
            except (OSError, sqlite3.Error):
                current = False

        if not current:
            try:
                _build_task_history_index(signature)
            except (OSError, sqlite3.Error, TypeError, ValueError):
                return None

        try:
            conn = sqlite3.connect(str(index_path), timeout=30)
            conn.row_factory = sqlite3.Row
            return conn
        except (OSError, sqlite3.Error):
            return None


def load_task_history():
    data = read_json(TASK_HISTORY_FILE, [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _history_search_candidate(conn, ngrams):
    """Choose the rarest indexed trigram before applying exact LIKE semantics."""
    if not ngrams:
        return None
    ngrams = list(ngrams[:MAX_HISTORY_SEARCH_NGRAM_CANDIDATES])
    placeholders = ", ".join("?" for _ in ngrams)
    row = conn.execute(
        "SELECT ngram FROM history_ngram_stats "
        f"WHERE ngram IN ({placeholders}) "
        "ORDER BY row_count, ngram LIMIT 1",
        ngrams,
    ).fetchone()
    return str(row[0]) if row else ngrams[0]


def query_task_history(q="", status="", limit=30, offset=0, task_id="", statuses=None):
    """Query the history read index while retaining the JSON fallback contract."""
    q = str(q or "").strip()
    status = str(status or "").strip()
    task_id = str(task_id or "").strip()
    status_values = [str(value or "").strip() for value in (statuses or []) if str(value or "").strip()]
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    if limit is not None:
        try:
            limit = max(1, min(5000, int(limit)))
        except (TypeError, ValueError):
            limit = 30

    with storage_transaction():
        if (
            limit is None
            and offset == 0
            and not q
            and not status
            and not task_id
            and not status_values
        ):
            items = load_task_history()
            return items, len(items)

        conn = _open_current_task_history_index()
        if conn is None:
            items = load_task_history()
            if task_id:
                items = [item for item in items if str(item.get("task_id") or "") == task_id]
            if status_values:
                allowed = set(status_values)
                items = [item for item in items if str(item.get("status") or "") in allowed]
            elif status:
                items = [item for item in items if str(item.get("status") or "") == status]
            if q:
                needle = q.lower()
                items = [item for item in items if needle in _history_search_text(item)]
            total = len(items)
            if limit is None:
                return items[offset:], total
            return items[offset:offset + limit], total

        try:
            clauses = []
            params = []
            if task_id:
                clauses.append("task_id = ?")
                params.append(task_id)
            if status_values:
                placeholders = ", ".join("?" for _ in status_values)
                clauses.append(f"status IN ({placeholders})")
                params.extend(status_values)
            elif status:
                clauses.append("status = ?")
                params.append(status)
            if q:
                ngrams = _history_search_ngrams(q)
                if ngrams:
                    candidate = _history_search_candidate(conn, ngrams)
                    clauses.append(
                        "seq IN (SELECT seq FROM history_ngrams "
                        "WHERE ngram = ?)"
                    )
                    params.append(candidate)
                clauses.append("search_text LIKE ? ESCAPE '\\'")
                params.append(_history_like_pattern(q))
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            total = int(conn.execute(f"SELECT COUNT(*) FROM history_rows{where}", params).fetchone()[0])
            query = f"SELECT row_json FROM history_rows{where} ORDER BY seq"
            if limit is not None:
                query += " LIMIT ? OFFSET ?"
                params = [*params, limit, offset]
            elif offset:
                query += " LIMIT -1 OFFSET ?"
                params = [*params, offset]
            rows = []
            for row in conn.execute(query, params):
                try:
                    rows.append(json.loads(row[0]))
                except (TypeError, ValueError):
                    continue
            return rows, total
        finally:
            conn.close()


def explain_task_history_query(q="", status="", task_id=""):
    """Return SQLite query-plan details for the bounded performance benchmark."""
    with storage_transaction():
        conn = _open_current_task_history_index()
        if conn is None:
            return []
        try:
            clauses = []
            params = []
            if task_id:
                clauses.append("task_id = ?")
                params.append(str(task_id))
            if status:
                clauses.append("status = ?")
                params.append(str(status))
            if q:
                ngrams = _history_search_ngrams(q)
                if ngrams:
                    candidate = _history_search_candidate(conn, ngrams)
                    clauses.append(
                        "seq IN (SELECT seq FROM history_ngrams "
                        "WHERE ngram = ?)"
                    )
                    params.append(candidate)
                clauses.append("search_text LIKE ? ESCAPE '\\'")
                params.append(_history_like_pattern(q))
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = conn.execute(
                f"EXPLAIN QUERY PLAN SELECT row_json FROM history_rows{where} ORDER BY seq LIMIT 30",
                params,
            )
            return [str(row[3]) for row in rows]
        finally:
            conn.close()


def save_task_history(items):
    write_json(TASK_HISTORY_FILE, list(items or [])[:TASK_HISTORY_LIMIT])


def reconcile_task_history_after_restart(active_history_ids=None):
    """Mark unfinished attempts as unknown without touching currently held handles."""
    active_history_ids = {
        str(history_id)
        for history_id in (active_history_ids or ())
        if str(history_id or "").strip()
    }
    recovered_at = now_str()
    status_labels = {
        "starting": "启动中",
        "delaying": "延迟中",
        "running": "运行中",
    }
    recovered_count = 0

    def reconcile(data):
        nonlocal recovered_count
        history = data if isinstance(data, list) else []
        history = [item for item in history if isinstance(item, dict)]
        changed = False

        for item in history:
            status = str(item.get("status") or "")
            if status not in TASK_HISTORY_IN_FLIGHT_STATUSES:
                continue
            if str(item.get("id") or "") in active_history_ids:
                continue
            if str(item.get("end_at") or "").strip():
                continue

            original_message = str(item.get("message") or "").strip()
            message = (
                "面板重启后无法确认任务进程是否仍存活；"
                f"重启前状态：{status_labels.get(status, status)}"
            )
            if original_message:
                message += f"；原说明：{original_message}"

            item.update({
                "status": "unknown",
                "end_at": recovered_at,
                "message": message,
            })
            changed = True
            recovered_count += 1

        return history[:TASK_HISTORY_LIMIT] if changed else None

    mutate_json(TASK_HISTORY_FILE, [], reconcile)

    return recovered_count


def add_task_history(record):
    def add_record(data):
        history = data if isinstance(data, list) else []
        history = [item for item in history if isinstance(item, dict)]
        history.insert(0, dict(record or {}))
        return history[:TASK_HISTORY_LIMIT]

    mutate_json(TASK_HISTORY_FILE, [], add_record)


def update_task_history(record_id, updates):
    if not record_id:
        return False

    changed = False

    def update_record(data):
        nonlocal changed
        history = data if isinstance(data, list) else []
        history = [item for item in history if isinstance(item, dict)]

        for item in history:
            if item.get("id") == record_id:
                item.update(updates or {})
                changed = True
                break

        return history[:TASK_HISTORY_LIMIT] if changed else None

    mutate_json(TASK_HISTORY_FILE, [], update_record)

    return changed


def task_history_for_task(task_id, limit=20):
    result, _total = query_task_history(
        task_id=str(task_id or ""),
        limit=max(1, int(limit or 20)),
    )
    return result


def load_global_env():
    data = read_json(GLOBAL_ENV_FILE, {})
    env = normalize_env_map(data)

    if env != data:
        save_global_env(env)

    return env


def save_global_env(env):
    write_json(GLOBAL_ENV_FILE, normalize_env_map(env))


def load_proxies():
    data = read_json(PROXY_FILE, [])
    proxies, changed = _normalize_list(data, normalize_proxy)

    if changed:
        save_proxies(proxies)

    return proxies


def save_proxies(proxies):
    normalized, _ = _normalize_list(proxies or [], normalize_proxy)
    write_json(PROXY_FILE, normalized)


def get_proxy(proxy_id):
    if not proxy_id:
        return None

    for p in load_proxies():
        if p.get("id") == proxy_id and p.get("enabled", True):
            return p

    return None


def load_collections():
    with COLLECTION_MUTATION_LOCK:
        data = read_json(COLLECTION_FILE, [])
        collections, changed = _normalize_list(data, normalize_collection)

        if changed:
            save_collections(collections)

        return collections


def save_collections(collections):
    with COLLECTION_MUTATION_LOCK:
        normalized, _ = _normalize_list(collections or [], normalize_collection)
        write_json(COLLECTION_FILE, normalized)


def get_collection(collection_id):
    if not collection_id:
        return None

    for c in load_collections():
        if c.get("id") == collection_id:
            return c

    return None


def unique_collection_name(name="", exclude_id=""):
    base = str(name or "").strip() or "未命名合集"

    exists = {
        str(x.get("name", ""))
        for x in load_collections()
        if x.get("id") != exclude_id
    }

    if base not in exists:
        return base

    idx = 1
    while True:
        candidate = f"{base}-{idx}"
        if candidate not in exists:
            return candidate
        idx += 1
