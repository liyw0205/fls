import os
import re
import json
import time
import uuid
import html
import base64
import hmac
import hashlib
import smtplib
import urllib.parse
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

import requests

from .config import load_config, save_config
from .utils import now_str


NOTIFY_CHANNELS = {
    "bark": {
        "name": "Bark",
        "fields": [
            ("BARK_PUSH", "Bark 地址或设备码", "例如：https://api.day.app/xxxx 或 xxxx"),
            ("BARK_GROUP", "分组，可空", ""),
            ("BARK_SOUND", "声音，可空", ""),
            ("BARK_URL", "点击跳转 URL，可空", ""),
        ],
    },
    "serverj": {
        "name": "Server 酱",
        "fields": [
            ("PUSH_KEY", "SendKey", "Server 酱 SendKey"),
        ],
    },
    "pushplus": {
        "name": "PushPlus",
        "fields": [
            ("PUSH_PLUS_TOKEN", "Token", ""),
            ("PUSH_PLUS_USER", "群组编码，可空", ""),
            ("PUSH_PLUS_TEMPLATE", "模板", "html / markdown / txt，默认 html"),
        ],
    },
    "telegram": {
        "name": "Telegram Bot",
        "fields": [
            ("TG_BOT_TOKEN", "Bot Token", ""),
            ("TG_USER_ID", "User ID / Chat ID", ""),
            ("TG_API_HOST", "API Host，可空", "默认：https://api.telegram.org"),
        ],
    },
    "qywxbot": {
        "name": "企业微信机器人",
        "fields": [
            ("QYWX_KEY", "机器人 Key", ""),
            ("QYWX_ORIGIN", "企业微信 API 地址，可空", "默认：https://qyapi.weixin.qq.com"),
        ],
    },
    "dingding": {
        "name": "钉钉机器人",
        "fields": [
            ("DD_BOT_TOKEN", "Access Token", ""),
            ("DD_BOT_SECRET", "加签 Secret", ""),
        ],
    },
    "feishu": {
        "name": "飞书机器人",
        "fields": [
            ("FSKEY", "Webhook Key", ""),
            ("FSSECRET", "签名 Secret，可空", ""),
        ],
    },
    "smtp": {
        "name": "SMTP 邮件",
        "fields": [
            ("SMTP_SERVER", "SMTP 服务器", "例如：smtp.qq.com:465"),
            ("SMTP_SSL", "是否 SSL", "true / false"),
            ("SMTP_EMAIL", "邮箱", ""),
            ("SMTP_PASSWORD", "密码/授权码", ""),
            ("SMTP_NAME", "发件人名称", ""),
            ("SMTP_TO", "收件人，可空", "为空则发送给自己"),
        ],
    },
    "ntfy": {
        "name": "Ntfy",
        "fields": [
            ("NTFY_URL", "Ntfy 地址", "例如：https://ntfy.sh"),
            ("NTFY_TOPIC", "Topic", ""),
            ("NTFY_PRIORITY", "优先级", "1-5，默认 3"),
            ("NTFY_TOKEN", "Token，可空", ""),
        ],
    },
    "wxpusher": {
        "name": "WxPusher",
        "fields": [
            ("WXPUSHER_APP_TOKEN", "App Token", ""),
            ("WXPUSHER_TOPIC_IDS", "Topic IDs", "多个用英文分号 ; 分隔"),
            ("WXPUSHER_UIDS", "UIDs", "多个用英文分号 ; 分隔"),
        ],
    },
    "gotify": {
        "name": "Gotify",
        "fields": [
            ("GOTIFY_URL", "Gotify 地址", "例如：https://push.example.com"),
            ("GOTIFY_TOKEN", "应用 Token", ""),
            ("GOTIFY_PRIORITY", "优先级", "默认 0"),
        ],
    },
    "pushdeer": {
        "name": "PushDeer",
        "fields": [
            ("DEER_KEY", "PushDeer Key", ""),
            ("DEER_URL", "PushDeer URL，可空", "默认：https://api2.pushdeer.com/message/push"),
        ],
    },
    "webhook": {
        "name": "自定义 Webhook",
        "fields": [
            ("WEBHOOK_URL", "请求 URL", "支持 $title / $content"),
            ("WEBHOOK_METHOD", "请求方法", "POST / GET"),
            ("WEBHOOK_CONTENT_TYPE", "Content-Type", "application/json / text/plain / application/x-www-form-urlencoded"),
            ("WEBHOOK_HEADERS", "请求头", "每行一个：Key: Value"),
            ("WEBHOOK_BODY", "请求体", "支持 $title / $content"),
        ],
    },
}


