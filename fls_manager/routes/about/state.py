import threading


ABOUT_JOBS = {}
ABOUT_STATE_LOCK = threading.RLock()
UPDATE_LOG_STATE_CONDITION = threading.Condition(ABOUT_STATE_LOCK)
UPDATE_LOG_STATE = {
    "checking": False,
    "available": False,
    "version": "",
    "current_version": "",
    "checked_at": "",
    "error": "",
}


def update_log_state(wait_for_completion=False, timeout=0):
    with UPDATE_LOG_STATE_CONDITION:
        if wait_for_completion and UPDATE_LOG_STATE.get("checking"):
            UPDATE_LOG_STATE_CONDITION.wait_for(
                lambda: not UPDATE_LOG_STATE.get("checking"),
                timeout=max(0, float(timeout or 0)),
            )
        return dict(UPDATE_LOG_STATE)


def set_update_log_state(**changes):
    with UPDATE_LOG_STATE_CONDITION:
        UPDATE_LOG_STATE.update(changes)
        UPDATE_LOG_STATE_CONDITION.notify_all()
        return dict(UPDATE_LOG_STATE)
