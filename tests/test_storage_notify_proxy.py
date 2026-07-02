import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]


@contextlib.contextmanager
def isolated_fls_modules():
    keys = ("FLS_BASE_DIR", "PYTHONDONTWRITEBYTECODE")
    old_env = {key: os.environ.get(key) for key in keys}

    with tempfile.TemporaryDirectory(prefix="fls-storage-notify-proxy-test-") as temp_dir:
        os.environ["FLS_BASE_DIR"] = temp_dir
        os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
        purge_fls_modules()

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))

        try:
            yield Path(temp_dir)
        finally:
            shutdown_scheduler()
            purge_fls_modules()

            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def purge_fls_modules():
    for name in list(sys.modules):
        if name == "fls_manager" or name.startswith("fls_manager."):
            sys.modules.pop(name, None)


def shutdown_scheduler():
    state = sys.modules.get("fls_manager.state")
    scheduler = getattr(state, "scheduler", None) if state else None

    if scheduler is not None:
        with contextlib.suppress(Exception):
            scheduler.shutdown(wait=False)


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status_code=200, text="OK", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")

        return self._json_data


class StorageTests(unittest.TestCase):
    def test_read_json_returns_default_for_missing_or_bad_json(self):
        with isolated_fls_modules() as base_dir:
            from fls_manager import storage

            default = {"fallback": True}
            missing = base_dir / "data" / "missing.json"
            broken = base_dir / "data" / "broken.json"
            broken.parent.mkdir(parents=True, exist_ok=True)
            broken.write_text("{bad json", encoding="utf-8")

            self.assertIs(storage.read_json(missing, default), default)
            self.assertIs(storage.read_json(broken, default), default)

    def test_write_json_creates_parent_and_replaces_tmp_file(self):
        with isolated_fls_modules() as base_dir:
            from fls_manager import storage

            target = base_dir / "nested" / "data.json"

            storage.write_json(target, {"version": 1})
            storage.write_json(target, {"version": 2, "items": ["a", "b"]})

            self.assertEqual(read_json(target), {"version": 2, "items": ["a", "b"]})
            self.assertFalse(target.with_name(target.name + ".tmp").exists())

    def test_write_json_replace_failure_preserves_existing_file(self):
        with isolated_fls_modules() as base_dir:
            from fls_manager import storage

            target = base_dir / "data" / "state.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"version": 1}), encoding="utf-8")

            with mock.patch.object(Path, "replace", side_effect=OSError("locked")):
                with self.assertRaises(OSError):
                    storage.write_json(target, {"version": 2})

            tmp_file = target.with_name(target.name + ".tmp")
            self.assertEqual(read_json(target), {"version": 1})
            self.assertEqual(read_json(tmp_file), {"version": 2})


