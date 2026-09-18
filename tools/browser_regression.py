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
  ["/history", "page-history"],
  ["/env", "page-env"],
  ["/proxy", "page-proxy"],
  ["/pull", "page-pull"],
  ["/online-scripts", "page-online_scripts"],
  ["/backup", "page-backup"],
  ["/deps", "page-deps"],
  ["/logs", "page-logs"],
  ["/notify", "page-notify"],
  ["/panel/status", "page-status"],
  ["/config", "page-config"],
  ["/about", "page-about"]
];
const viewports = [
  [390, 844], [412, 915], [768, 1024],
  [1024, 768], [1280, 800], [1440, 900]
];

function issue(result, kind, detail) {
  result.issues.push({kind, detail});
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
    for (const [path, expectedClass] of pages) {
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
          return {
            bodyClass: body ? body.className : "",
            scrollWidth: document.documentElement.scrollWidth,
            innerWidth: window.innerWidth,
            topbar: rect ? {top: rect.top, bottom: rect.bottom, height: rect.height} : null,
            menu: menu ? {x: menu.getBoundingClientRect().x, y: menu.getBoundingClientRect().y} : null
          };
        });
        result.scrollWidth = state.scrollWidth;
        if (!state.bodyClass.split(/\s+/).includes(expectedClass)) {
          issue(result, "body-class", state.bodyClass);
        }
        if (state.scrollWidth > width + 1) {
          issue(result, "document-overflow", `${state.scrollWidth} > ${width}`);
        }
        for (const target of [0, 500, 1000]) {
          await page.evaluate(y => window.scrollTo({top: y, left: 0, behavior: "auto"}), target);
          await page.waitForTimeout(40);
          const top = await page.locator(".topbar").boundingBox();
          if (!top || Math.abs(top.y) > 1) issue(result, "topbar-not-sticky", JSON.stringify(top));
        }
        if (width <= 900) {
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
      if (width <= 900) {
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
  await browser.close();
  const failures = results.filter(item => item.issues.length);
  console.log(JSON.stringify({total: results.length, failures}, null, 2));
  process.exit(failures.length ? 1 : 0);
})().catch(error => { console.error(error.stack || String(error)); process.exit(2); });
'''


def run_node(base_url, playwright_core, chromium, token):
    env = os.environ.copy()
    env.update(
        FLS_BASE_URL=base_url.rstrip("/"),
        FLS_TOKEN=token,
        FLS_PLAYWRIGHT_CORE=str(playwright_core),
        FLS_CHROMIUM=chromium,
    )
    # Chromium inherits the Node stdout descriptor.  A temporary regular file
    # avoids waiting on an inherited pipe after Playwright closes the browser.
    with tempfile.TemporaryFile(mode="w+") as output:
        completed = subprocess.run(
            ["node", "-e", node_script()],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=output,
            stderr=subprocess.STDOUT,
            check=False,
        )
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

        result = run_node(base_url, playwright_core, chromium, args.token)
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
