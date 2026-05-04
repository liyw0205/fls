import json
import threading

_STORAGE_LOCK = threading.RLock()

def read_json(file_path, default):
    with _STORAGE_LOCK:
        if not file_path.exists():
            return default

        try:
            return json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            return default

def write_json(file_path, data):
    with _STORAGE_LOCK:
        file_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_file = file_path.with_name(file_path.name + ".tmp")

        tmp_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        tmp_file.replace(file_path)
