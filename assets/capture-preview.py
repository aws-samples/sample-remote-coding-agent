"""Capture the actual console with synthetic API responses; never contact AWS/GitHub.

Run from the repo root: uv run --with playwright==1.58.0 assets/capture-preview.py
Requires Chrome and ffmpeg. No production code or credentials are modified.
"""

import functools
import http.server
import json
import shutil
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
TITLE = "Add CSV export"
PROMPT = TITLE + (
    "\nAdd a Download CSV button to Expenses."
    "\nExport date, category, and amount."
    "\nTest CSV escaping and empty exports."
)
CONFIG = {"configId": "cfg_preview", "repository": "example/expense-tracker", "status": "READY"}
RESULT = "Synthetic example: CSV escaping and empty-export tests pass."
STAGES = ["PLANNING", "CODING", "VERIFYING", "REVIEWING", "SUBMITTING", "SUCCEEDED"]
COLUMNS = ["PLANNING", "CODING", "VERIFYING", "VERIFYING", "VERIFYING", "DONE"]
STEPS = ["checkout", "plan", "code", "verify", "review", "submit"]


def capture():  # noqa: C901 - Keep the bounded fixture and its linear capture sequence together.
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT / "web")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    card = {
        "cardId": "card_preview",
        "configId": CONFIG["configId"],
        "title": TITLE,
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
    created = False

    def route_request(route):
        nonlocal created
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
            if route.request.method == "POST":
                assert not created
                assert route.request.post_data_json == {
                    "configId": CONFIG["configId"],
                    "description": PROMPT,
                }
                created = True
                payload = card
            else:
                payload = {"cards": [card] if created else []}
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
                    label.id = 'preview-label';
                    label.textContent = 'UI PREVIEW · SYNTHETIC DATA · NOT A LIVE AWS RUN';
                    label.style.cssText = 'position:fixed;bottom:0;left:0;right:0;z-index:9999;background:#232f3e;color:white;padding:12px;text-align:center;font:16px Arial';
                    document.body.append(label);
                });
            """)
            page.goto(origin + "/#/boards/cfg_preview")
            composer = page.get_by_role("textbox", name="New card", exact=True)
            composer.wait_for()
            assert page.locator(".card").count() == 0
            frames = []

            def frame(seconds=4):
                dest = Path(tmp) / f"frame-{len(frames):02}.png"
                page.screenshot(path=str(dest))
                frames.append(dest)
                for _ in range(round(seconds * 10) - 1):
                    duplicate = Path(tmp) / f"frame-{len(frames):02}.png"
                    shutil.copyfile(dest, duplicate)
                    frames.append(duplicate)

            def slate(title, detail):
                page.evaluate(
                    """({title, detail}) => {
                    const cover = document.createElement('div');
                    cover.id = 'preview-slate';
                    cover.style.cssText = 'position:fixed;inset:0 0 42px;z-index:9998;background:#f6f7fa;display:flex;flex-direction:column;align-items:center;justify-content:center;color:#232f3e;font:24px Arial';
                    const heading = document.createElement('h1');
                    heading.textContent = title;
                    const text = document.createElement('p');
                    text.textContent = detail;
                    cover.append(heading, text);
                    document.body.append(cover);
                }""",
                    {"title": title, "detail": detail},
                )

            slate("Create a coding task", "Type the title and description · Add card · Start run")
            frame(2)
            page.locator("#preview-slate").evaluate("(node) => node.remove()")
            frame(1)
            # Use the textarea's native resize handle so title and description stay visible.
            bounds = composer.bounding_box()
            page.mouse.move(bounds["x"] + bounds["width"] - 3, bounds["y"] + bounds["height"] - 3)
            page.mouse.down()
            page.mouse.move(
                bounds["x"] + bounds["width"] - 3, bounds["y"] + bounds["height"] + 170, steps=8
            )
            page.mouse.up()
            composer.focus()
            for start in range(0, len(PROMPT), 2):
                composer.press_sequentially(PROMPT[start : start + 2])
                frame(0.1)
            assert composer.input_value() == PROMPT
            frame(2)
            page.get_by_role("button", name="Add card", exact=True).click()
            page.get_by_role("button", name="Start run", exact=True).wait_for()
            assert created and composer.input_value() == ""
            assert page.locator(".card-title").inner_text() == TITLE
            frame(2)
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
                "summary": RESULT,
                "vetoed": False,
            }
            # A reserved example domain prevents a fixture from implying an actual published PR.
            task["prUrl"] = "https://example.com/preview/pull/1"
            page.goto(origin + "/#/tasks/cfg_preview/task_preview")
            page.get_by_text(RESULT, exact=True).wait_for()
            assert (
                page.get_by_role("link", name="Open pull request").get_attribute("href")
                == task["prUrl"]
            )
            frame()
            slate(
                "Task complete",
                "The next loop creates a new example. It does not move this task back to Backlog.",
            )
            frame(5)
            page.locator("#preview-slate").evaluate("(node) => node.remove()")
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
            page.screenshot(path=str(ROOT / "assets/console-draft.png"))
            assert not errors, errors
            browser.close()
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-loglevel",
                    "error",
                    "-framerate",
                    "10",
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
