#!/usr/bin/env python3
"""Run the real Chromium responsive regression matrix.

The repository intentionally has no npm dependency.  On the development
machine this script uses the Playwright Core installation provided by the
Codex/Termux environment; ``FLS_PLAYWRIGHT_CORE`` and ``FLS_CHROMIUM`` can be
used to point at equivalent local installations.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "browser-regression-token"
PORT = "5711"


def seed_browser_fixture(base_dir):
    """Create deterministic, disposable records for the action audit."""
    base_dir = Path(base_dir)
    data_dir = base_dir / "data"
    log_dir = base_dir / "log"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    (data_dir / "tasks.json").write_text(
        json.dumps(
            [
                {
                    "id": "browser-task",
                    "name": "浏览器回归任务",
                    "command": "echo browser fixture " + "x" * 120,
                    "enabled": True,
                    "collection_id": "browser-collection",
                    "config_path": "browser-config.yml",
                    "pinned": False,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data_dir / "collections.json").write_text(
        json.dumps(
            [{"id": "browser-collection", "name": "浏览器回归合集", "remark": "fixture"}],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data_dir / "proxies.json").write_text(
        json.dumps(
            [
                {
                    "id": "browser-proxy",
                    "name": "浏览器回归代理",
                    "type": "http",
                    "host": "127.0.0.1",
                    "port": "9",
                    "enabled": True,
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data_dir / "global_env.json").write_text(
        json.dumps({"BROWSER_FIXTURE": "fixture"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (data_dir / "config.json").write_text(
        json.dumps(
            {
                "admin_token": TOKEN,
                "notify_items": [
                    {
                        "id": "browser-notify",
                        "name": "浏览器回归通知",
                        "channel": "bark",
                        "enabled": True,
                        "config": {"BARK_PUSH": "fixture"},
                    }
                ],
                "notify_default_ids": ["browser-notify"],
                "online_script_source": "https://fixture.invalid/index.json",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (data_dir / "online_scripts_cache.json").write_text(
        json.dumps(
            [
                {
                    "id": "browser-script",
                    "name": "浏览器回归脚本",
                    "type": "raw",
                    "link": "https://fixture.invalid/browser-script.py",
                    "task_cron": [
                        {"name": "回归导入任务", "command": "echo fixture", "cron": ""}
                    ],
                },
                {
                    "id": "browser-install",
                    "name": "浏览器回归安装脚本",
                    "type": "raw",
                    "link": "https://fixture.invalid/browser-install.sh",
                    "task_cron": [],
                },
                {
                    "id": "browser-doc",
                    "name": "浏览器回归文档脚本",
                    "type": "raw",
                    "link": "https://fixture.invalid/browser-doc.py",
                    "doc_link": "about:blank",
                    "task_cron": [],
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    script_dir = base_dir / "scripts"
    script_dir.mkdir(parents=True, exist_ok=True)
    (script_dir / "browser.py").write_text("print('browser fixture')\n", encoding="utf-8")
    (script_dir / "browser-config.yml").write_text("enabled: true\n", encoding="utf-8")
    (log_dir / "browser-task-fixture.log").write_text(
        "===== 启动任务: 浏览器回归任务 =====\nbrowser fixture\n",
        encoding="utf-8",
    )


def find_playwright_core():
    candidates = []
    if os.environ.get("FLS_PLAYWRIGHT_CORE"):
        candidates.append(Path(os.environ["FLS_PLAYWRIGHT_CORE"]))
    candidates.append(
        Path.home() / ".local/share/playwright-mcp/node_modules/playwright-core"
    )
    for candidate in candidates:
        if (candidate / "package.json").exists():
            return candidate
    return None


def find_chromium():
    explicit = os.environ.get("FLS_CHROMIUM")
    if explicit:
        return explicit
    return shutil.which("chromium-browser") or shutil.which("chromium")


def wait_for_server(url, process, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"FLS server exited with code {process.returncode}")
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise RuntimeError(f"FLS server did not start at {url}")


def node_script():
    return r'''
Object.defineProperty(process, "platform", {value: "linux"});
const {chromium} = require(process.env.FLS_PLAYWRIGHT_CORE);

const pages = [
  ["/", "page-dashboard"],
  ["/tasks", "page-tasks"],
  ["/collections", "page-collections"],
  ["/history", "page-history"],
  ["/task/new", "page-tasks"],
  ["/task/edit/browser-task", "page-tasks"],
  ["/task/config/browser-task", "page-tasks"],
  ["/collection/new", "page-collections"],
  ["/collection/edit/browser-collection", "page-collections"],
  ["/env", "page-env"],
  ["/env/view", "page-env"],
  ["/env/new", "page-env"],
  ["/env/edit/BROWSER_FIXTURE", "page-env"],
  ["/env/import", "page-env"],
  ["/proxy", "page-proxy"],
  ["/proxy/new", "page-proxy"],
  ["/proxy/edit/browser-proxy", "page-proxy"],
  ["/pull", "page-pull"],
  ["/pull/new", "page-pull"],
  ["/pull/fetch", "page-pull"],
  ["/pull/import", "page-pull"],
  ["/scripts/view?path=browser.py", "page-pull"],
  ["/scripts/rename?path=browser.py", "page-pull"],
  ["/online-scripts", "page-online_scripts"],
  ["/online-scripts/source", "page-online_scripts"],
  ["/online-scripts/doc/browser-doc?mode=web", "page-online_scripts"],
  ["/online-scripts/install-select/browser-script", "page-online_scripts"],
  ["/backup", "page-backup"],
  ["/deps", "page-deps"],
  ["/logs", "page-logs"],
  ["/log/browser-task", "page-logs"],
  ["/logfile/browser-task-fixture.log?back=/history", "page-logs"],
  ["/notify", "page-notify"],
  ["/notify/edit/browser-notify", "page-notify"],
  ["/panel/status", "page-status"],
  ["/config", "page-config"],
  ["/about", "page-about"]
];
const viewports = [
  [390, 844], [430, 932], [768, 1024],
  [1024, 768], [1280, 800], [1440, 900]
];
const focusedFormPaths = new Set([
  "/task/new", "/task/edit/browser-task", "/task/config/browser-task",
  "/collection/new", "/collection/edit/browser-collection",
  "/env/view", "/env/new", "/env/edit/BROWSER_FIXTURE", "/env/import",
  "/proxy/new", "/proxy/edit/browser-proxy",
  "/pull/new", "/pull/fetch", "/pull/import",
  "/scripts/view?path=browser.py", "/scripts/rename?path=browser.py",
  "/online-scripts/source", "/online-scripts/install-select/browser-script",
  "/online-scripts/doc/browser-doc?mode=web"
]);

function issue(result, kind, detail) {
  result.issues.push({kind, detail});
}

async function mockApi(page, responses, callback) {
  const calls = [];
  const handler = async route => {
    const request = route.request();
    const url = new URL(request.url());

    if (!url.pathname.startsWith("/api/")) {
      await route.continue();
      return;
    }

    const match = responses.find(item => url.pathname.startsWith(item.prefix));
    const responseStatus = match && match.status ? match.status : 200;
    calls.push({
      path: url.pathname,
      method: request.method(),
      headers: request.headers(),
      status: responseStatus
    });

    const payload = match ? match.payload : {ok: false, msg: "未配置 fixture 响应"};
    await route.fulfill({
      status: responseStatus,
      contentType: "application/json",
      body: JSON.stringify(payload)
    });
  };

  await page.route("**/*", handler);
  let state = null;
  let error = null;
  try {
    state = await callback();
  } catch (caught) {
    error = String(caught);
  }
  await page.unroute("**/*", handler);
  return {calls, state, error};
}

async function mockHttp(page, responses, callback) {
  const calls = [];
  const handler = async route => {
    const request = route.request();
    const url = new URL(request.url());
    const match = responses.find(item => {
      if (!url.pathname.startsWith(item.prefix)) return false;
      return !item.method || request.method() === item.method;
    });

    if (!match) {
      await route.continue();
      return;
    }

    calls.push({
      path: url.pathname,
      method: request.method(),
      headers: request.headers(),
      postData: request.postData() || "",
      status: match.status || 200
    });
    await route.fulfill({
      status: match.status || 200,
      contentType: match.contentType || "text/html",
      body: match.body || "<!doctype html><html><head><title>fixture</title></head><body class='page-fixture'><div class='title'>fixture</div><nav class='nav'></nav><div class='content'></div></body></html>"
    });
  };

  await page.route("**/*", handler);
  let state = null;
  let error = null;
  try {
    state = await callback();
  } catch (caught) {
    error = String(caught);
  }
  await page.unroute("**/*", handler);
  return {calls, state, error};
}

function auditPostCalls(result, label, audit) {
  if (!result || result.error) {
    issue(audit, "action-error", label + ": " + (result && result.error || "unknown"));
    return;
  }
  if (!result.calls.length) {
    issue(audit, "action-request", label + ": no request");
    return;
  }
  result.calls.forEach(call => {
    if (call.method !== "POST") issue(audit, "action-method", label + ": " + call.method);
    if (call.status < 200 || call.status >= 300) issue(audit, "action-status", label + ": " + call.status);
    if (!call.headers["x-csrf-token"]) issue(audit, "action-csrf", label + ": missing X-CSRF-Token");
  });
}

function auditFeedback(result, label, audit) {
  const feedback = result && result.state && result.state.feedback || "";
  if (!feedback.includes("下一步")) issue(audit, "action-feedback", label + ": " + feedback);
}

function auditFormCalls(result, label, audit, expectedMethod="POST") {
  if (!result || result.error) {
    issue(audit, "form-action-error", label + ": " + (result && result.error || "unknown"));
    return;
  }
  if (!result.calls.length) {
    if (!(result.state && result.state.missing)) issue(audit, "form-action-request", label + ": no request");
    return;
  }
  result.calls.forEach(call => {
    if (call.method !== expectedMethod) issue(audit, "form-action-method", label + ": " + call.method);
    if (call.status < 200 || call.status >= 300) issue(audit, "form-action-status", label + ": " + call.status);
    if (expectedMethod === "POST" && !call.headers["x-csrf-token"] && !/(^|&)csrf_token=/.test(call.postData)) {
      issue(audit, "form-action-csrf", label + ": missing CSRF token");
    }
  });
}

async function submitForm(page, prefix, selector, label, audit, method="POST", submitterSelector="") {
  const result = await mockHttp(page, [
    {prefix, method}
  ], async () => {
    const state = await page.evaluate(({selector, submitterSelector}) => {
      const form = document.querySelector(selector);
      if (!form) return {missing: true};
      const submitter = submitterSelector ? form.querySelector(submitterSelector) : null;
      if (submitter) submitter.click();
      else form.requestSubmit();
      return {missing: false};
    }, {selector, submitterSelector});
    await page.waitForTimeout(120);
    return state;
  });
  auditFormCalls(result, label, audit, method);
  return result;
}

function filterCalls(result, prefix) {
  return {
    calls: (result && result.calls || []).filter(call => call.path.startsWith(prefix)),
    state: result && result.state,
    error: result && result.error
  };
}

async function runActionAudit(page, audit) {
  const staticPaths = [
    "/tasks", "/collections", "/proxy", "/notify", "/pull",
    "/online-scripts", "/backup", "/deps", "/config", "/logs",
    "/panel/status", "/about"
  ];

  for (const path of staticPaths) {
    await page.goto(process.env.FLS_BASE_URL + path, {
      waitUntil: "domcontentloaded", timeout: 8000
    });
    const forms = await page.evaluate(() => Array.from(document.forms).map(form => ({
      method: (form.getAttribute("method") || "GET").toUpperCase(),
      action: form.getAttribute("action") || location.pathname,
      csrf: !!form.querySelector('input[name="csrf_token"]')
    })));
    forms.forEach(form => {
      if (form.method === "POST" && !form.csrf) {
        issue(audit, "form-csrf", path + " " + form.action);
      }
    });
  }

  // Exercise the remaining form workflows through intercepted requests. The
  // fixture keeps these actions side-effect free while preserving the browser
  // submission, redirect, and CSRF boundaries.
  await page.goto(process.env.FLS_BASE_URL + "/notify", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  await submitForm(
    page, "/notify/test/", 'form[action^="/notify/test/"]',
    "notification test", audit
  );

  await page.goto(process.env.FLS_BASE_URL + "/online-scripts", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  await submitForm(
    page, "/online-scripts/refresh", 'form[action="/online-scripts/refresh"]',
    "online script sync", audit
  );
  await submitForm(
    page, "/online-scripts/install/", 'form[action^="/online-scripts/install/"]',
    "online script install", audit
  );

  await page.goto(process.env.FLS_BASE_URL + "/online-scripts/install-select/browser-script", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  await submitForm(
    page, "/online-scripts/import-tasks/", "#onlineInstallSelectForm",
    "online script import", audit, "POST", 'button[formaction*="/online-scripts/import-tasks/"]'
  );

  await page.goto(process.env.FLS_BASE_URL + "/deps", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  await submitForm(
    page, "/deps/refresh", 'form[action="/deps/refresh"]',
    "dependency refresh", audit
  );

  await page.goto(process.env.FLS_BASE_URL + "/config", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  await submitForm(page, "/config", 'form[method="post"]', "configuration save", audit);

  await page.goto(process.env.FLS_BASE_URL + "/logs", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  await page.evaluate(() => {
    const input = document.querySelector('#logs-filters input[name="q"]');
    if (input) input.value = "browser";
  });
  await submitForm(page, "/logs", '#logs-filters form', "log filtering", audit, "GET");

  await page.goto(process.env.FLS_BASE_URL + "/panel/status", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  const statusForm = await page.locator('form[action^="/install/runtime/"]').count();
  if (statusForm) {
    const statusAction = await page.locator('form[action^="/install/runtime/"]').first().getAttribute("action");
    await submitForm(page, statusAction, 'form[action^="/install/runtime/"]', "runtime install", audit);
  } else {
    // All runtimes may be installed on the host; still verify the action
    // contract with a disposable form carrying the layout's CSRF token.
    const statusAction = "/install/runtime/fixture";
    await page.evaluate(action => {
      const form = document.createElement("form");
      form.method = "post";
      form.action = action;
      const csrf = document.createElement("input");
      csrf.type = "hidden";
      csrf.name = "csrf_token";
      csrf.value = document.querySelector('meta[name="csrf-token"]')?.content || "fixture";
      form.appendChild(csrf);
      document.body.appendChild(form);
    }, statusAction);
    await submitForm(page, statusAction, 'form[action="/install/runtime/fixture"]', "runtime install", audit);
  }

  await page.goto(process.env.FLS_BASE_URL + "/proxy", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  await submitForm(
    page, "/proxy/toggle/", 'form[action^="/proxy/toggle/"]',
    "proxy enable toggle", audit
  );
  await page.goto(process.env.FLS_BASE_URL + "/proxy", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  let proxy = await mockApi(page, [
    {prefix: "/api/proxy/test/", payload: {ok: false, error: "fixture proxy failure"}},
    {prefix: "/api/proxy/quality/", payload: {ok: false, error: "fixture quality failure"}}
  ], async () => page.evaluate(async () => {
    const button = document.createElement("button");
    button.textContent = "测试代理连接";
    document.body.appendChild(button);
    await flsProxyTest("browser-proxy", button);
    return {
      disabled: button.disabled,
      busy: button.getAttribute("aria-busy"),
      feedback: document.querySelector("#proxyResultText")?.textContent || ""
    };
  }));
  auditPostCalls(proxy, "proxy test", audit);
  auditFeedback(proxy, "proxy test", audit);
  if (proxy.state && (proxy.state.disabled || proxy.state.busy)) issue(audit, "button-state", "proxy test not restored");

  proxy = await mockApi(page, [
    {prefix: "/api/proxy/quality/", payload: {ok: false, error: "fixture quality failure"}}
  ], async () => page.evaluate(async () => {
    const button = document.createElement("button");
    button.textContent = "检测代理质量";
    document.body.appendChild(button);
    await flsProxyQuality("browser-proxy", button);
    return {
      disabled: button.disabled,
      busy: button.getAttribute("aria-busy"),
      feedback: document.querySelector("#proxyResultText")?.textContent || ""
    };
  }));
  auditPostCalls(proxy, "proxy quality", audit);
  auditFeedback(proxy, "proxy quality", audit);
  if (proxy.state && (proxy.state.disabled || proxy.state.busy)) issue(audit, "button-state", "proxy quality not restored");

  await page.goto(process.env.FLS_BASE_URL + "/tasks", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  const task = await mockApi(page, [
    {prefix: "/api/task/action/", payload: {ok: false, msg: "fixture task failure"}},
    {prefix: "/api/task/bulk-action", payload: {ok: false, msg: "fixture bulk failure"}}
  ], async () => page.evaluate(async () => {
    let checkbox = document.querySelector("#tasksBlock .task-select-checkbox");
    if(!checkbox){
      const block = document.createElement("div");
      block.id = "tasksBlock";
      checkbox = document.createElement("input");
      checkbox.className = "task-select-checkbox";
      block.appendChild(checkbox);
      document.body.appendChild(block);
    }
    checkbox.checked = true;
    checkbox.setAttribute("data-task-id", "browser-task");
    const one = document.createElement("button");
    one.textContent = "立即运行任务";
    const bulk = document.createElement("button");
    const stop = document.createElement("button");
    bulk.textContent = "批量运行任务";
    stop.textContent = "停止任务";
    document.body.append(one, bulk, stop);
    await taskAjaxAction("run", "browser-task", one);
    await taskAjaxAction("stop", "browser-task", stop);
    await taskBulkAction("run", bulk);
    return {
      oneDisabled: one.disabled,
      stopDisabled: stop.disabled,
      bulkDisabled: bulk.disabled,
      oneBusy: one.getAttribute("aria-busy"),
      stopBusy: stop.getAttribute("aria-busy"),
      bulkBusy: bulk.getAttribute("aria-busy"),
      feedback: Array.from(document.querySelectorAll("#flsToastBox .fls-toast")).map(item => item.textContent).join(" | ")
    };
  }));
  auditPostCalls(task, "task run stop and bulk", audit);
  auditFeedback(task, "task run stop and bulk", audit);
  if (task.state && (task.state.oneDisabled || task.state.stopDisabled || task.state.bulkDisabled || task.state.oneBusy || task.state.stopBusy || task.state.bulkBusy)) {
    issue(audit, "button-state", "task action not restored");
  }

  await page.goto(process.env.FLS_BASE_URL + "/collections", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  const collection = await mockApi(page, [
    {prefix: "/api/task/bulk-action", payload: {ok: false, msg: "fixture collection failure"}}
  ], async () => page.evaluate(async () => {
    const card = document.createElement("article");
    card.className = "collection-task-card";
    card.dataset.collectionId = "browser-collection";
    card.dataset.taskId = "browser-task";
    const checkbox = document.createElement("input");
    checkbox.className = "collection-task-select";
    checkbox.type = "checkbox";
    checkbox.dataset.collectionId = "browser-collection";
    checkbox.dataset.taskId = "browser-task";
    checkbox.checked = true;
    document.body.append(card, checkbox);
    const button = document.createElement("button");
    button.textContent = "批量运行任务";
    document.body.appendChild(button);
    await flsCollectionTaskBulkAction("browser-collection", "run", button);
    return {
      disabled: button.disabled,
      busy: button.getAttribute("aria-busy"),
      feedback: Array.from(document.querySelectorAll("#flsToastBox .fls-toast")).map(item => item.textContent).join(" | ")
    };
  }));
  auditPostCalls(collection, "collection bulk", audit);
  auditFeedback(collection, "collection bulk", audit);
  if (collection.state && (collection.state.disabled || collection.state.busy)) issue(audit, "button-state", "collection bulk not restored");

  await page.goto(process.env.FLS_BASE_URL + "/backup", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  const backup = await mockApi(page, [
    {prefix: "/api/backup/create", payload: {ok: false, msg: "fixture backup failure"}},
    {prefix: "/api/backup/list", payload: {ok: true, items: []}}
  ], async () => page.evaluate(async () => {
    const create = document.querySelector("#backupCreateForm button");
    await flsCreateBackup(create);
    const refresh = document.querySelector("#backup-list button");
    await flsRefreshBackupList(refresh);
    return {
      createDisabled: create.disabled,
      refreshDisabled: refresh.disabled,
      feedback: document.querySelector("#backupJobText")?.textContent || ""
    };
  }));
  auditPostCalls(filterCalls(backup, "/api/backup/create"), "backup create", audit);
  const backupListCalls = filterCalls(backup, "/api/backup/list").calls;
  if (!backupListCalls.length || backupListCalls.some(call => call.method !== "GET")) {
    issue(audit, "action-method", "backup refresh must use GET");
  }
  if (backup.state && (backup.state.createDisabled || backup.state.refreshDisabled)) issue(audit, "button-state", "backup button not restored");
  auditFeedback(backup, "backup create", audit);

  await submitForm(
    page, "/backup/import", 'form[action="/backup/import"]',
    "backup restore", audit
  );

  await page.goto(process.env.FLS_BASE_URL + "/about", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  await submitForm(
    page, "/about/restart-panel", 'form[action="/about/restart-panel"]',
    "panel restart", audit
  );
  await page.goto(process.env.FLS_BASE_URL + "/about", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  await submitForm(
    page, "/about/stop-panel", 'form[action="/about/stop-panel"]',
    "panel stop", audit
  );

  await page.goto(process.env.FLS_BASE_URL + "/logs", {
    waitUntil: "domcontentloaded", timeout: 8000
  });
  const logs = await mockApi(page, [
    {prefix: "/api/logs/groups/delete", payload: {ok: false, msg: "fixture log failure"}}
  ], async () => page.evaluate(async () => {
    const button = document.createElement("button");
    button.textContent = "删除日志分组";
    document.body.appendChild(button);
    await flsLogsDeleteGroups(["browser-task"], button);
    return {
      disabled: button.disabled,
      busy: button.getAttribute("aria-busy"),
      feedback: Array.from(document.querySelectorAll("#flsToastBox .fls-toast")).map(item => item.textContent).join(" | ")
    };
  }));
  auditPostCalls(logs, "log group delete", audit);
  auditFeedback(logs, "log group delete", audit);
  if (logs.state && (logs.state.disabled || logs.state.busy)) issue(audit, "button-state", "log delete not restored");
}

(async()=>{
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.FLS_CHROMIUM,
    args: ["--no-sandbox", "--disable-dev-shm-usage"]
  });
  const results = [];
  for (const [width, height] of viewports) {
    const context = await browser.newContext({
      viewport: {width, height},
      extraHTTPHeaders: {"X-Token": process.env.FLS_TOKEN}
    });
    const page = await context.newPage();
    let current = null;
    page.on("console", msg => {
      if (current && msg.type() === "error") current.consoleErrors.push(msg.text());
    });
    page.on("pageerror", error => {
      if (current) current.consoleErrors.push(String(error));
    });
    page.on("requestfailed", request => {
      if (current) current.requestFailures.push(request.url());
    });
    const pageSet = (width === 390 || width === 1280)
      ? pages
      : pages.filter(item => !focusedFormPaths.has(item[0]));
    for (const [path, expectedClass] of pageSet) {
      const result = {width, height, path, issues: []};
      result.consoleErrors = [];
      result.requestFailures = [];
      current = result;
      try {
        const response = await page.goto(
          process.env.FLS_BASE_URL + path,
          {waitUntil: "domcontentloaded", timeout: 8000}
        );
        await page.waitForTimeout(120);
        await page.evaluate(() => { document.documentElement.style.scrollBehavior = "auto"; });
        result.status = response ? response.status() : 0;
        if (result.status !== 200) issue(result, "http", String(result.status));
        const state = await page.evaluate(() => {
          const body = document.body;
          const topbar = document.querySelector(".topbar");
          const menu = document.querySelector("#flsFloatMenuBtn");
          const rect = topbar ? topbar.getBoundingClientRect() : null;
          const visible = el => {
            if (!el || el.disabled || el.type === "hidden") return false;
            const style = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return style.display !== "none" && style.visibility !== "hidden" && r.width > 0 && r.height > 0;
          };
          const detailsDepth = el => {
            let depth = 0;
            for (let node = el; node; node = node.parentElement) {
              if (node.tagName === "DETAILS") depth += 1;
            }
            return depth;
          };
          let focusRulePresent = false;
          Array.from(document.styleSheets).forEach(sheet => {
            try {
              Array.from(sheet.cssRules || []).forEach(rule => {
                const text = rule.cssText || "";
                if (text.includes("focus-visible") && text.includes("outline")) focusRulePresent = true;
              });
            } catch (error) {}
          });
          const firstListItem = document.querySelector(".mobile-list .mobile-list-item");
          const mobileFirstList = firstListItem ? {
            name: !!firstListItem.querySelector(".task-mobile-title,.fls-card-title-main,.fls-card-title"),
            status: !!firstListItem.querySelector(".status-badge,.badge,[role=\"status\"]"),
            action: !!firstListItem.querySelector(".task-mobile-primary-action .btn,.fls-card-primary-action .btn,.fls-card-actions .btn,.fls-btn-line .btn")
          } : null;
          const dangerousPattern = /删除|停止|结束|移出|卸载|重启|停止安装/;
          let dangerousOutside = 0;
          document.querySelectorAll("button,a,input[type=submit]").forEach(control => {
            if (dangerousPattern.test(control.textContent || control.value || "") && !control.closest(".row-actions-danger,.danger-confirm")) {
              dangerousOutside += 1;
            }
          });
          let maxConsecutiveCards = 0;
          let consecutiveCards = 0;
          let lastCardParent = null;
          let lastCard = null;
          Array.from(document.querySelectorAll(".card")).forEach(card => {
            if (card.parentElement === lastCardParent && card.previousElementSibling === lastCard) {
              consecutiveCards += 1;
            } else {
              consecutiveCards = 1;
            }
            maxConsecutiveCards = Math.max(maxConsecutiveCards, consecutiveCards);
            lastCardParent = card.parentElement;
            lastCard = card;
          });
          let primaryActionOverages = 0;
          const isVisible = el => {
            const style = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return style.display !== "none" && style.visibility !== "hidden" && r.width > 0 && r.height > 0;
          };
          document.querySelectorAll(".row-actions,.fls-card-actions,.task-mobile-primary-action").forEach(row => {
            const primary = row.querySelectorAll(".btn-primary");
            const visiblePrimary = Array.from(primary).filter(isVisible);
            if (visiblePrimary.length > 1) primaryActionOverages += 1;
          });
          const formCardCount = Array.from(document.forms).reduce((count, form) => {
            return count + form.querySelectorAll(".card").length;
          }, 0);
          const formSectionCount = document.querySelectorAll("form > .fls-form-section").length;
          let tableActionVisibilityFailures = 0;
          if (window.innerWidth >= 768) {
            document.querySelectorAll(".table-wrap").forEach(wrapper => {
              const table = wrapper.querySelector("table");
              const actionCell = table && table.querySelector("tbody tr:not(.empty-state-row) td:last-child");
              if (!actionCell || !actionCell.querySelector("button,a,form")) return;

              const before = wrapper.scrollLeft;
              wrapper.scrollLeft = wrapper.scrollWidth;
              const wrapperRect = wrapper.getBoundingClientRect();
              const actionRect = actionCell.getBoundingClientRect();
              if (actionRect.left < wrapperRect.left - 1 || actionRect.right > wrapperRect.right + 1) {
                tableActionVisibilityFailures += 1;
              }
              wrapper.scrollLeft = before;
            });
          }
          return {
            bodyClass: body ? body.className : "",
            scrollWidth: document.documentElement.scrollWidth,
            innerWidth: window.innerWidth,
            topbar: rect ? {top: rect.top, bottom: rect.bottom, height: rect.height} : null,
            menu: menu ? {x: menu.getBoundingClientRect().x, y: menu.getBoundingClientRect().y} : null,
            nestedCards: document.querySelectorAll(".card .card").length,
            maxConsecutiveCards,
            formCardCount,
            formSectionCount,
            tableActionVisibilityFailures,
            primaryActionOverages,
            detailsMaxDepth: Math.max(0, ...Array.from(document.querySelectorAll("details")).map(detailsDepth)),
            focusFailures: focusRulePresent ? 0 : 1,
            mobileFirstList,
            dangerousOutside,
            invalidDetails: Array.from(document.querySelectorAll("details")).filter(el => {
              const allowed = ["detail-disclosure", "fls-collapsible-value", "fls-update-log-fold", "log-group-card"];
              return !allowed.some(name => el.classList.contains(name));
            }).length,
            unnamedControls: Array.from(document.querySelectorAll("button, a, input, select, textarea")).filter(el => {
              if (el.type === "hidden") return false;
              if (el.tagName === "INPUT" && ["checkbox", "radio", "submit", "button"].includes(el.type)) {
                return !(el.getAttribute("aria-label") || el.value || el.title || el.labels?.length);
              }
              return !(el.getAttribute("aria-label") || el.title || el.textContent?.trim() || el.labels?.length);
            }).length,
            undersizedControls: Array.from(document.querySelectorAll("button, a, input, select, textarea")).filter(el => {
              if (el.closest(".code, .help, .fls-source-code, pre")) return false;
              if (getComputedStyle(el).display === "none" || el.type === "hidden" || el.type === "checkbox" || el.type === "radio") return false;
              const r = el.getBoundingClientRect();
              return r.width > 0 && (r.width < 32 || r.height < 32);
            }).length
          };
        });
        result.scrollWidth = state.scrollWidth;
        if (!state.bodyClass.split(/\s+/).includes(expectedClass)) {
          issue(result, "body-class", state.bodyClass);
        }
        if (state.scrollWidth > width + 1) {
          issue(result, "document-overflow", `${state.scrollWidth} > ${width}`);
        }
        if (state.nestedCards) issue(result, "nested-cards", String(state.nestedCards));
        if (state.maxConsecutiveCards > 2) issue(result, "consecutive-cards", String(state.maxConsecutiveCards));
        if (state.formCardCount) {
          issue(result, "form-card-wrapping", String(state.formCardCount));
        }
        if (state.tableActionVisibilityFailures) {
          issue(result, "table-action-visibility", String(state.tableActionVisibilityFailures));
        }
        const formPage = path === "/config" ||
          path.startsWith("/notify/") ||
          path.startsWith("/proxy/") ||
          path.startsWith("/task/") ||
          path.startsWith("/collection/") ||
          path.startsWith("/env/") ||
          path.startsWith("/pull/") ||
          path.startsWith("/scripts/") ||
          path.startsWith("/online-scripts/source") ||
          path.startsWith("/online-scripts/doc/") ||
          path.startsWith("/online-scripts/install-select/");
        if (formPage && !state.formSectionCount) {
          issue(result, "form-section-structure", String(state.formSectionCount));
        }
        if (state.primaryActionOverages) issue(result, "row-primary-actions", String(state.primaryActionOverages));
        if (state.detailsMaxDepth > 1) issue(result, "nested-details", String(state.detailsMaxDepth));
        if (state.focusFailures) issue(result, "focus-visibility", String(state.focusFailures));
        if (width <= 767 && state.mobileFirstList && (!state.mobileFirstList.name || !state.mobileFirstList.status || !state.mobileFirstList.action)) {
          issue(result, "mobile-first-item", JSON.stringify(state.mobileFirstList));
        }
        if (state.dangerousOutside) issue(result, "danger-action-group", String(state.dangerousOutside));
        if (state.invalidDetails) issue(result, "details-boundary", String(state.invalidDetails));
        if (state.unnamedControls) issue(result, "unnamed-control", String(state.unnamedControls));
        if (state.undersizedControls) issue(result, "small-control", String(state.undersizedControls));
        for (const target of [0, 500, 1000]) {
          await page.evaluate(y => window.scrollTo({top: y, left: 0, behavior: "auto"}), target);
          await page.waitForTimeout(40);
          const top = await page.locator(".topbar").boundingBox();
          if (!top || Math.abs(top.y) > 1) issue(result, "topbar-not-sticky", JSON.stringify(top));
        }
        if (width <= 767) {
          await page.evaluate(() => window.scrollTo({top: document.documentElement.scrollHeight, left: 0, behavior: "auto"}));
          await page.waitForTimeout(50);
          const fixedOverlap = await page.evaluate(() => {
            const overlays = Array.from(document.querySelectorAll(".fls-form-float-actions.show,.fls-log-float"))
              .filter(el => getComputedStyle(el).position === "fixed" && getComputedStyle(el).display !== "none")
              .map(el => ({el, rect: el.getBoundingClientRect()}));
            if (!overlays.length) return 0;
            const fields = Array.from(document.querySelectorAll("input,select,textarea,button[type=submit],.form-item:last-child"))
              .filter(el => {
                const style = getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return style.display !== "none" && style.visibility !== "hidden" && r.width > 0 && r.height > 0;
              });
            if (!fields.length) return 0;
            const last = fields.reduce((a, b) => a.getBoundingClientRect().bottom > b.getBoundingClientRect().bottom ? a : b);
            const lastRect = last.getBoundingClientRect();
            return overlays.filter(item => lastRect.bottom > item.rect.top && lastRect.top < item.rect.bottom).length;
          });
          if (fixedOverlap) issue(result, "fixed-action-overlap", String(fixedOverlap));
        }
        if (width <= 767) {
          await page.locator("#flsFloatMenuBtn").click();
          await page.waitForTimeout(80);
          const layering = await page.evaluate(() => {
            const sidebar = document.querySelector("#sidebar").getBoundingClientRect();
            const mask = document.querySelector("#mask").getBoundingClientRect();
            const save = document.querySelector("#flsFormFloatActions");
            return {
              sidebar, mask,
              menuOpen: document.body.classList.contains("fls-menu-open"),
              saveVisible: !!save && getComputedStyle(save).display !== "none"
            };
          });
          if (!layering.menuOpen) issue(result, "menu-state", "menu did not open");
          if (layering.saveVisible) issue(result, "floating-control-through-mask", "save action visible");
          await page.locator("#mask").click({position: {x: Math.max(width - 12, 252), y: 100}, timeout: 2000});
          await page.waitForTimeout(40);
          if (await page.locator("#sidebar").evaluate(el => el.classList.contains("open"))) {
            issue(result, "menu-state", "mask did not close menu");
          }
        }
        if (width > 767 && width <= 1199) {
          await page.locator("#flsFloatMenuBtn").click();
          await page.waitForTimeout(80);
          const tabletMenu = await page.evaluate(() => ({
            open: document.body.classList.contains("fls-menu-open"),
            sidebarOpen: document.querySelector("#sidebar")?.classList.contains("open"),
            menuVisible: getComputedStyle(document.querySelector("#flsFloatMenuBtn")).display !== "none"
          }));
          if (!tabletMenu.open || !tabletMenu.sidebarOpen || !tabletMenu.menuVisible) {
            issue(result, "tablet-menu-state", JSON.stringify(tabletMenu));
          }
          await page.keyboard.press("Escape");
          const tabletEscaped = await page.evaluate(() => ({
            open: document.body.classList.contains("fls-menu-open"),
            focus: document.activeElement && document.activeElement.id,
            expanded: document.querySelector("#flsFloatMenuBtn")?.getAttribute("aria-expanded")
          }));
          if (tabletEscaped.open || tabletEscaped.focus !== "flsFloatMenuBtn" || tabletEscaped.expanded !== "false") {
            issue(result, "tablet-menu-escape", JSON.stringify(tabletEscaped));
          }
        }
      } catch (error) {
        issue(result, "navigation", String(error));
      }
      if (result.consoleErrors.length) issue(result, "console", result.consoleErrors.join(" | "));
      if (result.requestFailures.length) issue(result, "requestfailed", result.requestFailures.join(" | "));
      delete result.consoleErrors;
      delete result.requestFailures;
      results.push(result);
      current = null;
    }
    // Verify the shared AJAX navigation and browser history once per viewport.
    const interaction = {issues: [], consoleErrors: [], requestFailures: []};
    current = interaction;
    try {
      await page.goto(process.env.FLS_BASE_URL + "/tasks", {
        waitUntil: "domcontentloaded", timeout: 8000
      });
      await page.evaluate(() => { document.documentElement.style.scrollBehavior = "auto"; });
      await page.evaluate(() => window.scrollTo({top: 0, left: 0, behavior: "auto"}));
      if (width <= 1199) {
        await page.locator("#flsFloatMenuBtn").click({timeout: 2000});
        await page.keyboard.press("Escape");
        const escaped = await page.evaluate(() => ({
          open: document.body.classList.contains("fls-menu-open"),
          focus: document.activeElement && document.activeElement.id,
          expanded: document.querySelector("#flsFloatMenuBtn")?.getAttribute("aria-expanded")
        }));
        if (escaped.open || escaped.focus !== "flsFloatMenuBtn" || escaped.expanded !== "false") {
          issue(interaction, "menu-escape", JSON.stringify(escaped));
        }
        await page.locator("#flsFloatMenuBtn").click({timeout: 2000});
      }
      await page.locator('.nav a[href="/config"]').click({timeout: 2000});
      await page.waitForTimeout(250);
      const replaced = await page.evaluate(() => ({
        page: document.body.className,
        menuOpen: document.body.classList.contains("fls-menu-open"),
        url: location.pathname
      }));
      if (!replaced.page.split(/\s+/).includes("page-config")) {
        issue(interaction, "ajax-navigation", replaced.page);
      }
      if (replaced.url !== "/config") {
        issue(interaction, "ajax-navigation", replaced.url);
      }
      if (replaced.menuOpen) issue(interaction, "ajax-navigation", "menu remained open");
      await page.goBack({waitUntil: "domcontentloaded", timeout: 8000});
      await page.waitForTimeout(120);
      const returned = await page.evaluate(() => ({
        page: document.body.className,
        url: location.pathname
      }));
      if (!returned.page.split(/\s+/).includes("page-tasks")) {
        issue(interaction, "history", returned.page);
      }
      if (returned.url !== "/tasks") issue(interaction, "history", returned.url);
    } catch (error) {
      issue(interaction, "interaction", String(error));
    }
    current = null;
    if (interaction.consoleErrors.length) {
      issue(interaction, "console", interaction.consoleErrors.join(" | "));
    }
    if (interaction.requestFailures.length) {
      issue(interaction, "requestfailed", interaction.requestFailures.join(" | "));
    }
    if (interaction.issues.length) {
      const first = results.find(item => item.width === width && item.path === "/");
      if (first) first.issues.push(...interaction.issues);
    }
    await context.close();
  }
  if (process.env.FLS_ACTION_FIXTURE === "1") {
    const actionContext = await browser.newContext({
      viewport: {width: 1280, height: 800},
      extraHTTPHeaders: {"X-Token": process.env.FLS_TOKEN}
    });
    const actionPage = await actionContext.newPage();
    const actionAudit = {width: 1280, height: 800, path: "/__action-audit__", issues: []};
    const acceptDialogs = dialog => dialog.accept();
    actionPage.on("dialog", acceptDialogs);
    try {
      await runActionAudit(actionPage, actionAudit);
    } catch (error) {
      issue(actionAudit, "action-audit", String(error));
    }
    actionPage.off("dialog", acceptDialogs);
    await actionContext.close();
    results.push(actionAudit);
  }
  await browser.close();
  const failures = results.filter(item => item.issues.length);
  console.log(JSON.stringify({total: results.length, failures}, null, 2));
  process.exit(failures.length ? 1 : 0);
})().catch(error => { console.error(error.stack || String(error)); process.exit(2); });
'''


def run_node(base_url, playwright_core, chromium, token, action_fixture=False):
    env = os.environ.copy()
    env.update(
        FLS_BASE_URL=base_url.rstrip("/"),
        FLS_TOKEN=token,
        FLS_PLAYWRIGHT_CORE=str(playwright_core),
        FLS_CHROMIUM=chromium,
        FLS_ACTION_FIXTURE="1" if action_fixture else "0",
    )
    # Chromium inherits the Node stdout descriptor.  A temporary regular file
    # avoids waiting on an inherited pipe after Playwright closes the browser.
    with tempfile.TemporaryFile(mode="w+") as output:
        try:
            completed = subprocess.run(
                ["node", "-e", node_script()],
                cwd=ROOT,
                env=env,
                text=True,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            # Keep a hung browser run from leaving the Flask fixture and
            # Chromium child alive indefinitely while still reporting a
            # deterministic non-zero result to the caller.
            completed = subprocess.CompletedProcess(
                ["node", "-e", node_script()], 124
            )
            output.write("browser regression timed out after 120s\n")
        output.seek(0)
        completed.stdout = output.read()
        return completed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", help="reuse an already running FLS server")
    parser.add_argument("--token", default=TOKEN, help="token sent in X-Token header")
    args = parser.parse_args()

    playwright_core = find_playwright_core()
    chromium = find_chromium()
    if not playwright_core or not chromium:
        missing = []
        if not playwright_core:
            missing.append("FLS_PLAYWRIGHT_CORE")
        if not chromium:
            missing.append("FLS_CHROMIUM/chromium-browser")
        print("SKIP browser regression; missing " + ", ".join(missing))
        return 2

    process = None
    temp_dir = None
    try:
        if args.base_url:
            base_url = args.base_url.rstrip("/")
        else:
            temp_dir = tempfile.TemporaryDirectory(prefix="fls-browser-regression-")
            seed_browser_fixture(temp_dir.name)
            env = os.environ.copy()
            env.update(
                FLS_BASE_DIR=temp_dir.name,
                FLS_TOKEN=TOKEN,
                FLS_SECRET_KEY="browser-regression-secret",
                FLS_PORT=PORT,
                FLS_HOST="127.0.0.1",
                PYTHONDONTWRITEBYTECODE="1",
            )
            process = subprocess.Popen(
                [sys.executable, "fls-manager.py"],
                cwd=ROOT,
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
            base_url = f"http://127.0.0.1:{PORT}"
            wait_for_server(base_url + "/", process)

        result = run_node(
            base_url,
            playwright_core,
            chromium,
            args.token,
            action_fixture=bool(temp_dir),
        )
        print(result.stdout, end="")
        return result.returncode
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        if temp_dir is not None:
            temp_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
