"""The console's invariants, asserted from Python because there is no JS test runner.

`web/` is hand-written ES modules on purpose: five views are not worth a toolchain no
other directory in this repository uses. The cost of that choice is that nothing runs the
JavaScript in CI -- a syntax error in `web/js/` passes the whole Python suite -- so every
invariant that matters is a contract over the source text here, the same technique
`tests/test_api_route_contract.py` uses on the handlers.

What each test protects, in one line each:

* **Syntax.** `node --check <path>` is a silent no-op on an ES module: node retries the
  failed CommonJS parse as ESM and exits 0. Only stdin with an explicit type reports the
  error, so that is the spelling used below.
* **One clock.** A board-only refresh timer leaves every other view printing a frozen
  relative time, and teaches each view to grow a clock of its own.
* **Readable refusal.** A `disabled` button fires no click and takes no focus, so a reason
  carried in a tooltip is unreachable by touch and by keyboard alike. Hover is not an
  affordance.
* **Chained timers.** A repeating timer cannot stop itself reliably, so it outlives the
  DOM it was drawing into; a chained one is torn down by its own next tick not firing.
* **No HTML strings.** Every node through `ui.el()` and every string through
  `textContent`, which makes the in-place activity pulse structural rather than a
  convention and removes the XSS class that card titles and activity details would open.
* **One HTTP boundary, one credential store.** `fetch(` in two files, the bearer header in
  one, and session-scoped storage in one line of one file.
* **Hash routing only.** A fragment never reaches a server, which is why
  `infra/stacks/web_stack.py` carries no CloudFront error responses; a `pushState` here
  makes every deep link answer with S3's `AccessDenied` XML instead.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import REPO_ROOT

WEB = REPO_ROOT / "web"
JS = WEB / "js"
INDEX_HTML = WEB / "index.html"
STYLES_CSS = WEB / "styles.css"
CONFIG_EXAMPLE = WEB / "config.example.js"

# The eight modules and the five views the tests below scan. Listed rather than globbed
# so an empty directory (or a module deleted in a refactor) fails here instead of turning
# every parametrised test in this file into a vacuous pass.
MODULES = ("activity", "api", "app", "auth", "configs", "status", "transitions", "ui")
VIEWS = ("board", "card", "register", "sidebar", "task")

# `from "./x.js"` and `import("./views/x.js")`: `app.js` loads the four routed views
# dynamically, so a `from`-only reader would see none of them.
_IMPORT = re.compile(r"""(?:from|import)\s*\(?\s*["'](?P<spec>\.[^"']+)["']""")
# The `disabled` property or attribute, but not `aria-disabled`.
_DISABLED = re.compile(r"(?<!aria-)\bdisabled\s*[:=]")
_SCRIPT_TAG = re.compile(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script>", re.DOTALL)
_HREF_OR_SRC = re.compile(r"""\b(?:href|src)\s*=\s*["'](?P<value>[^"']*)["']""")

_NO_NODE = shutil.which("node") is None


def _js_files() -> list[Path]:
    return sorted(JS.rglob("*.js"))


def _sources() -> dict[str, str]:
    """Every module under `web/js/`, keyed by its path relative to `web/`."""
    return {str(path.relative_to(WEB)): path.read_text() for path in _js_files()}


def _files_containing(token: str) -> set[str]:
    return {name for name, text in _sources().items() if token in text}


def _nested_function_body(source: str, name: str) -> str:
    """The body of a `function name()` nested one level inside a view module.

    Every function in `mountSettings` is indented two spaces, so the first `\\n  }` after
    the declaration is its own closing brace -- nothing inside a body sits at that
    indentation. Sliced rather than parsed because there is no JS parser in this suite.
    """
    start = source.find(f"function {name}(")
    assert start != -1, f"web/js/views/register.js declares no {name}()"
    end = source.find("\n  }", start)
    assert end != -1, f"web/js/views/register.js: {name}() has no closing brace at depth 1"
    return source[start:end]


def _css_rules() -> list[tuple[str, str]]:
    """`(selector, body)` per innermost rule, comments stripped -- the stylesheet's header
    discusses `[aria-disabled="true"]` and `pointer-events: none` in prose, and a reader
    that kept comments would read the explanation as the rule."""
    css = re.sub(r"/\*.*?\*/", "", STYLES_CSS.read_text(), flags=re.DOTALL)
    return [(match[1].strip(), match[2]) for match in re.finditer(r"([^{}]*)\{([^{}]*)\}", css)]


def test_the_console_has_the_modules_these_tests_scan() -> None:
    on_disk = {str(path.relative_to(JS)) for path in _js_files()}
    expected = {f"{name}.js" for name in MODULES} | {f"views/{name}.js" for name in VIEWS}
    assert on_disk == expected, (
        f"web/js/ holds {sorted(on_disk ^ expected)} more or less than this test knows "
        f"about. A new module has to be added here, because every other assertion in this "
        f"file is a sweep over the directory: a file nobody listed is a file whose "
        f"invariants nothing checks, and a directory that went empty would make the whole "
        f"suite pass by describing nothing."
    )


@pytest.mark.skipif(
    _NO_NODE,
    reason=(
        "node is not on PATH, so the JavaScript syntax gate DID NOT RUN. Nothing else in "
        "this Python suite parses web/js/, so a skip here means a syntax error in the "
        "console ships green."
    ),
)
@pytest.mark.parametrize("path", _js_files(), ids=lambda path: str(Path(path).relative_to(JS)))
def test_every_module_parses_as_an_es_module(path: Path) -> None:
    """The file's bytes on **stdin**, with an explicit `--input-type=module`.

    `node --check <path>` exits 0 on a module with a syntax error (verified on v22): the
    CommonJS parse fails, node retries as ESM, and the error is lost. A test written the
    obvious way passes for ever and this gate would be gone without anyone noticing.
    """
    result = subprocess.run(
        ["node", "--input-type=module", "--check"],
        input=path.read_bytes(),
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"{path.relative_to(REPO_ROOT)} is not a parseable ES module:\n"
        f"{result.stderr.decode(errors='replace')}\n"
        f"There is no build step and no JS test runner here, so a browser is the only "
        f"other thing that would ever have told us -- at run time, in front of an operator."
    )


def test_no_module_uses_a_repeating_timer() -> None:
    offenders = _files_containing("setInterval")
    assert not offenders, (
        f"{sorted(offenders)} schedules work with a repeating timer. Every timer in this "
        f"console is a chained setTimeout, because a chained timer can stop itself: that "
        f"is what makes `destroy()` reliable when the router swaps views. A repeating "
        f"timer whose clear is missed keeps drawing into a DOM that is gone, and the only "
        f"symptom is a console that gets slower the longer it is open."
    )


def test_the_poll_and_pulse_constants_are_the_ones_plan_names() -> None:
    activity = (JS / "activity.js").read_text()
    for name, value in (("MIN_POLL_MS", 6000), ("MAX_POLL_MS", 30000), ("PULSE_MS", 5000)):
        match = re.search(rf"^export const {name} = (\d+);", activity, re.MULTILINE)
        assert match and int(match[1]) == value, (
            f"web/js/activity.js: {name} is not {value}. The backoff window (6s to 30s) "
            f"and the 5s activity pulse are the difference between a console that feels "
            f"live and one that bills for a request a second per open tab."
        )
    assert "document.hidden" in activity, (
        "web/js/activity.js never checks `document.hidden`. A background tab that keeps "
        "polling is the whole cost of a polled console: the timer must re-arm without "
        "fetching while the document is hidden, and wake on visibilitychange."
    )


def test_there_is_exactly_one_clock() -> None:
    for spelling in ("Date.parse(", "new Date("):
        offenders = _files_containing(spelling)
        assert offenders == {"js/ui.js"}, (
            f"`{spelling}` appears in {sorted(offenders)}. One clock, in ui.js, that every "
            f"relative timestamp registers with: a board-only refresh timer leaves the "
            f"card, run and settings views printing a number frozen at first render, and "
            f"teaches each view to grow a clock of its own. Both spellings are pinned so "
            f"the rule cannot be evaded by switching constructors."
        )
    ui = (JS / "ui.js").read_text()
    assert "WeakRef" in ui and ".deref()" in ui, (
        "web/js/ui.js's clock must hold its `<time>` nodes weakly and prune on "
        "`deref()` returning undefined -- otherwise the registry is a leak that grows "
        "with every navigation and keeps every torn-down view's DOM alive."
    )
    assert not _files_containing("isConnected"), (
        "the clock prunes on `deref() === undefined`, never on `node.isConnected`. The "
        "first render happens BEFORE the node is in the document, so an isConnected prune "
        "drops every timestamp at the moment it is created and every view prints a number "
        "that never moves."
    )


def test_only_ui_js_may_spell_a_refusal() -> None:
    offenders = _files_containing("aria-disabled")
    assert offenders == {"js/ui.js"}, (
        f"`aria-disabled` appears in {sorted(offenders)}. It belongs to `ui.action` and "
        f"`ui.setRefusal` alone, which is what forces every refusable control through the "
        f"one helper that short-circuits its click to a toast of the reason. A view that "
        f"hand-rolls a dimmed button gets the look and loses the sentence."
    )


def test_no_control_is_ever_disabled() -> None:
    offenders = {name for name, text in _sources().items() if _DISABLED.search(text)}
    assert not offenders, (
        f"{sorted(offenders)} sets the `disabled` property or attribute. A disabled button "
        f"fires no click and takes no focus, so the reason it is refused -- which the "
        f"operator needs more than the refusal itself -- is reachable only by hovering a "
        f"tooltip: not by touch, not by keyboard, not by a screen reader. Refuse with "
        f"`ui.setRefusal(node, reason)` and let the click carry the sentence."
    )


def test_a_refused_control_still_receives_its_click() -> None:
    for selector, body in _css_rules():
        if "aria-disabled" in selector:
            assert "pointer-events" not in body or "none" not in body, (
                f"web/styles.css `{selector}` sets pointer-events: none. That swallows the "
                f"click that carries the reason, which turns every readable refusal back "
                f"into a dead button -- the exact failure the aria-disabled treatment "
                f"exists to avoid. Dim it and change the cursor; let the event through."
            )


def test_no_module_assigns_an_html_string() -> None:
    for spelling in ("innerHTML", "outerHTML", "insertAdjacentHTML"):
        offenders = _files_containing(spelling)
        assert not offenders, (
            f"`{spelling}` appears in {sorted(offenders)}. Every node is built with "
            f"`ui.el()` and every string assigned via textContent, for two reasons: card "
            f"titles, prompts and activity `detail` strings come from a repository and "
            f"would otherwise be an XSS hole, and the activity pulse's in-place update "
            f"only stays in place if nobody can re-render a container from a string."
        )


def test_there_is_one_http_boundary_and_one_credential_module() -> None:
    offenders = _files_containing("fetch(")
    assert offenders == {"js/api.js", "js/auth.js"}, (
        f"`fetch(` appears in {sorted(offenders)}. `api.js` is the only control-plane HTTP "
        f"boundary: it is where the bearer token, the error envelope and the single 401 "
        f"retry live, and a view that fetches directly gets none of them. `auth.js` is the "
        f"one exception and is scoped deliberately -- a PKCE code exchange with Cognito is "
        f"not an API call and must not pass through the API's own bearer plumbing."
    )
    offenders = _files_containing("Authorization")
    assert offenders == {"js/api.js"}, (
        f"the bearer header is spelled in {sorted(offenders)}. One place attaches a "
        f"credential to a request, so there is one place to audit when the token type "
        f"changes -- the authorizer validates the ID token, not the access token, and that "
        f"is a one-line mistake to make twice."
    )


def test_the_token_store_is_session_scoped_and_named_once() -> None:
    assert not _files_containing("localStorage"), (
        "a token in localStorage outlives the browser session and every tab, which is a "
        "credential left on a shared machine. PLAN Tier 1b: closing the browser signs you "
        "out, and that is the intended trade."
    )
    occurrences = [
        (name, text.count("sessionStorage"))
        for name, text in _sources().items()
        if "sessionStorage" in text
    ]
    assert occurrences == [("js/auth.js", 1)], (
        f"session storage is named at {occurrences}. Exactly once, in auth.js's "
        f"`const tokenStore = sessionStorage`, so the storage choice is greppable in one "
        f"line -- a second reference is a second place to change when it moves, and the "
        f"one that gets forgotten is the one that keeps the token."
    )


def test_the_confirm_panel_puts_cancel_before_the_danger_button() -> None:
    ui = (JS / "ui.js").read_text()
    match = re.search(r"export const CONFIRM_ORDER = \[(?P<items>[^\]]*)\];", ui)
    assert match, "web/js/ui.js declares no CONFIRM_ORDER"
    assert re.findall(r'"([a-z]+)"', match["items"]) == ["prompt", "cancel", "danger"], (
        "CONFIRM_ORDER is the DOM order of the confirm panel, and therefore the tab order "
        "and which button takes the initial focus. Cancel comes before the danger button "
        "so a stray Enter on an unread panel cancels. The builder maps over this constant "
        "precisely so a later refactor cannot reorder the panel without touching it."
    )


def test_the_settings_view_reports_whether_a_repository_has_memory() -> None:
    """The Memory row, and the signature that lets it redraw.

    `renderFacts` is only re-run when `signature()` changes -- that gating is what keeps an
    armed Deregister button and the operator's focus alive across a six-second poll. So a
    row added to the grid without its field added to the signature is drawn once, before
    the memory exists, and says "none" for ever.
    """
    register = (JS / "views/register.js").read_text()
    facts = _nested_function_body(register, "renderFacts")
    signature = _nested_function_body(register, "signature")

    assert "Memory" in facts and "memoryId" in facts, (
        "web/js/views/register.js renderFacts() draws no Memory row. `memoryId` is on the "
        "configuration when the provisioner made one and absent when it did not, and an "
        "operator looking at a settings page has no other way to find out which."
    )
    assert "memoryId" in signature, (
        "web/js/views/register.js signature() does not include config.memoryId, so the "
        "facts grid is not redrawn when a memory id appears. The panel is rebuilt only on "
        "a signature change -- deliberately, so a poll cannot disarm Deregister -- which "
        "means the Memory row would stay at whatever it said before provisioning finished."
    )
    assert "none" in facts, (
        "the absent arm has to render a sentence, not `undefined`: a repository registered "
        "before memory existed will never have one, and that is the common case."
    )


def test_the_armed_button_cannot_be_satisfied_by_one_double_click() -> None:
    ui = (JS / "ui.js").read_text()
    grace = re.search(r"export const ARM_GRACE_MS = (\d+);", ui)
    disarm = re.search(r"export const DISARM_MS = (\d+);", ui)
    assert grace and disarm, "web/js/ui.js declares no ARM_GRACE_MS / DISARM_MS"
    assert 300 <= int(grace[1]) <= 1500, (
        f"ARM_GRACE_MS is {grace[1]}. Destructive confirmation is two clicks on one "
        f"button, so there has to be a window after arming in which the second click is "
        f"ignored -- otherwise one physical double-click deletes a card and the "
        f"confirmation was never there. Too long and the second click feels broken."
    )
    assert int(disarm[1]) >= 3000, (
        f"DISARM_MS is {disarm[1]}. An armed button that stays armed is a delete waiting "
        f"for an unrelated click; one that disarms too fast makes a deliberate second "
        f"click re-arm instead of commit."
    )
    assert "data-armed" in ui, "the armed state must be an attribute the stylesheet can see"
    assert ".focus()" in ui, (
        "nothing in web/js/ui.js calls .focus(). The confirm panel has to move focus to "
        "Cancel on open, or a keyboard operator's next Enter lands on whatever had focus "
        "before -- which may well be the control that opened the panel."
    )


def test_every_view_is_reachable_from_the_router() -> None:
    referenced = set(_IMPORT.findall((JS / "app.js").read_text()))
    for view in VIEWS:
        assert f"./views/{view}.js" in referenced, (
            f"web/js/app.js never references views/{view}.js. A view module nothing imports "
            f"is a route that renders nothing: the hash resolves, `destroy` runs on the "
            f"previous view, and the main region stays empty with no error anywhere. The "
            f"four routed views are loaded with a dynamic import(), so this reads both "
            f"spellings."
        )


def test_the_router_is_hash_only_so_cloudfront_never_sees_a_deep_link() -> None:
    """The premise `infra/stacks/web_stack.py` spends its error-response comment on.

    A fragment is never sent to a server, so the only paths the distribution is ever asked
    for are the real objects, `/api/*`, and `/` -- which `default_root_object` covers.
    History routing breaks that silently: nothing here fails, and every deep link starts
    answering from S3 instead.
    """
    offenders = _files_containing("pushState")
    assert not offenders, (
        f"{sorted(offenders)} pushes a history entry, so the console now has path-based "
        f"routes. A path is sent to CloudFront, and `infra/stacks/web_stack.py` deliberately "
        f"carries no `error_responses` because a hash router gives it nothing to rewrite: "
        f"every one of those deep links answers with S3's "
        f"<Error><Code>AccessDenied</Code></Error> XML, on a reload or a shared URL rather "
        f"than on the navigation that was tested. Restore the rewrite -- as a CloudFront "
        f"Function on the default behaviour alone, because a distribution-level "
        f"`CustomErrorResponses` would also turn /api/*'s error envelopes into 200 "
        f"text/html -- in the same change that adds the route."
    )
    offenders = _files_containing("replaceState")
    assert offenders == {"js/auth.js"}, (
        f"`replaceState` appears in {sorted(offenders)}. The one permitted use is auth.js "
        f"stripping the OAuth `?code=` out of the address bar, which rewrites the query and "
        f"leaves `location.pathname` exactly as it was. Anywhere else it is a path-based "
        f"route wearing a quieter name, and it costs the same thing: `web_stack.py` has no "
        f"`error_responses` to catch the path, so the deep link is S3's "
        f"<Error><Code>AccessDenied</Code></Error> XML."
    )


@pytest.mark.parametrize("path", _js_files(), ids=lambda path: str(Path(path).relative_to(JS)))
def test_every_relative_import_resolves_to_a_file(path: Path) -> None:
    for spec in _IMPORT.findall(path.read_text()):
        target = (path.parent / spec).resolve()
        assert target.is_file(), (
            f"{path.relative_to(REPO_ROOT)} imports `{spec}`, which does not exist. There "
            f"is no bundler to resolve or complain: the browser requests the URL, gets a "
            f"404, and the module graph fails to load -- a blank view, discovered by an "
            f"operator. Extensions are mandatory in a browser; `./x` is not `./x.js`."
        )


def test_the_markup_is_compatible_with_a_no_inline_content_security_policy() -> None:
    html = INDEX_HTML.read_text()
    scripts = _SCRIPT_TAG.findall(html)
    assert scripts, "web/index.html loads no script at all"
    for attrs, body in scripts:
        assert "src=" in attrs and not body.strip(), (
            f"web/index.html has an inline <script>: {body.strip()[:60]!r}. CloudFront's "
            f"response-headers policy sets `script-src 'self'` with no inline allowance, so "
            f"an inline script does not run in the deployed console -- and adding "
            f"'unsafe-inline' to make it work re-opens the hole the policy closed."
        )
    assert "<style" not in html, (
        "web/index.html carries an inline <style>. Same policy, same argument: one "
        "stylesheet, loaded by URL."
    )
    assert "http://" not in html, "web/index.html names an insecure URL"
    for value in _HREF_OR_SRC.findall(html):
        assert value.startswith(("./", "#")), (
            f"web/index.html points at `{value}`. Every asset is relative and same-origin: "
            f"a CDN URL is a third party that can change the console's code, and "
            f"`connect-src 'self'` would block it anyway."
        )
    assert '<script src="./config.js"></script>' in html, (
        "web/index.html must load ./config.js as a classic script, before the module "
        "graph: `window.RCA_CONFIG` has to exist by the time app.js reads it."
    )
    assert '<script type="module" src="./js/app.js"></script>' in html, (
        "web/index.html must load ./js/app.js as a module -- there is no bundle, so the "
        "browser's own module loader is the build step."
    )


def test_the_deployed_configuration_is_not_in_the_tree() -> None:
    assert "config.js" in (WEB / ".gitignore").read_text().split(), (
        "web/.gitignore must ignore config.js. The rule lives there rather than in the "
        "root .gitignore because the root is shared: a one-line tidy-up in a file two "
        "other cards are editing is a hand-resolved merge conflict for no gain."
    )
    # Deliberately NOT `assert not (WEB / "config.js").exists()`. `make deploy` writes that
    # file into the tree by design -- the Makefile calls it the one exception and
    # `web/.gitignore` covers it -- so asserting absence made `make test` fail on any
    # checkout that had actually deployed. Since `make test` is this repository's own
    # `verifyCommand`, that turned a successful deploy into a red suite for the agent working
    # on this repo, which is a self-inflicted version of exactly the confusion invariant 35
    # exists to prevent.
    #
    # The hazard in the docstring is a *committed* copy, not a present one, so that is what
    # is asserted: ignored (above) and untracked (below). Both are checked because they fail
    # differently -- a file already tracked stays tracked no matter what .gitignore says.
    if shutil.which("git") and (REPO_ROOT / ".git").exists():
        # The `safe.directory` exemption is passed explicitly, in git's own environment
        # channel, rather than relied on: this checkout can be owned by another uid (a
        # mounted workspace), git then refuses every command with `dubious ownership`
        # (exit 128), and `tests/test_git_ops.py` strips `GIT_CONFIG_*` out of the
        # environment its control run inherits. Neither is evidence about the ignore rule.
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", "web/config.js"],
            cwd=REPO_ROOT,
            env=os.environ
            | {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(REPO_ROOT),
            },
            check=False,
        )
        assert ignored.returncode == 0, (
            f"git does not ignore web/config.js (check-ignore exited "
            f"{ignored.returncode}; 1 means not ignored). The .gitignore line above says "
            f"it should, so something -- a negation elsewhere, a file already tracked -- is "
            f"overriding it, and the next `git add -A` after a deploy commits a live "
            f"deployment's identifiers."
        )
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "web/config.js"],
            cwd=REPO_ROOT,
            env=os.environ
            | {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(REPO_ROOT),
            },
            capture_output=True,
            check=False,
        )
        assert tracked.returncode != 0, (
            "web/config.js is TRACKED by git. It names one live account's user pool, app "
            "client and hosted-UI domain, so a committed copy points every checkout at that "
            "one deployment and then goes stale in silence. `git rm --cached web/config.js`."
        )


def test_the_example_configuration_names_no_real_deployment() -> None:
    assert CONFIG_EXAMPLE.exists(), (
        "web/config.example.js is the only committed description of the window.RCA_CONFIG "
        "contract `make deploy` has to satisfy; without it the deploy target and the "
        "console agree by memory."
    )
    example = CONFIG_EXAMPLE.read_text()
    assert "amazoncognito.com" not in example, (
        "web/config.example.js carries a real hosted-UI domain. An example that is a copy "
        "of somebody's deployment is the committed config.js this repository refuses, "
        "wearing a different name."
    )
    assert not re.search(r"\b\d{12}\b", example), (
        "web/config.example.js contains a 12-digit number: an AWS account id."
    )
    assert not re.search(r"\b[a-z]{2}-[a-z]+-\d_[A-Za-z0-9]{6,}\b", example), (
        "web/config.example.js contains something shaped like a real user pool id "
        "(`<region>_<id>`). Placeholders only."
    )


def test_the_register_view_links_to_the_token_page_and_names_the_repository() -> None:
    """The register view builds a GitHub token-page link from whatever is typed above it.

    This is the console's answer to the step that actually confuses people. The path is
    Settings -> Developer settings -> Personal access tokens -> Fine-grained, four levels
    deep, and it is where the *wrong kind* of token gets chosen -- a classic one, which
    `configurations._require_supported_token` then refuses. A link removes the navigation and
    the guess.

    Asserted as text over the module rather than by executing it, like every other contract in
    this file: there is no DOM here. What is pinned is the shape a reviewer would otherwise
    have to take on trust -- that the link is built from the repository field, that it cannot
    pre-select the permissions (GitHub documents no query parameters for that page, so the two
    are named beside it instead), and that it opens with `noopener`, because it opens a page
    where a credential is about to be created.
    """
    source = (WEB / "js" / "views" / "register.js").read_text(encoding="utf-8")

    assert "https://github.com/settings/personal-access-tokens/new" in source, (
        "register.js must link to the fine-grained token page. Describing the path in prose is "
        "what it replaced."
    )
    assert "repositorySlug" in source and "tokenPageUrl" in source, (
        "the link's target is derived from the repository field, so both helpers must exist; a "
        "hard-coded href would stop naming the operator's repository."
    )
    assert 'urlField.addEventListener("input"' in source, (
        "the link has to track the repository field as it is typed, or it names the wrong "
        "repository for anyone who edits the URL after first render."
    )
    assert "noopener" in source, (
        "the token link opens a page where a credential is created, so it must carry rel=noopener."
    )
    # The two permissions are named in the view because a URL cannot select them.
    for permission in ("Contents: Read and write", "Pull requests: Read and write"):
        assert permission in source, (
            f"register.js must name {permission!r} beside the link. GitHub documents no query "
            "parameters for the fine-grained token page -- the unofficial `?scopes=` works only "
            "on the *classic* page -- so the permissions cannot be pre-selected and have to be "
            "spelled out where the operator is looking."
        )
    assert "Only select repositories" in source, (
        "the repository-access choice is the one that decides whether a leaked token reaches "
        "one repository or all of them, so the view names it."
    )
