#!/usr/bin/env python3
import asyncio
import gzip
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
PROOF_DIR = Path(__file__).resolve().parent
RESULTS_PATH = PROOF_DIR / "results.json"
README_PATH = PROOF_DIR / "README.md"
INDEX_PATH = ROOT / "index.html"
MULTIUSER_PATH = ROOT / "multiuser-postgres.html"
BROWSER_CANDIDATES = ("chromium", "chromium-browser", "google-chrome")

INDEX_SECTIONS = [
    ("modal", "Modal Dialog", 0, 1),
    ("darkmode", "Dark Mode Toggle", 2, 3),
    ("form", "Form Validation", 4, 5),
    ("accordion", "Accordion/Collapsible", 6, 7),
]


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def line_count(block: str) -> int:
    return sum(1 for line in block.splitlines() if line.strip())


def round_ms(value: float) -> float:
    return round(value, 3)


def size_metrics(path: Path) -> dict:
    html = path.read_text(encoding="utf-8")
    raw_bytes = path.stat().st_size
    gzip_bytes = len(gzip.compress(html.encode("utf-8"), compresslevel=9, mtime=0))
    return {
        "raw_bytes": raw_bytes,
        "raw_kib": round(raw_bytes / 1024, 2),
        "gzip_bytes": gzip_bytes,
        "gzip_kib": round(gzip_bytes / 1024, 2),
    }


def extract_index_claims() -> list[dict]:
    html = INDEX_PATH.read_text(encoding="utf-8")
    pre_blocks = re.findall(r"<pre>(.*?)</pre>", html, flags=re.S)
    claims = []

    for section, title, html_idx, react_idx in INDEX_SECTIONS:
        actual_html = line_count(pre_blocks[html_idx])
        actual_react = line_count(pre_blocks[react_idx])
        match = re.search(
            rf'<div class="comparison-title">\s*{re.escape(title)}\s*</div>\s*<div class="bundle-size">(.*?)</div>',
            html,
            flags=re.S,
        )
        label = re.sub(r"\s+", " ", match.group(1)).strip() if match else ""
        label_html = re.search(r"HTML:\s*(\d+)\s*lines?", label)
        label_react = re.search(r"React:\s*(\d+)\s*lines?", label)
        claim = {
            "section": section,
            "title": title,
            "label": label,
            "actual_html_lines": actual_html,
            "actual_react_lines": actual_react,
            "label_html_lines": int(label_html.group(1)) if label_html else None,
            "label_react_lines": int(label_react.group(1)) if label_react else None,
        }
        claim["status"] = (
            "pass"
            if claim["label_html_lines"] == actual_html and claim["label_react_lines"] == actual_react
            else "fail"
        )
        claims.append(claim)

    return claims


def check_record(name: str, status: str, details: dict) -> dict:
    return {"name": name, "status": status, "details": details}


