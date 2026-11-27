"""Own detached daemon logs until every inherited pipe writer has closed."""

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _detached_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
    return {"start_new_session": True}


def start_daemon_log(timeout=10):
    from fls_manager.paths import BASE_DIR

    env = os.environ.copy()
    env["FLS_BASE_DIR"] = str(BASE_DIR)
    process = subprocess.Popen(
        [sys.executable, "-u", str(Path(__file__).resolve())],
        cwd=tempfile.gettempdir(), env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **_detached_options(),
    )
    ready = threading.Event()
    reply = []

    def read_ready():
        try:
            reply.append(process.stdout.read(6))
        except OSError:
            pass
        finally:
            ready.set()

    reader = threading.Thread(target=read_ready, daemon=True, name="fls-daemon-log-ready")
    reader.start()
    if not ready.wait(timeout) or reply != [b"ready\n"]:
        # No command has inherited the pipe yet; aborting here cannot abandon a writer.
        process.kill()
        process.wait(timeout=5)
        process.stdin.close()
        reader.join(5)
        process.stdout.close()
        raise RuntimeError("daemon log collector failed to become ready")
    reader.join()
    process.stdout.close()
    threading.Thread(target=process.wait, daemon=True, name="fls-daemon-log-wait").start()
    return process


def redirect_daemon_output():
    process = start_daemon_log()
    saved = []
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        for descriptor in (1, 2):
            saved.append(os.dup(descriptor))
        try:
            os.dup2(process.stdin.fileno(), 1)
            os.dup2(process.stdin.fileno(), 2)
            _set_standard_handles()
            _replace_windows_text_streams()
        except BaseException:
            for original, destination in zip(saved, (1, 2)):
                os.dup2(original, destination)
            _set_standard_handles()
            raise
    finally:
        for descriptor in saved:
            os.close(descriptor)
        process.stdin.close()


def _set_standard_handles():
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        set_handle = ctypes.WinDLL("kernel32", use_last_error=True).SetStdHandle
        set_handle.argtypes = [wintypes.DWORD, wintypes.HANDLE]
        set_handle.restype = wintypes.BOOL
        # Child processes use GetStdHandle rather than Python's CRT descriptors.
        for standard, descriptor in ((-11, 1), (-12, 2)):
            if not set_handle(standard, msvcrt.get_osfhandle(descriptor)):
                raise ctypes.WinError(ctypes.get_last_error())


def _replace_windows_text_streams():
    if os.name != "nt":
        return
    streams = []
    try:
        # Startup ConsoleIO objects still call WriteConsoleW after dup2. Use
        # ordinary pipe-backed streams while keeping separate descriptors.
        for descriptor in (1, 2):
            duplicate = os.dup(descriptor)
            try:
                stream = os.fdopen(duplicate, "w", buffering=1, encoding="utf-8", errors="backslashreplace")
            except BaseException:
                os.close(duplicate)
                raise
            streams.append(stream)
    except BaseException:
        for stream in streams:
            stream.close()
        raise
    sys.stdout, sys.stderr = streams


def launch_logged_command(command, *, header=b""):
    from fls_manager.directory_jobs import directory_job
    from fls_manager.paths import BASE_DIR

    with directory_job():
        collector = start_daemon_log()
        try:
            collector.stdin.write(header)
            collector.stdin.flush()
            return subprocess.Popen(
                command, cwd=str(BASE_DIR), stdin=subprocess.DEVNULL,
                stdout=collector.stdin, stderr=subprocess.STDOUT, **_detached_options(),
            )
        finally:
            collector.stdin.close()


def _collect():
    from fls_manager.log_access import open_daemon_log
    from fls_manager.paths import LOG_DIR

    with open_daemon_log(LOG_DIR / "fls-manager-daemon.log") as log:
        sys.stdout.buffer.write(b"ready\n")
        sys.stdout.buffer.flush()
        sys.stdout.close()
        while True:
            block = os.read(sys.stdin.fileno(), 64 * 1024)
            if not block:
                return
            pending = memoryview(block)
            while pending:
                written = log.write(pending)
                if not written:
                    raise OSError("daemon log write did not progress")
                pending = pending[written:]


if __name__ == "__main__":
    _collect()