def get_config():
    return load_config()


def save_full_config(cfg):
    save_config(cfg)


def notify_items():
    cfg = get_config()
    items = cfg.get("notify_items")

    if not isinstance(items, list):
        items = []
        cfg["notify_items"] = items
        cfg.setdefault("notify_default_ids", [])
        save_full_config(cfg)

    changed = False

    cleaned = []
    for item in items:
        if not isinstance(item, dict):
            changed = True
            continue

        if item.get("channel") not in NOTIFY_CHANNELS:
            changed = True
            continue

        if not item.get("id"):
            item["id"] = uuid.uuid4().hex
            changed = True

        if "enabled" not in item:
            item["enabled"] = True
            changed = True

        if not isinstance(item.get("config"), dict):
            item["config"] = {}
            changed = True

        if not item.get("name"):
            item["name"] = channel_name(item.get("channel"))
            changed = True

        cleaned.append(item)

    if changed or cleaned != items:
        cfg["notify_items"] = cleaned
        save_full_config(cfg)

    return cleaned


def save_notify_items(items):
    cfg = get_config()
    cfg["notify_items"] = items
    save_full_config(cfg)


def enabled_notify_items():
    return [x for x in notify_items() if x.get("enabled", True)]


def channel_name(channel):
    return NOTIFY_CHANNELS.get(channel, {}).get("name", channel)


def get_notify_item(item_id):
    for item in notify_items():
        if item.get("id") == item_id:
            return item
    return None


def default_notify_ids():
    cfg = get_config()
    ids = cfg.get("notify_default_ids")
    if not isinstance(ids, list):
        return []

    enabled = {x.get("id") for x in enabled_notify_items()}

    result = []
    for item_id in ids:
        if item_id in enabled and item_id not in result:
            result.append(item_id)

    return result


def save_default_notify_ids(ids):
    enabled = {x.get("id") for x in enabled_notify_items()}

    result = []
    for item_id in ids or []:
        if item_id in enabled and item_id not in result:
            result.append(item_id)

    cfg = get_config()
    cfg["notify_default_ids"] = result
    save_full_config(cfg)


def unique_notify_name(channel, name="", exclude_id=""):
    base = str(name or "").strip()
    if not base:
        base = channel_name(channel)

    exists = {
        str(x.get("name", ""))
        for x in notify_items()
        if x.get("id") != exclude_id
    }

    if base not in exists:
        return base

    idx = 1
    while True:
        candidate = f"{base}-{idx}"
        if candidate not in exists:
            return candidate
        idx += 1


def split_content(content, limit=2000):
    text = str(content or "")

    if len(text) <= limit:
        return [text]

    parts = []
    seps = ["\n", "。", "，", ",", "；", ";"]

    while len(text) > limit:
        start = min(1800, max(0, len(text) - 1))
        end = min(2100, len(text))
        best = -1
        best_distance = 999999

        for i in range(start, end):
            if i < len(text) and text[i] in seps:
                distance = abs(i - limit)
                if distance < best_distance:
                    best = i
                    best_distance = distance

        cut = limit if best < 0 else best + 1
        parts.append(text[:cut].strip())
        text = text[cut:].lstrip()

    if text:
        parts.append(text)

    return parts or [""]


def parse_headers(headers):
    parsed = {}

    for raw in str(headers or "").splitlines():
        line = raw.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        parsed[k.strip()] = v.strip()

    return parsed


def cfg_value(config, name, default=""):
    v = config.get(name, default)
    if v is None:
        v = ""
    return str(v).strip()


