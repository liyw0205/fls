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


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


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
    def test_notify_items_cleans_invalid_rows_and_persists_defaults(self):
        with isolated_fls_modules():
            from fls_manager import notify, paths

            write_json(
                paths.CONFIG_FILE,
                {
                    "notify_items": [
                        "bad row",
                        {"id": "bad-channel", "channel": "missing"},
                        {
                            "channel": "telegram",
                            "config": "bad config",
                        },
                        {
                            "id": "n1",
                            "channel": "bark",
                            "name": "Bark One",
                            "enabled": False,
                            "config": {"BARK_PUSH": "token"},
                        },
                    ],
                },
            )

            fake_uuid = mock.Mock(hex="generated-id")
            with mock.patch.object(notify.uuid, "uuid4", return_value=fake_uuid):
                items = notify.notify_items()

            self.assertEqual(len(items), 2)
            self.assertEqual(
                items[0],
                {
                    "channel": "telegram",
                    "config": {},
                    "id": "generated-id",
                    "enabled": True,
                    "name": "Telegram Bot",
                },
            )
            self.assertEqual(
                items[1],
                {
                    "id": "n1",
                    "channel": "bark",
                    "name": "Bark One",
                    "enabled": False,
                    "config": {"BARK_PUSH": "token"},
                },
            )
            self.assertEqual(read_json(paths.CONFIG_FILE)["notify_items"], items)

    def test_default_notify_ids_filters_enabled_items_and_save_deduplicates(self):
        with isolated_fls_modules():
            from fls_manager import notify, paths

            write_json(
                paths.CONFIG_FILE,
                {
                    "notify_items": [
                        {
                            "id": "n1",
                            "channel": "bark",
                            "name": "One",
                            "enabled": True,
                            "config": {},
                        },
                        {
                            "id": "n2",
                            "channel": "telegram",
                            "name": "Two",
                            "enabled": True,
                            "config": {},
                        },
                        {
                            "id": "disabled",
                            "channel": "serverj",
                            "name": "Disabled",
                            "enabled": False,
                            "config": {},
                        },
                    ],
                    "notify_default_ids": ["n2", "n1", "n1", "disabled", "missing"],
                },
            )

            self.assertEqual(notify.default_notify_ids(), ["n2", "n1"])

            notify.save_default_notify_ids(["n2", "n1", "n2", "disabled", "missing"])
            self.assertEqual(read_json(paths.CONFIG_FILE)["notify_default_ids"], ["n2", "n1"])

            write_json(paths.CONFIG_FILE, {"notify_default_ids": "bad"})
            self.assertEqual(notify.default_notify_ids(), [])

    def test_split_content_handles_empty_exact_limit_and_separator_cut(self):
        with isolated_fls_modules():
            from fls_manager import notify

            self.assertEqual(notify.split_content(""), [""])
            self.assertEqual(notify.split_content(None), [""])

            exact = "a" * 2000
            self.assertEqual(notify.split_content(exact), [exact])

            no_separator = "a" * 2001
            self.assertEqual(notify.split_content(no_separator), ["a" * 2000, "a"])

            with_separator = ("a" * 1998) + "\n  second"
            self.assertEqual(
                notify.split_content(with_separator),
                ["a" * 1998, "second"],
            )

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

    def test_send_one_wxpusher_uses_html_payload_and_requires_target(self):
        with isolated_fls_modules():
            from fls_manager import notify

            item = {
                "channel": "wxpusher",
                "enabled": True,
                "config": {
                    "WXPUSHER_APP_TOKEN": "token",
                    "WXPUSHER_TOPIC_IDS": "1; 2",
                    "WXPUSHER_UIDS": "UID_a; UID_b",
                },
            }

            with mock.patch.object(
                notify.requests,
                "post",
                return_value=FakeResponse(json_data={"code": 1000}),
            ) as post:
                ok, _ = notify.send_one(item, "<Title>", "a <tag> & b")

            self.assertTrue(ok)
            post.assert_called_once()
            self.assertEqual(
                post.call_args.args[0],
                "https://wxpusher.zjiecode.com/api/send/message",
            )
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["appToken"], "token")
            self.assertEqual(payload["topicIds"], [1, 2])
            self.assertEqual(payload["uids"], ["UID_a", "UID_b"])
            self.assertEqual(payload["contentType"], 2)
            self.assertEqual(payload["verifyPayType"], 0)
            self.assertEqual(payload["summary"], "<Title>")
            self.assertIn("&lt;Title&gt;", payload["content"])
            self.assertIn("a &lt;tag&gt; &amp; b", payload["content"])

            missing_target = {
                "channel": "wxpusher",
                "enabled": True,
                "config": {"WXPUSHER_APP_TOKEN": "token"},
            }
            with mock.patch.object(notify.requests, "post") as post:
                ok, msg = notify.send_one(missing_target, "Title", "content")

            self.assertFalse(ok)
            self.assertEqual(msg, "WXPUSHER_TOPIC_IDS 和 WXPUSHER_UIDS 至少填写一个")
            post.assert_not_called()

    def test_send_by_ids_sends_all_items_for_each_content_chunk(self):
        with isolated_fls_modules():
            from fls_manager import notify

            items = [
                {"id": "n1", "name": "One", "enabled": True},
                {"id": "n2", "name": "Two", "enabled": True},
            ]
            by_id = {item["id"]: item for item in items}

            with mock.patch.object(notify, "enabled_notify_items", return_value=items), \
                    mock.patch.object(notify, "get_notify_item", side_effect=by_id.get), \
                    mock.patch.object(notify, "split_content", return_value=["part1", "part2"]), \
                    mock.patch.object(
                        notify,
                        "send_one",
                        side_effect=[
                            (True, "ok1"),
                            (False, "bad2"),
                            (True, "ok3"),
                            (True, "ok4"),
                        ],
                    ) as send_one, mock.patch("builtins.print"):
                results = notify.send_by_ids("Title", "content", ["n1", "n2"])

            self.assertEqual(
                send_one.call_args_list,
                [
                    mock.call(items[0], "Title [1/2]", "part1"),
                    mock.call(items[1], "Title [1/2]", "part1"),
                    mock.call(items[0], "Title [2/2]", "part2"),
                    mock.call(items[1], "Title [2/2]", "part2"),
                ],
            )
            self.assertEqual([result["id"] for result in results], ["n1", "n2", "n1", "n2"])
            self.assertEqual([result["name"] for result in results], ["One", "Two", "One", "Two"])
            self.assertEqual([result["ok"] for result in results], [True, False, True, True])


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

    def test_git_command_helpers_use_github_proxy_when_enabled(self):
        with isolated_fls_modules():
            from fls_manager import proxy

            proxies = [
                {
                    "id": "gh",
                    "type": "github",
                    "url": "https://gh.example/",
                    "enabled": True,
                },
                {
                    "id": "http",
                    "type": "http",
                    "host": "proxy.local",
                    "port": "8080",
                    "enabled": True,
                },
                {
                    "id": "disabled",
                    "type": "github",
                    "url": "https://disabled.example/",
                    "enabled": False,
                },
            ]

            with mock.patch.object(proxy, "load_proxies", return_value=proxies):
                self.assertEqual(
                    proxy.build_git_command_with_github_proxy(
                        "git",
                        "gh",
                        ["clone", "https://github.com/a/b.git"],
                        verify=False,
                    ),
                    [
                        "git",
                        "-c",
                        "url.https://gh.example/https://github.com/.insteadOf=https://github.com/",
                        "clone",
                        "https://github.com/a/b.git",
                    ],
                )
                self.assertTrue(proxy.github_git_proxy_used("gh", verify=False))
                self.assertEqual(
                    proxy.build_git_command_with_github_proxy(
                        "git",
                        "http",
                        ["clone", "https://github.com/a/b.git"],
                        verify=False,
                    ),
                    ["git", "clone", "https://github.com/a/b.git"],
                )
                self.assertFalse(proxy.github_git_proxy_used("http", verify=False))
                self.assertFalse(proxy.github_git_proxy_used("disabled", verify=False))
                self.assertFalse(proxy.github_git_proxy_used("missing", verify=False))

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

    def test_quality_github_proxy_object_handles_empty_proxy_url(self):
        with isolated_fls_modules():
            from fls_manager import proxy

            result = proxy.quality_github_proxy_object({"type": "github", "url": ""})

            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["url"], "GitHub 代理地址")
            self.assertFalse(result[0]["ok"])
            self.assertEqual(result[0]["status_code"], "-")
            self.assertEqual(result[0]["elapsed"], "GitHub 代理地址为空")

    def test_quality_github_proxy_object_records_concat_success_without_git(self):
        with isolated_fls_modules():
            from fls_manager import proxy

            github_proxy = {"type": "github", "url": "https://gh.example/"}

            with mock.patch.object(
                proxy.requests,
                "get",
                return_value=FakeResponse(status_code=200, text="01234567890"),
            ) as get, mock.patch.object(proxy.shutil, "which", return_value=None) as which:
                result = proxy.quality_github_proxy_object(github_proxy)

            self.assertEqual(len(result), 2)
            self.assertTrue(result[0]["ok"])
            self.assertEqual(result[0]["status_code"], 200)
            self.assertTrue(result[0]["url"].startswith("拼接方式："))
            get.assert_called_once()
            self.assertEqual(
                get.call_args.args[0],
                "https://gh.example/https://github.com/liyw0205/fls-scripts/raw/refs/heads/main/index.json",
            )
            self.assertEqual(get.call_args.kwargs["timeout"], 15)
            which.assert_called_once_with("git")
            self.assertFalse(result[1]["ok"])
            self.assertEqual(result[1]["status_code"], "-")
            self.assertEqual(result[1]["elapsed"], "未安装 git")

    def test_quality_github_proxy_object_records_concat_failure(self):
        with isolated_fls_modules():
            from fls_manager import proxy

            github_proxy = {"type": "github", "url": "https://gh.example/"}

            with mock.patch.object(
                proxy.requests,
                "get",
                side_effect=RuntimeError("down"),
            ), mock.patch.object(proxy.shutil, "which", return_value=None):
                result = proxy.quality_github_proxy_object(github_proxy)

            self.assertEqual(len(result), 2)
            self.assertFalse(result[0]["ok"])
            self.assertEqual(result[0]["status_code"], "-")
            self.assertIn("down", result[0]["elapsed"])
            self.assertFalse(result[1]["ok"])
            self.assertEqual(result[1]["elapsed"], "未安装 git")

    def test_quality_github_proxy_object_runs_git_instead_of_check(self):
        with isolated_fls_modules():
            from fls_manager import proxy

            github_proxy = {"type": "github", "url": "https://gh.example/"}
            completed = mock.Mock(returncode=0, stdout="hash\trefs/heads/main\n")

            with mock.patch.object(
                proxy.requests,
                "get",
                return_value=FakeResponse(status_code=200, text="01234567890"),
            ), mock.patch.object(
                proxy.shutil,
                "which",
                return_value="/usr/bin/git",
            ), mock.patch.object(
                proxy.subprocess,
                "run",
                return_value=completed,
            ) as run:
                result = proxy.quality_github_proxy_object(github_proxy)

            self.assertTrue(result[1]["ok"])
            self.assertEqual(result[1]["status_code"], 0)
            run.assert_called_once()
            self.assertEqual(
                run.call_args.args[0],
                [
                    "/usr/bin/git",
                    "-c",
                    "url.https://gh.example/https://github.com/.insteadOf=https://github.com/",
                    "ls-remote",
                    "--heads",
                    "https://github.com/liyw0205/fls-scripts.git",
                ],
            )
            self.assertEqual(run.call_args.kwargs["stdout"], proxy.subprocess.PIPE)
            self.assertEqual(run.call_args.kwargs["stderr"], proxy.subprocess.STDOUT)
            self.assertTrue(run.call_args.kwargs["text"])
            self.assertEqual(run.call_args.kwargs["timeout"], 25)

    def test_quality_github_proxy_object_records_git_failure_and_timeout(self):
        with isolated_fls_modules():
            from fls_manager import proxy

            github_proxy = {"type": "github", "url": "https://gh.example/"}

            with mock.patch.object(
                proxy.requests,
                "get",
                return_value=FakeResponse(status_code=200, text="01234567890"),
            ), mock.patch.object(
                proxy.shutil,
                "which",
                return_value="/usr/bin/git",
            ), mock.patch.object(
                proxy.subprocess,
                "run",
                return_value=mock.Mock(returncode=128, stdout="fatal error"),
            ):
                result = proxy.quality_github_proxy_object(github_proxy)

            self.assertFalse(result[1]["ok"])
            self.assertEqual(result[1]["status_code"], 128)
            self.assertIn("fatal error", result[1]["elapsed"])

            with mock.patch.object(
                proxy.requests,
                "get",
                return_value=FakeResponse(status_code=200, text="01234567890"),
            ), mock.patch.object(
                proxy.shutil,
                "which",
                return_value="/usr/bin/git",
            ), mock.patch.object(
                proxy.subprocess,
                "run",
                side_effect=proxy.subprocess.TimeoutExpired("git", 25),
            ):
                result = proxy.quality_github_proxy_object(github_proxy)

            self.assertFalse(result[1]["ok"])
            self.assertEqual(result[1]["status_code"], "-")
            self.assertIn("timed out", result[1]["elapsed"])


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

    def test_latest_log_for_task_uses_mtime_and_safe_name_prefix(self):
        with isolated_fls_modules():
            from fls_manager import logs, paths

            paths.LOG_DIR.mkdir(parents=True, exist_ok=True)

            older = paths.LOG_DIR / "Demo-older.log"
            newer = paths.LOG_DIR / "Demo-newer.log"
            other = paths.LOG_DIR / "Other-newer.log"

            older.write_text("old", encoding="utf-8")
            newer.write_text("new", encoding="utf-8")
            other.write_text("other", encoding="utf-8")
            os.utime(older, (100, 100))
            os.utime(newer, (200, 200))
            os.utime(other, (300, 300))

            self.assertEqual(logs.latest_log_for_task({"name": "Demo"}), str(newer))
            self.assertEqual(logs.latest_log_for_task({"name": "Missing"}), "")

    def test_cleanup_logs_keep_zero_removes_all_logs_for_task(self):
        with isolated_fls_modules():
            from fls_manager import logs, paths

            paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
            first = paths.LOG_DIR / "task-first.log"
            second = paths.LOG_DIR / "task-second.log"

            for path, mtime in ((first, 100), (second, 200)):
                path.write_text("===== 启动任务: Demo =====\nbody\n", encoding="utf-8")
                os.utime(path, (mtime, mtime))

            with mock.patch.object(
                logs,
                "load_config",
                return_value={"log_keep_per_task": 0, "log_max_size_mb": 10},
            ):
                logs.cleanup_logs()

            self.assertFalse(first.exists())
            self.assertFalse(second.exists())

    def test_cleanup_logs_invalid_numeric_config_raises_value_error(self):
        with isolated_fls_modules():
            from fls_manager import logs

            with mock.patch.object(
                logs,
                "load_config",
                return_value={"log_keep_per_task": "bad", "log_max_size_mb": 10},
            ):
                with self.assertRaises(ValueError):
                    logs.cleanup_logs()

            with mock.patch.object(
                logs,
                "load_config",
                return_value={"log_keep_per_task": 10, "log_max_size_mb": "bad"},
            ):
                with self.assertRaises(ValueError):
                    logs.cleanup_logs()

    def test_cleanup_logs_groups_missing_headers_as_other_logs(self):
        with isolated_fls_modules():
            from fls_manager import logs, paths

            paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
            old_log = paths.LOG_DIR / "plain-old.log"
            mid_log = paths.LOG_DIR / "plain-mid.log"
            new_log = paths.LOG_DIR / "plain-new.log"

            for path, mtime in ((old_log, 100), (mid_log, 200), (new_log, 300)):
                path.write_text("plain body\n", encoding="utf-8")
                os.utime(path, (mtime, mtime))

            with mock.patch.object(
                logs,
                "load_config",
                return_value={"log_keep_per_task": 2, "log_max_size_mb": 10},
            ):
                logs.cleanup_logs()

            self.assertFalse(old_log.exists())
            self.assertTrue(mid_log.exists())
            self.assertTrue(new_log.exists())

    def test_cleanup_logs_swallows_unlink_errors(self):
        with isolated_fls_modules():
            from fls_manager import logs, paths

            paths.LOG_DIR.mkdir(parents=True, exist_ok=True)
            old_log = paths.LOG_DIR / "locked-old.log"
            new_log = paths.LOG_DIR / "locked-new.log"

            for path, mtime in ((old_log, 100), (new_log, 200)):
                path.write_text("===== 启动任务: Demo =====\nbody\n", encoding="utf-8")
                os.utime(path, (mtime, mtime))

            with mock.patch.object(
                logs,
                "load_config",
                return_value={"log_keep_per_task": 1, "log_max_size_mb": 10},
            ), mock.patch.object(Path, "unlink", side_effect=OSError("locked")) as unlink:
                logs.cleanup_logs()

            self.assertGreaterEqual(unlink.call_count, 1)


if __name__ == "__main__":
    unittest.main()
