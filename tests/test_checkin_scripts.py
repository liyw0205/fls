import importlib.util
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CheckinHandler(BaseHTTPRequestHandler):
    state_payload = {}
    post_payload = {}
    post_count = 0
    response_status = 200
    response_body = None

    def do_GET(self):
        self._respond(self.state_payload)

    def do_POST(self):
        type(self).post_count += 1
        self._respond(self.post_payload)

    def _respond(self, payload):
        self.send_response(type(self).response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if type(self).response_body is not None:
            body = type(self).response_body
        else:
            body = json.dumps(payload).encode("utf-8")
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class CheckinScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), CheckinHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        CheckinHandler.post_count = 0
        CheckinHandler.response_status = 200
        CheckinHandler.response_body = None

    def test_wisart_success_and_request_headers(self):
        module = load_script("wisart_checkin.py")
        CheckinHandler.state_payload = {
            "points": 10,
            "daily_checkin": {"enabled": True, "eligible": True, "signed_today": False},
        }
        CheckinHandler.post_payload = {
            "ok": True,
            "added": 120,
            "balance": 130,
            "daily_checkin": {"signed_today": True},
        }
        args = SimpleNamespace(
            base_url=self.base_url,
            cookie="wisart_session=test",
            cookie_file=None,
            timeout=2,
            dry_run=False,
        )
        payload = module.run(args)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["added"], 120)
        self.assertEqual(CheckinHandler.post_count, 1)

    def test_wisart_already_signed_does_not_post(self):
        module = load_script("wisart_checkin.py")
        CheckinHandler.state_payload = {
            "points": 130,
            "daily_checkin": {"enabled": True, "eligible": False, "signed_today": True},
        }
        args = SimpleNamespace(
            base_url=self.base_url,
            cookie="wisart_session=test",
            cookie_file=None,
            timeout=2,
            dry_run=False,
        )
        payload = module.run(args)
        self.assertEqual(payload["status"], "already_signed")
        self.assertEqual(CheckinHandler.post_count, 0)

    def test_lupi_success_and_already_signed(self):
        module = load_script("lupi_checkin.py")
        CheckinHandler.state_payload = {"points": 20, "enabled": True, "checked_in_today": False}
        CheckinHandler.post_payload = {
            "awarded": True,
            "reward": 257,
            "points": 277,
            "checked_in_today": True,
        }
        args = SimpleNamespace(
            base_url=self.base_url,
            cookie="chatgpt2api_session=test",
            cookie_file=None,
            timeout=2,
            dry_run=False,
        )
        payload = module.run(args)
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["reward"], 257)
        self.assertEqual(CheckinHandler.post_count, 1)

        CheckinHandler.state_payload = {"points": 277, "enabled": True, "checked_in_today": True}
        payload = module.run(args)
        self.assertEqual(payload["status"], "already_signed")
        self.assertEqual(CheckinHandler.post_count, 1)

    def test_non_json_response_is_reported(self):
        module = load_script("lupi_checkin.py")
        CheckinHandler.response_body = b"upstream unavailable"
        args = SimpleNamespace(
            base_url=self.base_url,
            cookie="chatgpt2api_session=test",
            cookie_file=None,
            timeout=2,
            dry_run=False,
        )
        with self.assertRaises(module.CheckinError) as ctx:
            module.run(args)
        self.assertIn("非 JSON", str(ctx.exception))

    def test_explicit_missing_cookie_file_fails_without_default_fallback(self):
        module = load_script("wisart_checkin.py")
        with self.assertRaises(module.CheckinError) as ctx:
            module.resolve_cookie(None, "/definitely/missing/fls-cookie.txt")
        self.assertIn("凭据文件不存在", str(ctx.exception))

    def test_missing_cookie_fails_without_implicit_file_lookup(self):
        module = load_script("wisart_checkin.py")
        old_cookie = os.environ.pop(module.COOKIE_ENV, None)
        old_file = os.environ.pop("SIGNIN_COOKIE_FILE", None)
        try:
            with self.assertRaises(module.CheckinError) as ctx:
                module.resolve_cookie(None, None)
            self.assertIn("未找到 Cookie", str(ctx.exception))
        finally:
            if old_cookie is not None:
                os.environ[module.COOKIE_ENV] = old_cookie
            if old_file is not None:
                os.environ["SIGNIN_COOKIE_FILE"] = old_file


if __name__ == "__main__":
    unittest.main()
