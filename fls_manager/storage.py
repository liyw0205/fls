import json
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path

from .data_access import directory_access

_STORAGE_LOCK = threading.RLock()


@contextmanager
def storage_transaction(*, live_data=True):
    """Hold the JSON storage lock across a multi-file filesystem operation."""
    with directory_access(write=True) if live_data else nullcontext(), _STORAGE_LOCK:
        yield


def _file_access(file_path, *, write=False):
    from .paths import DATA_DIR
    if Path(file_path).absolute().is_relative_to(DATA_DIR.absolute()):
        return directory_access(write=write)
    return nullcontext()


def read_json(file_path, default):
    with _file_access(file_path), _STORAGE_LOCK:
        return _read_json_unlocked(file_path, default)


def _read_json_unlocked(file_path, default, strict=False):
    if strict:
        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except ValueError as exc:
            raise RuntimeError(f"JSON 文件读取失败，未覆盖原文件：{exc}") from exc
    if not file_path.exists():
        return default

    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(file_path, data):
    with _file_access(file_path, write=True), _STORAGE_LOCK:
        _write_json_unlocked(file_path, data)


def _write_json_unlocked(file_path, data):
    file_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = file_path.with_name(file_path.name + ".tmp")
    tmp_file.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    tmp_file.replace(file_path)


def mutate_json(file_path, default, update_fn, *, strict=False):
    """Apply one read-modify-write operation under the storage lock."""
    with _file_access(file_path, write=True), _STORAGE_LOCK:
        data = _read_json_unlocked(file_path, default, strict=strict)
        updated = update_fn(data)
        if updated is None:
            return data
        _write_json_unlocked(file_path, updated)
        return updated