def send_one(item, title, content):
    if not item:
        return False, "通知不存在"

    if not item.get("enabled", True):
        return False, "通知已禁用"

    channel = item.get("channel")
    if channel not in NOTIFY_CHANNELS:
        return False, "未知通知渠道"

    c = item.get("config", {}) or {}
    title = str(title or "")
    content = str(content or "")

    def _cfg(name, default=""):
        return cfg_value(c, name, default)

    def _json(resp):
        try:
            return resp.json()
        except Exception:
            return {
                "status_code": getattr(resp, "status_code", ""),
                "text": getattr(resp, "text", "")[:500],
            }

    try:
        if channel == "bark":
            push = _cfg("BARK_PUSH")
            if not push:
                return False, "BARK_PUSH 为空"

            url = push if push.startswith("http") else "https://api.day.app/" + push
            data = {
                "title": title,
                "body": content,
            }

            if _cfg("BARK_GROUP"):
                data["group"] = _cfg("BARK_GROUP")
            if _cfg("BARK_SOUND"):
                data["sound"] = _cfg("BARK_SOUND")
            if _cfg("BARK_URL"):
                data["url"] = _cfg("BARK_URL")

            r = requests.post(url, json=data, timeout=15)
            js = _json(r)
            return js.get("code") == 200, str(js)

        if channel == "serverj":
            key = _cfg("PUSH_KEY")
            if not key:
                return False, "PUSH_KEY 为空"

            m = re.match(r"sctp(\d+)t", key)
            if m:
                url = f"https://{m.group(1)}.push.ft07.com/send/{key}.send"
            else:
                url = f"https://sctapi.ftqq.com/{key}.send"

            r = requests.post(
                url,
                data={"text": title, "desp": content.replace("\n", "\n\n")},
                timeout=15,
            )
            js = _json(r)
            return js.get("errno") == 0 or js.get("code") == 0, str(js)

        if channel == "pushplus":
            token = _cfg("PUSH_PLUS_TOKEN")
            if not token:
                return False, "PUSH_PLUS_TOKEN 为空"

            data = {
                "token": token,
                "title": title,
                "content": content,
                "topic": _cfg("PUSH_PLUS_USER"),
                "template": _cfg("PUSH_PLUS_TEMPLATE", "html") or "html",
            }

            r = requests.post("https://www.pushplus.plus/send", json=data, timeout=15)
            js = _json(r)
            return js.get("code") == 200, str(js)

        if channel == "telegram":
            bot = _cfg("TG_BOT_TOKEN")
            uid = _cfg("TG_USER_ID")
            if not bot or not uid:
                return False, "TG_BOT_TOKEN 或 TG_USER_ID 为空"

            api_host = _cfg("TG_API_HOST").rstrip("/") or "https://api.telegram.org"
            url = f"{api_host}/bot{bot}/sendMessage"

            text = f"{title}\n\n{content}" if title else content

            r = requests.post(
                url,
                data={
                    "chat_id": uid,
                    "text": text,
                    "disable_web_page_preview": "true",
                },
                timeout=20,
            )
            js = _json(r)
            return bool(js.get("ok")), str(js)

        if channel == "qywxbot":
            key = _cfg("QYWX_KEY")
            if not key:
                return False, "QYWX_KEY 为空"

            origin = _cfg("QYWX_ORIGIN").rstrip("/") or "https://qyapi.weixin.qq.com"
            url = f"{origin}/cgi-bin/webhook/send?key={key}"
            text = f"{title}\n\n{content}" if title else content

            r = requests.post(
                url,
                json={"msgtype": "text", "text": {"content": text}},
                timeout=15,
            )
            js = _json(r)
            return js.get("errcode") == 0, str(js)

        if channel == "dingding":
            token = _cfg("DD_BOT_TOKEN")
            secret = _cfg("DD_BOT_SECRET")

            if not token or not secret:
                return False, "DD_BOT_TOKEN 或 DD_BOT_SECRET 为空"

            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(
                secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            url = f"https://oapi.dingtalk.com/robot/send?access_token={token}&timestamp={timestamp}&sign={sign}"
            text = f"{title}\n\n{content}" if title else content

            r = requests.post(
                url,
                json={"msgtype": "text", "text": {"content": text}},
                timeout=15,
            )
            js = _json(r)
            return not js.get("errcode"), str(js)

        if channel == "feishu":
            key = _cfg("FSKEY")
            if not key:
                return False, "FSKEY 为空"

            url = f"https://open.feishu.cn/open-apis/bot/v2/hook/{key}"
            text = f"{title}\n\n{content}" if title else content
            data = {"msg_type": "text", "content": {"text": text}}

            secret = _cfg("FSSECRET")
            if secret:
                timestamp = str(int(time.time()))
                string_to_sign = f"{timestamp}\n{secret}"
                hmac_code = hmac.new(
                    string_to_sign.encode("utf-8"),
                    digestmod=hashlib.sha256,
                ).digest()
                data["timestamp"] = timestamp
                data["sign"] = base64.b64encode(hmac_code).decode("utf-8")

            r = requests.post(url, json=data, timeout=15)
            js = _json(r)
            return js.get("StatusCode") == 0 or js.get("code") == 0, str(js)

        if channel == "smtp":
            server = _cfg("SMTP_SERVER")
            email_addr = _cfg("SMTP_EMAIL")
            password = _cfg("SMTP_PASSWORD")
            name = _cfg("SMTP_NAME") or email_addr
            to_addr = _cfg("SMTP_TO") or email_addr

            if not server or not email_addr or not password:
                return False, "SMTP 配置不完整"

            msg = MIMEText(content, "plain", "utf-8")
            msg["From"] = formataddr((Header(name, "utf-8").encode(), email_addr))
            msg["To"] = to_addr
            msg["Subject"] = Header(title, "utf-8")

            if ":" in server:
                host, port = server.rsplit(":", 1)
                port = int(port)
            else:
                host = server
                port = 465 if _cfg("SMTP_SSL", "false").lower() == "true" else 25

            if _cfg("SMTP_SSL", "false").lower() == "true":
                smtp_obj = smtplib.SMTP_SSL(host, port, timeout=20)
            else:
                smtp_obj = smtplib.SMTP(host, port, timeout=20)

            smtp_obj.login(email_addr, password)
            smtp_obj.sendmail(email_addr, [to_addr], msg.as_bytes())
            smtp_obj.close()

            return True, "ok"

        if channel == "ntfy":
            url = _cfg("NTFY_URL").rstrip("/")
            topic = _cfg("NTFY_TOPIC")

            if not url or not topic:
                return False, "NTFY_URL 或 NTFY_TOPIC 为空"

            headers = {
                "Title": title,
                "Priority": _cfg("NTFY_PRIORITY", "3") or "3",
            }

            if _cfg("NTFY_TOKEN"):
                headers["Authorization"] = "Bearer " + _cfg("NTFY_TOKEN")

            r = requests.post(
                f"{url}/{topic}",
                data=content.encode("utf-8"),
                headers=headers,
                timeout=15,
            )

            return r.status_code in (200, 201, 202), r.text[:500]

        if channel == "wxpusher":
            app_token = _cfg("WXPUSHER_APP_TOKEN")
            if not app_token:
                return False, "WXPUSHER_APP_TOKEN 为空"

            topic_ids = []
            if _cfg("WXPUSHER_TOPIC_IDS"):
                topic_ids = [
                    int(x.strip())
                    for x in _cfg("WXPUSHER_TOPIC_IDS").split(";")
                    if x.strip()
                ]

            uids = []
            if _cfg("WXPUSHER_UIDS"):
                uids = [
                    x.strip()
                    for x in _cfg("WXPUSHER_UIDS").split(";")
                    if x.strip()
                ]

            if not topic_ids and not uids:
                return False, "WXPUSHER_TOPIC_IDS 和 WXPUSHER_UIDS 至少填写一个"

            data = {
                "appToken": app_token,
                "content": f"<h2>{html.escape(title)}</h2><pre style='white-space:pre-wrap'>{html.escape(content)}</pre>",
                "summary": title[:96],
                "contentType": 2,
                "topicIds": topic_ids,
                "uids": uids,
                "verifyPayType": 0,
            }

            r = requests.post(
                "https://wxpusher.zjiecode.com/api/send/message",
                json=data,
                timeout=15,
            )
            js = _json(r)
            return js.get("code") == 1000, str(js)

        if channel == "gotify":
            url = _cfg("GOTIFY_URL").rstrip("/")
            token = _cfg("GOTIFY_TOKEN")

            if not url or not token:
                return False, "GOTIFY_URL 或 GOTIFY_TOKEN 为空"

            r = requests.post(
                f"{url}/message",
                params={"token": token},
                data={
                    "title": title,
                    "message": content,
                    "priority": _cfg("GOTIFY_PRIORITY", "0") or "0",
                },
                timeout=15,
            )
            js = _json(r)
            return bool(js.get("id")), str(js)

        if channel == "pushdeer":
            key = _cfg("DEER_KEY")
            if not key:
                return False, "DEER_KEY 为空"

            url = _cfg("DEER_URL") or "https://api2.pushdeer.com/message/push"
            r = requests.post(
                url,
                data={
                    "pushkey": key,
                    "text": title,
                    "desp": content,
                    "type": "markdown",
                },
                timeout=15,
            )
            js = _json(r)
            ok = False
            try:
                ok = len(js.get("content", {}).get("result", [])) > 0
            except Exception:
                ok = js.get("code") == 0
            return ok, str(js)

        if channel == "webhook":
            url = _cfg("WEBHOOK_URL")
            method = (_cfg("WEBHOOK_METHOD") or "POST").upper()
            content_type = _cfg("WEBHOOK_CONTENT_TYPE") or "application/json"

            if not url:
                return False, "WEBHOOK_URL 为空"

            url = url.replace("$title", urllib.parse.quote_plus(title))
            url = url.replace("$content", urllib.parse.quote_plus(content))

            headers = parse_headers(c.get("WEBHOOK_HEADERS", ""))
            if content_type:
                headers.setdefault("Content-Type", content_type)

            raw_body = str(c.get("WEBHOOK_BODY", "") or "")
            raw_body = raw_body.replace("$title", title).replace("$content", content)

            data = None
            if raw_body:
                data = raw_body.encode("utf-8")
            elif method == "POST":
                if content_type == "application/json":
                    data = json.dumps(
                        {"title": title, "content": content},
                        ensure_ascii=False
                    ).encode("utf-8")
                else:
                    data = f"title={urllib.parse.quote_plus(title)}&content={urllib.parse.quote_plus(content)}".encode("utf-8")

            r = requests.request(
                method=method,
                url=url,
                headers=headers,
                data=data,
                timeout=15,
            )

            return 200 <= r.status_code < 300, f"{r.status_code} {r.text[:500]}"

        return False, "未知渠道"

    except Exception as e:
        return False, str(e)


def send_by_ids(title, content, ids=None):
    if isinstance(ids, str):
        ids = [ids]

    ids = ids or []

    if "__none__" in ids:
        return []

    enabled_ids = {x.get("id") for x in enabled_notify_items()}

    real_ids = []
    for item_id in ids:
        if item_id in enabled_ids and item_id not in real_ids:
            real_ids.append(item_id)

    if not real_ids and "__default__" in ids:
        for item_id in default_notify_ids():
            if item_id in enabled_ids and item_id not in real_ids:
                real_ids.append(item_id)

    if not real_ids:
        return []

    chunks = split_content(content, 2000)
    results = []

    for idx, chunk in enumerate(chunks, 1):
        if len(chunks) > 1:
            part_title = f"{title} [{idx}/{len(chunks)}]"
        else:
            part_title = title

        for item_id in real_ids:
            item = get_notify_item(item_id)

            if not item:
                results.append({
                    "id": item_id,
                    "name": item_id,
                    "ok": False,
                    "msg": "通知不存在",
                })
                continue

            ok, msg = send_one(item, part_title, chunk)

            results.append({
                "id": item_id,
                "name": item.get("name", item_id),
                "ok": ok,
                "msg": msg,
            })

            print(f"[Notify] {item.get('name')} {'发送成功' if ok else '发送失败'}：{msg}")

    return results


def task_notify_ids(task):
    notify = task.get("notify")

    if isinstance(notify, dict):
        mode = notify.get("mode", "none")
        ids = notify.get("ids", [])

        if mode == "none":
            return ["__none__"]
        if mode == "default":
            return ["__default__"]
        if mode == "custom":
            return [x for x in ids if x]

    old_ids = task.get("notify_ids")
    if isinstance(old_ids, list) and old_ids:
        return old_ids

    return ["__none__"]


def extract_user_log_content(log_text):
    text = str(log_text or "")
    lines = text.splitlines()

    if not lines:
        return ""

    start_idx = 0
    if lines and lines[0].startswith("===== 启动任务:"):
        for idx, line in enumerate(lines):
            if line.strip().startswith("====") and idx > 0:
                start_idx = idx + 1
                break

    lines = lines[start_idx:]

    end_idx = len(lines)
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("===== 任务已结束:"):
            end_idx = idx
            break
        if stripped.startswith("===== 通知结果"):
            end_idx = idx
            break

    lines = lines[:end_idx]

    while lines and not lines[0].strip():
        lines.pop(0)

    while lines and not lines[-1].strip():
        lines.pop()

    return "\n".join(lines)


def notify_select_options(selected=None):
    selected = selected or []
    if isinstance(selected, str):
        selected = [selected]

    enabled = enabled_notify_items()

    options = ""
    for item in enabled:
        s = "selected" if item.get("id") in selected else ""
        options += f'<option value="{item.get("id")}" {s}>{html.escape(item.get("name", ""))} [{html.escape(channel_name(item.get("channel")))}]</option>'

    if not options:
        options = '<option value="" disabled>暂无已启用通知</option>'

    return options

# ============================================================
# 正式辅助函数：发送给所有已启用通知 / 系统失败通知
# ============================================================
def send_all_enabled(title, content):
    ids = [x.get("id") for x in enabled_notify_items() if x.get("id")]
    return send_by_ids(title, content, ids)


def send_system_failure(action, message, extra=""):
    try:
        lines = [
            f"时间：{now_str()}",
            f"功能：{action}",
            "结果：失败",
            f"原因：{message}",
        ]

        if extra:
            lines.append("")
            lines.append(str(extra))

        return send_all_enabled(
            f"FLS 系统功能失败：{action}",
            "\n".join(lines)
        )
    except Exception as e:
        print(f"[Notify] 系统失败通知发送失败: {e}")
        return []
