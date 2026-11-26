import json
import smtplib
import time
from dataclasses import dataclass

from .utils import LazyModule

requests = LazyModule("requests")
MAX_RESPONSE = 64 * 1024
RESPONSE_SECONDS = 30


@dataclass(frozen=True)
class SendResult:
    status: str
    code: str


class _ResponseLimit(Exception):
    pass


class _Response:
    def __init__(self, status, body):
        self.status_code = status
        self.text = body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)


class _Transport:
    def __init__(self, client, clock):
        self.client, self.clock = client, clock
        self.attempted = False
        self.response = None
        self.error = ""

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        self.attempted = True
        started = self.clock()
        kwargs.update(timeout=(5, 10), stream=True, allow_redirects=False)
        response = None
        try:
            response = self.client.request(method=method, url=url, **kwargs)
            if 300 <= response.status_code < 400:
                self.error = "redirect"
                raise ValueError("redirect")
            size = response.headers.get("Content-Length", "")
            if size.isdigit() and int(size) > MAX_RESPONSE:
                raise _ResponseLimit()
            body = bytearray()
            # One-byte reads let the deadline observe a slowly dripping body.
            # DNS/headers/SMTP still have phase timeouts, not a hard job deadline.
            for chunk in response.iter_content(chunk_size=1):
                if self.clock() - started > RESPONSE_SECONDS:
                    self.error = "timeout"
                    raise TimeoutError()
                body.extend(chunk)
                if len(body) > MAX_RESPONSE:
                    raise _ResponseLimit()
            self.response = _Response(response.status_code, body)
            return self.response
        except _ResponseLimit:
            self.error = "response_limit"
            raise
        except (TimeoutError, requests.exceptions.Timeout):
            self.error = "timeout"
            raise
        except Exception:
            self.error = self.error or "network_error"
            raise
        finally:
            if response is not None:
                response.close()


class _Smtp:
    def __init__(self, module):
        self.module = module
        self.attempted = False
        self.accepted = False
        self.connection = None

    def _open(self, factory, *args, **kwargs):
        self.attempted = True
        kwargs["timeout"] = 20
        self.connection = factory(*args, **kwargs)
        return self

    def SMTP(self, *args, **kwargs):
        return self._open(self.module.SMTP, *args, **kwargs)

    def SMTP_SSL(self, *args, **kwargs):
        return self._open(self.module.SMTP_SSL, *args, **kwargs)

    def login(self, *args):
        return self.connection.login(*args)

    def sendmail(self, *args):
        refused = self.connection.sendmail(*args)
        self.accepted = isinstance(refused, dict) and not refused
        return refused

    def close(self):
        if self.connection is not None:
            connection, self.connection = self.connection, None
            connection.close()


def _accepted(channel, response):
    if not 200 <= response.status_code < 300:
        return False
    if channel == "webhook":
        return True
    if channel == "ntfy":
        return response.status_code in (200, 201, 202)
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("invalid response")

    def code(name, value):
        return type(data.get(name)) is int and data[name] == value

    if channel in ("bark", "pushplus"):
        return code("code", 200)
    if channel == "serverj":
        return code("errno", 0) or code("code", 0)
    if channel == "telegram":
        return data.get("ok") is True
    if channel in ("qywxbot", "dingding"):
        return code("errcode", 0)
    if channel == "feishu":
        return code("StatusCode", 0) or code("code", 0)
    if channel == "wxpusher":
        return code("code", 1000)
    if channel == "gotify":
        return type(data.get("id")) is int and data["id"] > 0
    if channel == "pushdeer":
        content = data.get("content")
        results = content.get("result") if isinstance(content, dict) else None
        return code("code", 0) and isinstance(results, list) and bool(results)
    return False


def send_native(item, title, content, *, client=None, smtp_module=None, clock=time.monotonic):
    from .notify import send_one
    transport = _Transport(client if client is not None else requests, clock)
    smtp = _Smtp(smtp_module if smtp_module is not None else smtplib)
    try:
        send_one(item, title, content, transport=transport, smtp_module=smtp)
        if smtp.accepted:
            return SendResult("succeeded", "accepted")
        if transport.response is not None:
            try:
                accepted = _accepted(item["channel"], transport.response)
            except (ValueError, TypeError, KeyError):
                return SendResult("unknown", "invalid_response")
            return SendResult("succeeded", "accepted") if accepted else SendResult("unknown", "upstream_rejected")
        if transport.attempted or smtp.attempted:
            return SendResult("unknown", transport.error or "network_error")
        return SendResult("failed", "invalid_config")
    finally:
        try:
            smtp.close()
        except Exception:
            pass