class NotifyChannelTests(unittest.TestCase):
    def test_send_one_serverj_uses_post_form(self):
        with isolated_fls_modules():
            from fls_manager import notify

            item = {
                "channel": "serverj",
                "enabled": True,
                "config": {"PUSH_KEY": "SCTKEY"},
            }

            with mock.patch.object(
                notify.requests,
                "post",
                return_value=FakeResponse(json_data={"errno": 0}),
            ) as post:
                ok, msg = notify.send_one(item, "Title", "line1\nline2")

            self.assertTrue(ok)
            self.assertIn("'errno': 0", msg)
            post.assert_called_once_with(
                "https://sctapi.ftqq.com/SCTKEY.send",
                data={"text": "Title", "desp": "line1\n\nline2"},
                timeout=15,
            )

    def test_send_one_pushplus_uses_json_payload(self):
        with isolated_fls_modules():
            from fls_manager import notify

            item = {
                "channel": "pushplus",
                "enabled": True,
                "config": {
                    "PUSH_PLUS_TOKEN": "token",
                    "PUSH_PLUS_USER": "topic",
                    "PUSH_PLUS_TEMPLATE": "markdown",
                },
            }

            with mock.patch.object(
                notify.requests,
                "post",
                return_value=FakeResponse(json_data={"code": 200}),
            ) as post:
                ok, _ = notify.send_one(item, "Title", "content")

            self.assertTrue(ok)
            post.assert_called_once_with(
                "https://www.pushplus.plus/send",
                json={
                    "token": "token",
                    "title": "Title",
                    "content": "content",
                    "topic": "topic",
                    "template": "markdown",
                },
                timeout=15,
            )

    def test_send_one_telegram_uses_custom_api_host(self):
        with isolated_fls_modules():
            from fls_manager import notify

            item = {
                "channel": "telegram",
                "enabled": True,
                "config": {
                    "TG_BOT_TOKEN": "bot-token",
                    "TG_USER_ID": "chat-id",
                    "TG_API_HOST": "https://tg.example/",
                },
            }

            with mock.patch.object(
                notify.requests,
                "post",
                return_value=FakeResponse(json_data={"ok": True}),
            ) as post:
                ok, _ = notify.send_one(item, "Title", "content")

            self.assertTrue(ok)
            post.assert_called_once_with(
                "https://tg.example/botbot-token/sendMessage",
                data={
                    "chat_id": "chat-id",
                    "text": "Title\n\ncontent",
                    "disable_web_page_preview": "true",
                },
                timeout=20,
            )

    def test_send_one_qywxbot_uses_text_message(self):
        with isolated_fls_modules():
            from fls_manager import notify

            item = {
                "channel": "qywxbot",
                "enabled": True,
                "config": {
                    "QYWX_KEY": "key",
                    "QYWX_ORIGIN": "https://wx.example/",
                },
            }

            with mock.patch.object(
                notify.requests,
                "post",
                return_value=FakeResponse(json_data={"errcode": 0}),
            ) as post:
                ok, _ = notify.send_one(item, "Title", "content")

            self.assertTrue(ok)
            post.assert_called_once_with(
                "https://wx.example/cgi-bin/webhook/send?key=key",
                json={"msgtype": "text", "text": {"content": "Title\n\ncontent"}},
                timeout=15,
            )

    def test_send_one_dingding_adds_signed_url(self):
        with isolated_fls_modules():
            from fls_manager import notify

            item = {
                "channel": "dingding",
                "enabled": True,
                "config": {
                    "DD_BOT_TOKEN": "token",
                    "DD_BOT_SECRET": "secret",
                },
            }

            with mock.patch.object(notify.time, "time", return_value=1234.567), \
                    mock.patch.object(
                        notify.requests,
                        "post",
                        return_value=FakeResponse(json_data={"errcode": 0}),
                    ) as post:
                ok, _ = notify.send_one(item, "Title", "content")

            self.assertTrue(ok)
            url = post.call_args.args[0]
            self.assertIn("access_token=token", url)
            self.assertIn("timestamp=1234567", url)
            self.assertIn("sign=", url)
            self.assertEqual(
                post.call_args.kwargs["json"],
                {"msgtype": "text", "text": {"content": "Title\n\ncontent"}},
            )

    def test_send_one_feishu_adds_signature_when_secret_is_set(self):
        with isolated_fls_modules():
            from fls_manager import notify

            item = {
                "channel": "feishu",
                "enabled": True,
                "config": {
                    "FSKEY": "hook-key",
                    "FSSECRET": "secret",
                },
            }

            with mock.patch.object(notify.time, "time", return_value=100), \
                    mock.patch.object(
                        notify.requests,
                        "post",
                        return_value=FakeResponse(json_data={"StatusCode": 0}),
                    ) as post:
                ok, _ = notify.send_one(item, "Title", "content")

            self.assertTrue(ok)
            post.assert_called_once()
            self.assertEqual(
                post.call_args.args[0],
                "https://open.feishu.cn/open-apis/bot/v2/hook/hook-key",
            )
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["msg_type"], "text")
            self.assertEqual(payload["content"], {"text": "Title\n\ncontent"})
            self.assertEqual(payload["timestamp"], "100")
            self.assertTrue(payload["sign"])

    def test_send_one_ntfy_gotify_and_pushdeer_use_requests_post(self):
        with isolated_fls_modules():
            from fls_manager import notify

            cases = [
                (
                    {
                        "channel": "ntfy",
                        "enabled": True,
                        "config": {
                            "NTFY_URL": "https://ntfy.example/",
                            "NTFY_TOPIC": "topic",
                            "NTFY_PRIORITY": "4",
                            "NTFY_TOKEN": "token",
                        },
                    },
                    FakeResponse(status_code=202, text="accepted"),
                    True,
                ),
                (
                    {
                        "channel": "gotify",
                        "enabled": True,
                        "config": {
                            "GOTIFY_URL": "https://gotify.example/",
                            "GOTIFY_TOKEN": "token",
                            "GOTIFY_PRIORITY": "5",
                        },
                    },
                    FakeResponse(json_data={"id": 1}),
                    True,
                ),
                (
                    {
                        "channel": "pushdeer",
                        "enabled": True,
                        "config": {
                            "DEER_KEY": "key",
                            "DEER_URL": "https://pushdeer.example/push",
                        },
                    },
                    FakeResponse(json_data={"content": {"result": [{"id": 1}]}}),
                    True,
                ),
            ]

            for item, response, expected_ok in cases:
                with self.subTest(channel=item["channel"]):
                    with mock.patch.object(
                        notify.requests,
                        "post",
                        return_value=response,
                    ) as post:
                        ok, _ = notify.send_one(item, "Title", "content")

                    self.assertIs(ok, expected_ok)
                    post.assert_called_once()