def browser_binary() -> str:
    for candidate in BROWSER_CANDIDATES:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise FileNotFoundError(
        f"Could not find a Chromium-based browser. Tried: {', '.join(BROWSER_CANDIDATES)}"
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get_json(url: str) -> dict | list:
    with urllib.request.urlopen(url, timeout=1) as response:
        return json.load(response)


def wait_http_ready(url: str, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=0.5).close()
            return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for HTTP server: {url}")


def wait_page_target(devtools_port: int, url_suffix: str, timeout_s: float = 10.0) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            targets = get_json(f"http://127.0.0.1:{devtools_port}/json/list")
            for target in targets:
                if target.get("type") == "page" and target.get("url", "").endswith(url_suffix):
                    return target["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for browser target: {url_suffix}")


class DevToolsPage:
    def __init__(self, websocket_url: str):
        self.websocket_url = websocket_url
        self._message_id = 0
        self._ws = None

    async def __aenter__(self):
        self._ws = await websockets.connect(self.websocket_url, max_size=None)
        await self.call("Page.enable")
        await self.call("Runtime.enable")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._ws is not None:
            await self._ws.close()

    async def call(self, method: str, params: dict | None = None) -> dict:
        self._message_id += 1
        call_id = self._message_id
        await self._ws.send(
            json.dumps({"id": call_id, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(await self._ws.recv())
            if message.get("id") != call_id:
                continue
            if "error" in message:
                raise RuntimeError(f"{method} failed: {message['error']}")
            return message["result"]

    async def evaluate(self, expression: str, await_promise: bool = True):
        result = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
        )
        if "exceptionDetails" in result:
            description = result["result"].get("description") or result["result"].get("value")
            raise RuntimeError(f"Runtime.evaluate failed: {description}")
        remote = result["result"]
        if remote.get("type") == "undefined":
            return None
        return remote.get("value")

    async def wait_for(self, expression: str, timeout_s: float = 10.0):
        deadline = time.monotonic() + timeout_s
        last_error = None
        while time.monotonic() < deadline:
            try:
                value = await self.evaluate(expression)
                if value:
                    return value
            except Exception as exc:
                last_error = exc
            await asyncio.sleep(0.05)
        raise TimeoutError(f"Timed out waiting for condition: {expression}. Last error: {last_error}")

    async def navigate(self, url: str) -> None:
        await self.call("Page.navigate", {"url": url})
        await self.wait_for("document.readyState === 'complete'")


async def navigation_metrics(page: DevToolsPage) -> dict:
    metrics = await page.evaluate(
        """
        (() => {
          const nav = performance.getEntriesByType('navigation')[0];
          if (!nav) return null;
          return {
            response_start_ms: nav.responseStart,
            dom_content_loaded_ms: nav.domContentLoadedEventEnd,
            load_ms: nav.loadEventEnd,
            duration_ms: nav.duration
          };
        })()
        """
    )
    if metrics is None:
        return {}
    return {key: round_ms(value) for key, value in metrics.items()}


async def index_functionality_checks(page: DevToolsPage) -> list[dict]:
    checks = []

    toggle_result = await page.evaluate(
        """
        (() => {
          const sections = ['modal', 'darkmode', 'form', 'accordion'];
          return sections.map((section) => {
            const el = document.getElementById(section + '-react');
            const before = el.classList.contains('show');
            toggleReactCode(section);
            const after_open = el.classList.contains('show');
            toggleReactCode(section);
            const after_close = el.classList.contains('show');
            return { section, before, after_open, after_close };
          });
        })()
        """
    )
    toggle_ok = all(
        item["before"] is False and item["after_open"] is True and item["after_close"] is False
        for item in toggle_result
    )
    checks.append(
        check_record(
            "react_code_toggles",
            "pass" if toggle_ok else "fail",
            {"sections": toggle_result},
        )
    )

    dialog_result = await page.evaluate(
        """
        (() => {
          const dialog = document.getElementById('demoModal');
          document.querySelector('button[onclick="demoModal.showModal()"]').click();
          const after_open = dialog.open;
          document.querySelector('#demoModal button').click();
          return { after_open, after_close: dialog.open };
        })()
        """
    )
    checks.append(
        check_record(
            "native_dialog_open_close",
            "pass" if dialog_result["after_open"] and not dialog_result["after_close"] else "fail",
            dialog_result,
        )
    )

    form_result = await page.evaluate(
        """
        (() => {
          const form = document.querySelector('.demo-content form');
          const [email, password] = form.querySelectorAll('input');
          const blank_valid = form.checkValidity();
          email.value = 'user@example.com';
          password.value = 'hunter422';
          const filled_valid = form.checkValidity();
          return { blank_valid, filled_valid };
        })()
        """
    )
    checks.append(
        check_record(
            "native_form_validation",
            "pass" if (not form_result["blank_valid"]) and form_result["filled_valid"] else "fail",
            form_result,
        )
    )

    accordion_result = await page.evaluate(
        """
        (() => {
          const details = document.querySelector('.demo-content details');
          const summary = details.querySelector('summary');
          const before = details.open;
          summary.click();
          const after_open = details.open;
          summary.click();
          return { before, after_open, after_close: details.open };
        })()
        """
    )
    checks.append(
        check_record(
            "native_accordion_toggle",
            "pass"
            if (not accordion_result["before"])
            and accordion_result["after_open"]
            and (not accordion_result["after_close"])
            else "fail",
            accordion_result,
        )
    )

    dark_mode_result = await page.evaluate(
        """
        (() => {
          const button = document.querySelector('.demo-content button[onclick*="dark-mode"]');
          const before = document.body.classList.contains('dark-mode');
          button.click();
          const after_toggle = document.body.classList.contains('dark-mode');
          button.click();
          return { before, after_toggle, after_reset: document.body.classList.contains('dark-mode') };
        })()
        """
    )
    checks.append(
        check_record(
            "dark_mode_toggle",
            "pass"
            if (not dark_mode_result["before"])
            and dark_mode_result["after_toggle"]
            and (not dark_mode_result["after_reset"])
            else "fail",
            dark_mode_result,
        )
    )

    return checks


async def multiuser_functionality_checks(page: DevToolsPage) -> list[dict]:
    checks = []

    toggle_result = await page.evaluate(
        """
        (() => {
          const sections = ['users', 'chat', 'analytics'];
          return sections.map((section) => {
            const el = document.getElementById(section + '-react');
            const before = el.classList.contains('show');
            toggleReactCode(section);
            const after_open = el.classList.contains('show');
            toggleReactCode(section);
            const after_close = el.classList.contains('show');
            return { section, before, after_open, after_close };
          });
        })()
        """
    )
    toggle_ok = all(
        item["before"] is False and item["after_open"] is True and item["after_close"] is False
        for item in toggle_result
    )
    checks.append(
        check_record(
            "react_code_toggles",
            "pass" if toggle_ok else "fail",
            {"sections": toggle_result},
        )
    )

    db_result = await page.evaluate(
        """
        (async () => {
          const el = document.getElementById('dbTime');
          const before = el.textContent;
          simulateDbQuery();
          const deadline = performance.now() + 2500;
          while (performance.now() < deadline) {
            if (el.textContent !== before && el.textContent !== '-') {
              return { ok: true, value: el.textContent };
            }
            await new Promise((resolve) => setTimeout(resolve, 25));
          }
          return { ok: false, value: el.textContent };
        })()
        """
    )
    checks.append(check_record("simulate_db_query", "pass" if db_result["ok"] else "fail", db_result))

    react_result = await page.evaluate(
        """
        (async () => {
          const el = document.getElementById('reactTime');
          const before = el.textContent;
          simulateReactUpdates();
          const deadline = performance.now() + 2500;
          while (performance.now() < deadline) {
            if (el.textContent !== before && el.textContent !== '-') {
              return { ok: true, value: el.textContent };
            }
            await new Promise((resolve) => setTimeout(resolve, 25));
          }
          return { ok: false, value: el.textContent };
        })()
        """
    )
    checks.append(
        check_record("simulate_react_updates", "pass" if react_result["ok"] else "fail", react_result)
    )

    bundle_result = await page.evaluate(
        """
        (async () => {
          const pg = document.getElementById('pgLoad');
          const react = document.getElementById('reactLoad');
          measureBundleImpact();
          const deadline = performance.now() + 2500;
          while (performance.now() < deadline) {
            if (pg.textContent === '187ms' && react.textContent === '3.2s') {
              return { ok: true, pg: pg.textContent, react: react.textContent };
            }
            await new Promise((resolve) => setTimeout(resolve, 25));
          }
          return { ok: false, pg: pg.textContent, react: react.textContent };
        })()
        """
    )
    checks.append(
        check_record("measure_bundle_impact", "pass" if bundle_result["ok"] else "fail", bundle_result)
    )

    return checks


def page_summary(checks: list[dict]) -> dict:
    passed = sum(1 for check in checks if check["status"] == "pass")
    failed = len(checks) - passed
    return {"passed": passed, "failed": failed, "total": len(checks)}


def build_readme(results: dict) -> str:
    lines = [
        "# Proof Bundle",
        "",
        f"Generated: `{results['generated_at_utc']}`",
        "",
        "End-to-end validation ran against a local `python -m http.server` using headless Chromium.",
        "",
        "## Summary",
        f"- Browser: **{results['tooling']['browser']}**",
        f"- Pages validated: **{len(results['pages'])}**",
        f"- Checks passed: **{results['summary']['passed_checks']} / {results['summary']['total_checks']}**",
        "",
    ]

    index_page = results["pages"]["index.html"]
    lines.extend(
        [
            "## `index.html`",
            f"- Raw bytes: **{index_page['size']['raw_bytes']} B** ({index_page['size']['raw_kib']} KiB)",
            f"- Gzip bytes: **{index_page['size']['gzip_bytes']} B** ({index_page['size']['gzip_kib']} KiB)",
            (
                "- Browser navigation: "
                f"response start **{index_page['navigation_ms']['response_start_ms']} ms**, "
                f"DOMContentLoaded **{index_page['navigation_ms']['dom_content_loaded_ms']} ms**, "
                f"load **{index_page['navigation_ms']['load_ms']} ms**"
            ),
            f"- Claim checks: **{index_page['claim_summary']['passed']} / {index_page['claim_summary']['total']}**",
        ]
    )
    for claim in index_page["claims"]:
        lines.append(
            (
                f"  - {claim['title']}: "
                f"HTML **{claim['actual_html_lines']}** / React **{claim['actual_react_lines']}** "
                f"(`{claim['status']}`)"
            )
        )
    lines.append(
        f"- Functionality checks: **{index_page['functionality_summary']['passed']} / {index_page['functionality_summary']['total']}**"
    )
    for check in index_page["functionality_checks"]:
        lines.append(f"  - {check['name']}: `{check['status']}`")
    lines.append("")

    multiuser_page = results["pages"]["multiuser-postgres.html"]
    lines.extend(
        [
            "## `multiuser-postgres.html`",
            f"- Raw bytes: **{multiuser_page['size']['raw_bytes']} B** ({multiuser_page['size']['raw_kib']} KiB)",
            f"- Gzip bytes: **{multiuser_page['size']['gzip_bytes']} B** ({multiuser_page['size']['gzip_kib']} KiB)",
            (
                "- Browser navigation: "
                f"response start **{multiuser_page['navigation_ms']['response_start_ms']} ms**, "
                f"DOMContentLoaded **{multiuser_page['navigation_ms']['dom_content_loaded_ms']} ms**, "
                f"load **{multiuser_page['navigation_ms']['load_ms']} ms**"
            ),
            (
                f"- Functionality checks: "
                f"**{multiuser_page['functionality_summary']['passed']} / {multiuser_page['functionality_summary']['total']}**"
            ),
        ]
    )
    for check in multiuser_page["functionality_checks"]:
        lines.append(f"  - {check['name']}: `{check['status']}`")
    lines.extend(
        [
            "",
            "## Reproduce",
            "```bash",
            "python proof/generate_proof.py",
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


async def generate_results(http_port: int, devtools_port: int, browser_path: str) -> dict:
    base_url = f"http://127.0.0.1:{http_port}"
    first_url = f"{base_url}/index.html"
    websocket_url = wait_page_target(devtools_port, "/index.html")

    async with DevToolsPage(websocket_url) as page:
        await page.navigate(first_url)
        index_navigation = await navigation_metrics(page)
        index_checks = await index_functionality_checks(page)

        await page.navigate(f"{base_url}/multiuser-postgres.html")
        multiuser_navigation = await navigation_metrics(page)
        multiuser_checks = await multiuser_functionality_checks(page)

    claims = extract_index_claims()
    claim_summary = page_summary(claims)
    index_functionality_summary = page_summary(index_checks)
    multiuser_functionality_summary = page_summary(multiuser_checks)

    total_checks = (
        claim_summary["total"]
        + index_functionality_summary["total"]
        + multiuser_functionality_summary["total"]
    )
    passed_checks = (
        claim_summary["passed"]
        + index_functionality_summary["passed"]
        + multiuser_functionality_summary["passed"]
    )

    return {
        "generated_at_utc": utc_now(),
        "tooling": {
            "browser": subprocess.check_output([browser_path, "--version"], text=True).strip(),
            "server": f"{Path(sys.executable).name} -m http.server",
        },
        "summary": {
            "passed_checks": passed_checks,
            "failed_checks": total_checks - passed_checks,
            "total_checks": total_checks,
        },
        "pages": {
            "index.html": {
                "size": size_metrics(INDEX_PATH),
                "navigation_ms": index_navigation,
                "claims": claims,
                "claim_summary": claim_summary,
                "functionality_checks": index_checks,
                "functionality_summary": index_functionality_summary,
            },
            "multiuser-postgres.html": {
                "size": size_metrics(MULTIUSER_PATH),
                "navigation_ms": multiuser_navigation,
                "functionality_checks": multiuser_checks,
                "functionality_summary": multiuser_functionality_summary,
            },
        },
    }


def main() -> int:
    http_port = free_port()
    devtools_port = free_port()
    browser_path = browser_binary()
    server = None
    browser = None
    user_data_dir = None

    try:
        server = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(http_port), "--bind", "127.0.0.1"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_http_ready(f"http://127.0.0.1:{http_port}/index.html")

        user_data_dir = tempfile.mkdtemp(prefix="proof-browser-")
        browser = subprocess.Popen(
            [
                browser_path,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                f"--remote-debugging-port={devtools_port}",
                f"--user-data-dir={user_data_dir}",
                f"http://127.0.0.1:{http_port}/index.html",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        wait_http_ready(f"http://127.0.0.1:{devtools_port}/json/version")

        results = asyncio.run(generate_results(http_port, devtools_port, browser_path))
        RESULTS_PATH.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
        README_PATH.write_text(build_readme(results), encoding="utf-8")

        print(
            f"Wrote {RESULTS_PATH.relative_to(ROOT)} and {README_PATH.relative_to(ROOT)} "
            f"({results['summary']['passed_checks']}/{results['summary']['total_checks']} checks passed)"
        )
        return 0 if results["summary"]["failed_checks"] == 0 else 1
    finally:
        if browser is not None:
            browser.terminate()
            try:
                browser.wait(timeout=5)
            except Exception:
                browser.kill()
        if server is not None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except Exception:
                server.kill()
        if user_data_dir is not None:
            shutil.rmtree(user_data_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
