"""Order task path changes and interactive task/script file publications."""

from .directory_jobs import DirectoryJobLock


# Acquire after TASKS_MUTATION_LOCK and before storage_transaction.
TASK_FILES_LOCK = DirectoryJobLock("task-files-publication", lambda: None)