class ProxyQualityTests(unittest.TestCase):
    def test_github_proxy_url_from_proxy_respects_type_url_and_health(self):
        with isolated_fls_modules():
            from fls_manager import proxy

            github_proxy = {
                "id": "gh",
                "type": "github",
                "url": "https://gh.example/",
            }
            url = "https://github.com/owner/repo.git"

            self.assertEqual(proxy.github_proxy_url_from_proxy(url, None), url)
            self.assertEqual(
                proxy.github_proxy_url_from_proxy(url, {"type": "http"}),
                url,
            )
            self.assertEqual(
                proxy.github_proxy_url_from_proxy("https://example.com/repo.git", github_proxy),
                "https://example.com/repo.git",
            )
            self.assertEqual(
                proxy.github_proxy_url_from_proxy(url, github_proxy, verify=False),
                "https://gh.example/https://github.com/owner/repo.git",
            )

            with mock.patch.object(
                proxy,
                "github_proxy_available",
                return_value=False,
            ) as available:
                self.assertEqual(proxy.github_proxy_url_from_proxy(url, github_proxy), url)
            available.assert_called_once_with(github_proxy, use_cache=True)

    def test_github_git_config_args_from_proxy_builds_instead_of_rule(self):
        with isolated_fls_modules():
            from fls_manager import proxy

            github_proxy = {
                "id": "gh",
                "type": "github",
                "url": "https://gh.example/",
            }

            self.assertEqual(proxy.github_git_config_args_from_proxy(None), [])
            self.assertEqual(
                proxy.github_git_config_args_from_proxy({"type": "http"}),
                [],
            )
            self.assertEqual(
                proxy.github_git_config_args_from_proxy(github_proxy, verify=False),
                [
                    "-c",
                    "url.https://gh.example/https://github.com/.insteadOf=https://github.com/",
                ],
            )

            with mock.patch.object(
                proxy,
                "github_proxy_available",
                return_value=False,
            ):
                self.assertEqual(proxy.github_git_config_args_from_proxy(github_proxy), [])

    def test_parse_quality_urls_normalizes_and_deduplicates(self):
        with isolated_fls_modules():
            from fls_manager import proxy

            self.assertEqual(
                proxy.parse_quality_urls("example.com, https://api.example.com，http://a.test example.com"),
                [
                    "https://example.com",
                    "https://api.example.com",
                    "http://a.test",
                ],
            )
            self.assertEqual(
                proxy.parse_quality_urls(""),
                [
                    "https://www.baidu.com",
                    "https://www.github.com",
                    "https://raw.githubusercontent.com",
                ],
            )

    def test_quality_proxy_object_uses_requests_proxy_and_preserves_url_order(self):
        with isolated_fls_modules():
            from fls_manager import proxy

            proxy_obj = {
                "id": "p1",
                "type": "http",
                "host": "proxy.local",
                "port": "8080",
            }
            urls = ["https://a.example", "https://b.example", "https://c.example"]

            def fake_get(url, **kwargs):
                if url == "https://b.example":
                    raise RuntimeError("down")

                return FakeResponse(status_code=204 if url.endswith("c.example") else 200)

            with mock.patch.object(proxy.requests, "get", side_effect=fake_get) as get:
                result = proxy.quality_proxy_object(proxy_obj, urls=urls)

            self.assertEqual([item["url"] for item in result], urls)
            self.assertEqual([item["ok"] for item in result], [True, False, True])
            self.assertEqual([item["status_code"] for item in result], [200, "-", 204])
            self.assertEqual(get.call_count, 3)

            for call in get.call_args_list:
                self.assertEqual(
                    call.kwargs["proxies"],
                    {
                        "http": "http://proxy.local:8080",
                        "https": "http://proxy.local:8080",
                    },
                )
                self.assertEqual(call.kwargs["timeout"], 8)


class LogBoundaryTests(unittest.TestCase):
    def test_tail_file_handles_missing_file_and_returns_last_lines(self):
        with isolated_fls_modules() as base_dir:
            from fls_manager import logs

            missing = base_dir / "log" / "missing.log"
            self.assertEqual(logs.tail_file(missing), "暂无日志")

            log_file = base_dir / "log" / "tail.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_bytes("one\ntwo\n".encode("utf-8") + b"\xff\nthree\n")

            self.assertEqual(logs.tail_file(log_file, lines=2), "�\nthree")

    def test_parse_task_name_from_log_reads_header_only(self):
        with isolated_fls_modules() as base_dir:
            from fls_manager import logs

            log_file = base_dir / "log" / "task.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(
                "===== 启动任务: Demo Task =====\nbody\n",
                encoding="utf-8",
            )
            missing_header = base_dir / "log" / "plain.log"
            missing_header.write_text("plain body\n", encoding="utf-8")

            self.assertEqual(logs.parse_task_name_from_log(log_file), "Demo Task")
            self.assertIsNone(logs.parse_task_name_from_log(missing_header))
            self.assertIsNone(logs.parse_task_name_from_log(base_dir / "log" / "missing.log"))


if __name__ == "__main__":
    unittest.main()
