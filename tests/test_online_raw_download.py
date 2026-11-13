import contextlib
import os
import stat
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_ui_route_components import TOKEN, isolated_app


class OnlineRawDownloadTests(unittest.TestCase):
    def item(self):
        return {
            "id": "raw-demo", "type": "raw", "name": "Raw demo",
            "link": "https://example.invalid/demo.sh", "link_name": "demo.sh",
        }

    @contextlib.contextmanager
    def response(self, install, chunks):
        response = Mock()
        response.iter_content.side_effect = lambda **_kwargs: iter(chunks)
        with patch.object(install, "requests", SimpleNamespace(get=Mock(return_value=response))), patch.object(
            install, "github_proxy_url", side_effect=lambda url, *_args, **_kwargs: url,
        ):
            yield response

    def test_stream_failure_preserves_existing_script(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import install

            target = install.online_script_target(self.item())
            target.write_bytes(b"original complete script")

            def chunks():
                yield b"partial replacement"
                raise RuntimeError("connection reset by peer")

            with self.response(install, chunks()) as response:
                with self.assertRaisesRegex(RuntimeError, "connection reset by peer"):
                    install.download_online_script_logged(self.item(), "", base_dir / "install.log", force=True)
            self.assertEqual(target.read_bytes(), b"original complete script")
            response.close.assert_called_once()
            self.assertEqual(list(target.parent.glob(".fls-download-*.part")), [])

    def test_cancellation_does_not_publish_a_partial_new_script(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import install
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_STOPPING

            target = install.online_script_target(self.item())

            def chunks():
                yield b"first chunk"
                ONLINE_INSTALL_STOPPING.add("cancel")
                yield b"second chunk"

            with self.response(install, chunks()) as response:
                with self.assertRaises(install.InstallStopped):
                    install.download_online_script_logged(self.item(), "", base_dir / "install.log", install_id="cancel")
            self.assertFalse(target.exists())
            response.close.assert_called_once()
            self.assertEqual(list(target.parent.glob(".fls-download-*.part")), [])

    def test_success_publishes_only_complete_content_and_sets_executable_mode(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import install

            target = install.online_script_target(self.item())
            target.write_bytes(b"original")

            def chunks():
                yield b"first half\n"
                self.assertEqual(target.read_bytes(), b"original")
                yield b"second half\n"
                self.assertEqual(target.read_bytes(), b"original")

            with self.response(install, chunks()) as response:
                result = install.download_online_script_logged(self.item(), "", base_dir / "install.log", force=True)
            self.assertEqual(result, target)
            self.assertEqual(target.read_bytes(), b"first half\nsecond half\n")
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)
            response.close.assert_called_once()
            self.assertEqual(list(target.parent.glob(".fls-download-*.part")), [])

    @unittest.skipIf(os.name == "nt", "POSIX file permissions")
    def test_existing_non_shell_permissions_are_preserved(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import install

            item = dict(self.item(), link_name="demo.py")
            target = install.online_script_target(item)
            target.write_bytes(b"original")
            target.chmod(0o640)
            with self.response(install, [b"complete"]):
                install.download_online_script_logged(item, "", base_dir / "install.log", force=True)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_existing_target_without_force_does_not_start_download(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import install

            target = install.online_script_target(self.item())
            target.write_bytes(b"original")
            with self.response(install, [b"replacement"]) as response:
                with self.assertRaises(FileExistsError):
                    install.download_online_script_logged(self.item(), "", base_dir / "install.log")
                install.requests.get.assert_not_called()
                response.close.assert_not_called()
            self.assertEqual(target.read_bytes(), b"original")

    def test_timeout_http_write_and_publication_errors_preserve_original(self):
        for failure in ("timeout", "http", "write", "replace", "chmod"):
            with self.subTest(failure=failure), isolated_app() as (_app, base_dir):
                from fls_manager.online_scripts import install

                target = install.online_script_target(self.item())
                target.write_bytes(b"original")

                def chunks():
                    yield b"partial"
                    if failure == "timeout":
                        raise TimeoutError("socket read timed out")

                with self.response(install, chunks()) as response, contextlib.ExitStack() as stack:
                    if failure == "http":
                        response.raise_for_status.side_effect = RuntimeError("503 upstream unavailable")
                    if failure == "write":
                        stack.enter_context(patch.object(install.os, "fsync", side_effect=OSError("disk full")))
                    if failure == "replace":
                        stack.enter_context(patch.object(install.os, "replace", side_effect=PermissionError("replace denied")))
                    if failure == "chmod":
                        stack.enter_context(patch.object(install.os, "chmod", side_effect=PermissionError("chmod denied")))
                    with self.assertRaisesRegex((RuntimeError, OSError), {
                        "timeout": "socket read timed out", "http": "503 upstream unavailable",
                        "write": "disk full", "replace": "replace denied", "chmod": "chmod denied",
                    }[failure]):
                        install.download_online_script_logged(self.item(), "", base_dir / "install.log", force=True)
                self.assertEqual(target.read_bytes(), b"original")
                response.close.assert_called_once()
                self.assertEqual(list(target.parent.glob(".fls-download-*.part")), [])

    def test_stop_after_last_chunk_prevents_publication(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import install
            from fls_manager.online_scripts.constants import ONLINE_INSTALL_STOPPING

            target = install.online_script_target(self.item())
            target.write_bytes(b"original")

            def chunks():
                yield b"complete replacement"
                ONLINE_INSTALL_STOPPING.add("cancel")

            with self.response(install, chunks()):
                with self.assertRaises(install.InstallStopped):
                    install.download_online_script_logged(self.item(), "", base_dir / "install.log", force=True, install_id="cancel")
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(list(target.parent.glob(".fls-download-*.part")), [])

    def test_concurrent_publication_never_mixes_data_or_overwrites_without_force(self):
        for force in (False, True):
            with self.subTest(force=force), isolated_app() as (_app, base_dir):
                from fls_manager.online_scripts import install

                target = install.online_script_target(self.item())
                barrier = threading.Barrier(2)
                outcomes, responses = [], []

                def get(*_args, **_kwargs):
                    label = threading.current_thread().name.encode()

                    def chunks(**_kwargs):
                        yield label + b"-first\n"
                        barrier.wait(timeout=3)
                        yield label + b"-second\n"

                    response = Mock()
                    response.iter_content.side_effect = chunks
                    responses.append(response)
                    return response

                def download():
                    try:
                        install.download_online_script_logged(self.item(), "", base_dir / "install.log", force=force)
                        outcomes.append("ok")
                    except Exception as e:
                        outcomes.append(e)

                with patch.object(install, "requests", SimpleNamespace(get=get)), patch.object(
                    install, "github_proxy_url", side_effect=lambda url, *_args, **_kwargs: url,
                ):
                    threads = [threading.Thread(target=download, name=name) for name in ("A", "B")]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=5)
                    self.assertFalse(any(thread.is_alive() for thread in threads))
                self.assertEqual(outcomes.count("ok"), 2 if force else 1, outcomes)
                if not force:
                    self.assertEqual(sum(isinstance(value, FileExistsError) for value in outcomes), 1)
                self.assertIn(target.read_bytes(), (b"A-first\nA-second\n", b"B-first\nB-second\n"))
                self.assertEqual(list(target.parent.glob(".fls-download-*.part")), [])
                for response in responses:
                    response.close.assert_called_once()

    def test_retry_keeps_unpublished_restart_artifact_untouched(self):
        with isolated_app() as (_app, base_dir):
            from fls_manager.online_scripts import install

            target = install.online_script_target(self.item())
            target.write_bytes(b"original")
            stale = target.with_name(".fls-download-stale.part")
            stale.write_bytes(b"unpublished partial data")
            with self.response(install, [b"complete"]):
                install.download_online_script_logged(self.item(), "", base_dir / "install.log", force=True)
            self.assertEqual(target.read_bytes(), b"complete")
            self.assertEqual(stale.read_bytes(), b"unpublished partial data")

    def test_raw_worker_failure_and_stop_persist_without_damaging_script(self):
        for stop in (False, True):
            with self.subTest(stop=stop), isolated_app() as (app, base_dir):
                from fls_manager.online_scripts import install
                from fls_manager.online_scripts.constants import ONLINE_INSTALL_RUNNING, get_online_install_job

                target = install.online_script_target(self.item())
                target.write_bytes(b"original")
                ONLINE_INSTALL_RUNNING["raw-result"] = {
                    "id": "raw-result", "script_id": "raw-demo", "running": True,
                    "log_file": str(base_dir / "install.log"), "status": "准备中", "error": "",
                }

                def chunks():
                    yield b"partial"
                    if stop:
                        response = app.test_client().post(
                            "/api/online-scripts/install-stop/raw-result", headers={"X-Token": TOKEN},
                        )
                        self.assertEqual(response.status_code, 200)
                        yield b"second chunk"
                    else:
                        raise TimeoutError("socket read timed out")

                with self.response(install, chunks()):
                    install.install_worker("raw-result", self.item(), force=True)
                ONLINE_INSTALL_RUNNING.clear()
                info = get_online_install_job("raw-result")
                self.assertEqual(info["status"], "已停止" if stop else "失败")
                self.assertFalse(info["running"])
                self.assertIsNone(info["returncode"])
                self.assertIn("安装已停止" if stop else "socket read timed out", info["error"])
                self.assertEqual(target.read_bytes(), b"original")
                response = app.test_client().get("/api/online-scripts/log/raw-result", headers={"X-Token": TOKEN})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json["status"], info["status"])
                self.assertEqual(response.json["error"], info["error"])


if __name__ == "__main__":
    unittest.main()
