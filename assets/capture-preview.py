"""Capture the actual console with synthetic API responses; never contact AWS/GitHub.

Run from the repo root: uv run --with playwright==1.58.0 assets/capture-preview.py
Requires Chrome and ffmpeg. No production code or credentials are modified.
"""

import functools
import http.server
import json
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PROMPT = "Add a regression test for an uncovered edge case in an existing utility."
CONFIG = {"configId": "cfg_preview", "repository": "example/utility-tests", "status": "READY"}
STAGES = ["PLANNING", "CODING", "VERIFYING", "REVIEWING", "SUBMITTING", "SUCCEEDED"]
COLUMNS = ["PLANNING", "CODING", "VERIFYING", "VERIFYING", "VERIFYING", "DONE"]
STEPS = ["checkout", "plan", "code", "verify", "review", "submit"]


def capture():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT / "web")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    card = {
        "cardId": "card_preview",
        "configId": CONFIG["configId"],
        "title": PROMPT,
        "description": PROMPT,
        "storedColumn": "BACKLOG",
        "column": "BACKLOG",
    }
    task = {
        "taskId": "task_preview",
        "configId": CONFIG["configId"],
        "cardId": card["cardId"],
        "prompt": PROMPT,
        "source": "console",
        "status": "QUEUED",
        "steps": [],
    }
    errors = []

    def route_request(route):
        url = route.request.url
        if not url.startswith(origin + "/"):
            errors.append("Unexpected external request: " + url)
            return route.abort()
        path = url.removeprefix(origin).split("?")[0]
        if path == "/config.js":
            return route.fulfill(
                content_type="text/javascript", body="window.RCA_CONFIG={apiBase:'/api'};"
            )
        if not path.startswith("/api/"):
            return route.continue_()
        if path == "/api/configurations":
            payload = {"items": [CONFIG]}
        elif path == "/api/cards":
            payload = {"cards": [card]}
        elif path.endswith("/move"):
            assert route.request.method == "POST"
            assert route.request.post_data_json == {"column": "PLANNING"}
            card.update(
                storedColumn="ACTIVE", column="PLANNING", status="PLANNING", taskId=task["taskId"]
            )
            payload = card
        elif path.endswith("/activity"):
            payload = {
                "rows": [],
                "nextCursor": 0,
                "status": task["status"],
                "done": task["status"] in ("SUCCEEDED", "SUCCEEDED_UNVERIFIED"),
            }
        elif path == "/api/tasks/task_preview":
            payload = task
        else:
            errors.append("Unexpected API request: " + path)
            return route.abort()
        route.fulfill(content_type="application/json", body=json.dumps(payload))

    try:
        with tempfile.TemporaryDirectory(prefix="rca-preview-") as tmp, sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 960}, device_scale_factor=1)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.route("**/*", route_request)
            page.add_init_script("""
                sessionStorage.setItem('rca.idToken', 'preview.' + btoa(JSON.stringify({email:'preview@example.com'})) + '.unsigned');
                sessionStorage.setItem('rca.expiresAt', '9999999999999');
                document.addEventListener('DOMContentLoaded', () => {
                    const label = document.createElement('div');
                    label.textContent = 'UI PREVIEW · SYNTHETIC DATA · NOT A LIVE AWS RUN';
                    label.style.cssText = 'position:fixed;bottom:0;left:0;right:0;z-index:9999;background:#232f3e;color:white;padding:12px;text-align:center;font:16px Arial';
                    document.body.append(label);
                });
            """)
            page.goto(origin + "/#/boards/cfg_preview")
            page.get_by_role("button", name="Start run", exact=True).wait_for()
            frames = []

            def frame():
                dest = Path(tmp) / f"frame-{len(frames):02}.png"
                page.screenshot(path=str(dest))
                frames.append(dest)

            frame()
            page.get_by_role("button", name="Start run", exact=True).click()
            for i, (status, column) in enumerate(zip(STAGES, COLUMNS, strict=True)):
                task.update(
                    status=status,
                    steps=STEPS[: min(i + 2, len(STEPS))],
                    updatedAt=datetime.now(UTC).isoformat(),
                )
                card.update(status=status, column=column, updatedAt=task["updatedAt"])
                # The production board polls; wait for its real rendered status, not a fake DOM.
                label = "Succeeded" if status == "SUCCEEDED" else status.capitalize()
                page.locator(".card .chip").filter(has_text=label).wait_for(timeout=40000)
                assert (
                    column.lower()
                    in page.locator(".column")
                    .filter(has=page.locator(".card"))
                    .locator(".column-head")
                    .inner_text()
                    .lower()
                )
                frame()
            page.screenshot(path=str(ROOT / "assets/console-preview.png"))
            task["verifyResult"] = {
                "command": "npm test",
                "exitCode": 0,
                "verified": True,
                "summary": "Synthetic example: regression test passes.",
                "vetoed": False,
            }
            # A reserved example domain prevents a fixture from implying an actual published PR.
            task["prUrl"] = "https://example.com/preview/pull/1"
            page.goto(origin + "/#/tasks/cfg_preview/task_preview")
            page.get_by_text("Synthetic example: regression test passes.", exact=True).wait_for()
            assert (
                page.get_by_role("link", name="Open pull request").get_attribute("href")
                == task["prUrl"]
            )
            frame()
            task.update(status="SUCCEEDED_UNVERIFIED")
            task["verifyResult"].update(
                verified=False,
                exitCode=None,
                command="",
                summary="Synthetic draft example.",
                warning="No verification command ran; the pull request stays a draft.",
            )
            page.reload()
            page.get_by_text(
                "No verification command ran; the pull request stays a draft.", exact=True
            ).wait_for()
            frame()
            assert not errors, errors
            browser.close()
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-framerate",
                    "1/4",
                    "-i",
                    str(Path(tmp) / "frame-%02d.png"),
                    "-filter_complex",
                    "split[a][b];[a]palettegen[p];[b][p]paletteuse",
                    "-loop",
                    "0",
                    str(ROOT / "assets/console-preview.gif"),
                ],
                check=True,
            )
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    capture()
