#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WisArt (智画创) daily check-in for FLS."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import requests


SITE_NAME = "智画创"
DEFAULT_URL = "https://wisart.kuaileshifu.com"
COOKIE_ENV = "WISART_COOKIE"
URL_ENV_NAMES = ("WISART_URL", "WISART_BASE_URL")
COOKIE_HOST = "wisart.kuaileshifu.com"
USER_AGENT = "fls-checkin/1.0 (+https://github.com/liyw0205/fls)"


class CheckinError(RuntimeError):
    """An expected, user-actionable check-in failure."""


def first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def normalize_base_url(value: str) -> str:
    raw = (value or DEFAULT_URL).strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise CheckinError("URL 必须是完整的 http(s) 地址")
    return f"{parsed.scheme}://{parsed.netloc}"


def normalize_cookie(value: str) -> str:
    cookie = (value or "").strip()
    if cookie.lower().startswith("cookie:"):
        cookie = cookie.split(":", 1)[1].strip()
    if "=" not in cookie or not cookie.split("=", 1)[0].strip():
        raise CheckinError("Cookie 格式不正确，应为 name=value")
    return cookie


def cookie_from_file(path: Path, host: str) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CheckinError(f"无法读取凭据文件 {path}: {exc}") from exc

    host = host.lower()
    for index, line in enumerate(lines):
        if host not in line.lower():
            continue
        for candidate in lines[index + 1 : index + 9]:
            candidate = candidate.strip()
            if not candidate or candidate.lower() in {"cookie", "cookies"}:
                continue
            if candidate.lower().startswith("cookie:"):
                candidate = candidate.split(":", 1)[1].strip()
            if "=" in candidate:
                return normalize_cookie(candidate)
        break
    raise CheckinError(f"凭据文件中没有找到 {host} 的 Cookie")


def resolve_cookie(explicit: str | None, cookie_file: str | None) -> str:
    if explicit and explicit.strip():
        return normalize_cookie(explicit)

    env_cookie = os.environ.get(COOKIE_ENV, "").strip()
    if env_cookie:
        return normalize_cookie(env_cookie)

    env_file = os.environ.get("SIGNIN_COOKIE_FILE", "").strip()
    explicit_file = cookie_file or env_file
    if explicit_file:
        path = Path(explicit_file).expanduser()
        if not path.is_file():
            raise CheckinError(f"凭据文件不存在：{path}")
        return cookie_from_file(path.resolve(), COOKIE_HOST)

    raise CheckinError(
        f"未找到 Cookie。请传入 --cookie，或设置 {COOKIE_ENV} / --cookie-file"
    )


def response_json(response: requests.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        snippet = response.text.strip().replace("\n", " ")[:200]
        raise CheckinError(
            f"接口返回非 JSON（HTTP {response.status_code}）：{snippet or '空响应'}"
        ) from exc

    if not isinstance(payload, dict):
        raise CheckinError("接口返回格式不正确")
    if not response.ok:
        detail = payload.get("message") or payload.get("error") or payload.get("detail")
        raise CheckinError(f"接口请求失败（HTTP {response.status_code}）：{detail or payload}")
    return payload


def request_json(
    session: requests.Session,
    method: str,
    url: str,
    timeout: float,
) -> dict:
    try:
        response = session.request(method, url, timeout=timeout)
    except requests.RequestException as exc:
        raise CheckinError(f"网络请求失败：{exc}") from exc
    return response_json(response)


def result(status: str, message: str, **fields: object) -> dict:
    payload = {"site": SITE_NAME, "status": status, "message": message}
    payload.update(fields)
    return payload


def run(args: argparse.Namespace) -> dict:
    base_url = normalize_base_url(args.base_url or first_env(*URL_ENV_NAMES) or DEFAULT_URL)
    cookie = resolve_cookie(args.cookie, args.cookie_file)

    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Origin": base_url,
            "Referer": base_url + "/",
            "Cookie": cookie,
        }
    )

    state = request_json(
        session,
        "GET",
        base_url + "/api/points/checkin",
        args.timeout,
    )
    daily = state.get("daily_checkin")
    if not isinstance(daily, dict):
        raise CheckinError("接口没有返回每日签到状态")

    points = state.get("points")
    if daily.get("signed_today") is True:
        return result("already_signed", "今日已签到", points=points)
    if daily.get("enabled") is False:
        raise CheckinError("站点当前未开启每日签到")
    if daily.get("eligible") is False:
        raise CheckinError("当前账号暂不满足签到条件")
    if args.dry_run:
        return result("pending", "状态正常，未执行签到", points=points)

    payload = request_json(
        session,
        "POST",
        base_url + "/api/points/checkin",
        args.timeout,
    )
    if payload.get("ok") is False:
        detail = payload.get("message") or payload.get("error") or payload
        raise CheckinError(f"签到失败：{detail}")

    post_daily = payload.get("daily_checkin")
    if isinstance(post_daily, dict) and post_daily.get("signed_today") is False:
        raise CheckinError("接口未确认签到成功")
    added = payload.get("added", 0)
    balance = payload.get("balance", points)
    return result("success", f"签到成功，获得 {added} 积分", added=added, points=balance)


def emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return
    print(f"{payload['site']}：{payload['message']}")
    details = []
    if "added" in payload:
        details.append(f"本次 +{payload['added']}")
    if payload.get("points") is not None:
        details.append(f"当前积分 {payload['points']}")
    if details:
        print("，".join(details))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="智画创每日签到（适用于 FLS）")
    parser.add_argument("--cookie", help=f"Cookie；未提供时读取 {COOKIE_ENV}")
    parser.add_argument("--cookie-file", help="包含站点 Cookie 的凭据文件")
    parser.add_argument("--url", "--base-url", dest="base_url", help="站点地址")
    parser.add_argument("--timeout", type=float, default=30.0, help="请求超时秒数，默认 30")
    parser.add_argument("--dry-run", action="store_true", help="只查询状态，不执行签到")
    parser.add_argument("--json", action="store_true", help="以单行 JSON 输出结果")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    return args


def main() -> int:
    args = parse_args()
    try:
        payload = run(args)
    except CheckinError as exc:
        payload = result("error", str(exc))
        emit(payload, args.json)
        return 1
    except KeyboardInterrupt:
        payload = result("error", "已取消")
        emit(payload, args.json)
        return 130

    emit(payload, args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
