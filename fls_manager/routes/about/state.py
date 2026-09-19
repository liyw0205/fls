import threading


ABOUT_JOBS = {}
ABOUT_STATE_LOCK = threading.RLock()
UPDATE_LOG_STATE = {
    "checking": False,
    "available": False,
    "version": "",
    "current_version": "",
    "checked_at": "",
    "error": "",
}


def update_log_state():
    with ABOUT_STATE_LOCK:
        return dict(UPDATE_LOG_STATE)


def set_update_log_state(**changes):
    with ABOUT_STATE_LOCK:
        UPDATE_LOG_STATE.update(changes)
        return dict(UPDATE_LOG_STATE)
