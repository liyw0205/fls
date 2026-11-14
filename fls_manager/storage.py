import json
import threading
from contextlib import contextmanager

_STORAGE_LOCK = threading.RLock()


@contextmanager
def storage_transaction():
    """Hold the JSON storage lock across a multi-file filesystem operation."""
    with _STORAGE_LOCK:
        yield


def read_json(file_path, default):
    with _STORAGE_LOCK:
        return _read_json_unlocked(file_path, default)


def _read_json_unlocked(file_path, default):
    if not file_path.exists():
        return default

    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(file_path, data):
    with _STORAGE_LOCK:
        _write_json_unlocked(file_path, data)


def _write_json_unlocked(file_path, data):
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = file_path.with_name(file_path.name + ".tmp")
    tmp_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    tmp_file.replace(file_path)


def mutate_json(file_path, default, update_fn):
    """Apply one read-modify-write operation under the storage lock."""
    with _STORAGE_LOCK:
        data = _read_json_unlocked(file_path, default)
        updated = update_fn(data)
        if updated is None:
            return data
        _write_json_unlocked(file_path, updated)
        return updated
