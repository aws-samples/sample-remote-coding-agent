"""The GitHub REST calls `nodes/submit.py` makes, over `urllib.request` alone.

No `requests`, no `httpx`, no `gh` CLI: two calls against one host do not earn a
dependency in the image the whole fleet ships (invariant 29), and the standard
library's client is the one thing guaranteed to be there at the pinned Python.

Every request goes through `_request`, which is the only function in this package
that opens a socket. A test fakes GitHub by monkeypatching that one module attribute
and everything else here -- the payload, the already-exists recovery, the scrubbing
-- still runs for real.

The token appears in exactly one place, the `Authorization` header: never in a URL,
never in a log line, and never in an exception message. A traceback out of `submit`
is written to CloudWatch, and a PAT in a log line is a leaked credential that
outlives the task by however long the retention is.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

_API_ROOT = "https://api.github.com"

# Pinned rather than omitted: the header is how GitHub keeps a breaking change out of
# an already-deployed image.
_API_VERSION = "2022-11-28"

# GitHub answers a request with no `User-Agent` with a 403, which reads as a bad token
# and sends you looking in entirely the wrong place.
_USER_AGENT = "agentcore-coding-agent"

# A timeout on every call. An unbounded `urlopen` in the submit node is a task that
# hangs with its branch pushed and no pull request until AgentCore reaps the session
# at 15 minutes, with nothing in the log saying what it was waiting for.
_TIMEOUT_SECONDS = 30

# A response body is only ever read by a human, out of a log line or a status row, so
# it is bounded here rather than at each of those readers.
_MAX_BODY = 2_000

# What a 422 says when the branch already has an open pull request. Matched on the
# text because the status carries no distinguishing code (see `_says_already_exists`).
_ALREADY_EXISTS = "already exists"

# A decoded body: GitHub answers with an object for one pull request, an array for a
# list of them.
_JsonBody = dict[str, Any] | list[Any]


class GitHubError(RuntimeError):
    """A GitHub API call that did not do what `submit` asked of it.

    Carries the status and the response body, because on this endpoint the body is
    the only thing that says *why* -- a 422 alone is indistinguishable between "no
    commits between the branches", "no such base branch" and "already open".

    Every construction site passes text that has already been through `_scrub`: the
    body came back from a request whose header held a PAT, and the reporting path for
    this exception is a CloudWatch log.
    """

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        self.status = status
        self.body = body
        detail = f" (HTTP {status})" if status is not None else ""
        super().__init__(f"{message}{detail}: {body}" if body else f"{message}{detail}")


def create_pull_request(
    repository: str,
    *,
    head: str,
    base: str,
    title: str,
    body: str,
    draft: bool,
    token: str,
) -> str:
    """Open a pull request on `repository` (`owner/repo`) and return its `html_url`.

    `draft` is decided by the caller from `verifyResult["verified"]` alone and nothing
    here re-derives it: this function is the last node of the graph and must not be
    able to promote an unverified run.
    """
    payload: dict[str, Any] = {
        "title": title,
        "head": head,
        "base": base,
        "body": body,
        "draft": draft,
    }
    log.info(
        "opening a %spull request on %s: %s -> %s",
        "draft " if draft else "",
        repository,
        head,
        base,
    )
    status, decoded = _request(
        "POST", f"{_API_ROOT}/repos/{repository}/pulls", token=token, payload=payload
    )
    if status in (200, 201):
        html_url = _html_url(decoded)
        if html_url:
            return html_url

    # A 422 saying the pull request already exists is the *resume* path, not a
    # failure: the watchdog restarts a task whose first attempt pushed the branch and
    # opened the PR before it died, and this branch name is derived from the task id,
    # so the second attempt hits its own first attempt's pull request. Recovering it
    # is the difference between a resume that finishes and one that dies at the last
    # node with the work already pushed.
    if status == 422 and _says_already_exists(decoded):
        log.info("a pull request is already open for %s on %s; reusing it", head, repository)
        existing = _open_pull_request_url(repository, head=head, token=token)
        if existing:
            return existing

    raise GitHubError(
        f"could not open a pull request on {repository}",
        status=status,
        body=_scrub(_render(decoded), token),
    )


def _open_pull_request_url(repository: str, *, head: str, token: str) -> str:
    """The `html_url` of the open pull request already opened from `head`, or `""`.

    `head` has to be qualified with the repository owner -- `owner:branch`, not
    `branch`. This is the part of the API people get wrong: an unqualified `head` is
    accepted, returns an empty list, and makes the recovery above look broken rather
    than mis-called. The documented filter is `user:ref`.
    """
    owner = repository.split("/", 1)[0]
    query = urllib.parse.urlencode({"head": f"{owner}:{head}", "state": "open"})
    status, decoded = _request("GET", f"{_API_ROOT}/repos/{repository}/pulls?{query}", token=token)
    if status != 200:
        raise GitHubError(
            f"could not list the open pull requests of {repository}",
            status=status,
            body=_scrub(_render(decoded), token),
        )
    # An empty list means the 422 was about something else; the caller raises with the
    # body that actually explains it, which is more use than anything said here.
    return _html_url(decoded)


def _request(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, _JsonBody]:
    """Perform one API call and return `(status, decoded body)`.

    The single HTTP seam of this module, and deliberately the only one: a test
    monkeypatches `github._request` and no test in the suite needs a socket, a
    credential or a recorded cassette.

    An HTTP error status is *returned* rather than raised, because the 422 recovery
    has to read the body of a failed response and `urllib` otherwise leaves it locked
    inside the `HTTPError`. Only a transport failure raises, since there is no status
    to hand back.
    """
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        # The token lives here and only here. Not in the URL: a query string is logged
        # verbatim by every proxy between this container and the API.
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _API_VERSION,
        "User-Agent": _USER_AGENT,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    # Every caller builds `url` from `_API_ROOT`, so this can only fail on a future one
    # that does not -- which is exactly the caller worth stopping, since `urlopen` honours
    # `file:` and every other scheme `urllib` was built with, and the header above carries
    # the PAT to whatever host the URL names.
    if not url.startswith(f"{_API_ROOT}/"):
        raise GitHubError(f"refusing to request {url}: not under {_API_ROOT}")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # The scheme is `https` and the host is GitHub's, both pinned by the guard above
        # rather than by the convention that every caller builds on `_API_ROOT`.
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # nosec B310 -- scheme and host pinned by the guard above
            return response.status, _decode(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read())
    except (urllib.error.URLError, TimeoutError) as exc:
        # `str(exc)` can quote the URL back, so it is scrubbed like any body even
        # though the token was never in it.
        raise GitHubError(f"{method} {url} failed: {_scrub(str(exc), token)}") from exc


def _decode(raw: bytes) -> _JsonBody:
    """The response body as JSON, or a one-key dict carrying it verbatim.

    A rate limiter or a proxy in front of the API answers with HTML, and a decode
    error there would replace GitHub's own explanation with a `JSONDecodeError`
    pointing at column 1.
    """
    text = raw.decode("utf-8", "replace")
    try:
        decoded = json.loads(text or "null")
    except json.JSONDecodeError:
        return {"message": text}
    return decoded if isinstance(decoded, dict | list) else {"message": text}


def _html_url(decoded: _JsonBody) -> str:
    """The `html_url` of a pull request object, or of the first of a list of them."""
    if isinstance(decoded, list):
        decoded = next((item for item in decoded if isinstance(item, dict)), {})
    url = decoded.get("html_url") if isinstance(decoded, dict) else None
    return str(url) if url else ""


def _says_already_exists(decoded: _JsonBody) -> bool:
    """Whether a 422 body is GitHub saying this pull request is already open.

    Matched on the text because the response carries nothing better: the shape is
    `{"errors": [{"code": "custom", "message": "A pull request already exists for
    owner:branch."}]}` and `custom` is also what a dozen unrelated 422s use. Every
    other 422 from this endpoint (no commits between the branches, an unknown base)
    says something else, so the match is narrow in practice.
    """
    return _ALREADY_EXISTS in _dumps(decoded).lower()


def _dumps(decoded: _JsonBody) -> str:
    """The body as one line. `default=str` because it is diagnostics, never a payload."""
    return json.dumps(decoded, default=str)


def _render(decoded: _JsonBody) -> str:
    """The body as one bounded line, for a log line or an exception message."""
    text = _dumps(decoded)
    if len(text) <= _MAX_BODY:
        return text
    return f"{text[:_MAX_BODY]}...({len(text) - _MAX_BODY} more characters truncated)"


def _scrub(text: str, token: str) -> str:
    """Remove the token from anything about to be logged, raised or returned.

    Used on every path that can surface a body, a URL or a transport error, because
    all three are reachable from a request that carried a PAT in its header. Returns
    `text` untouched for an empty token, so the empty string is never "found" between
    every pair of characters.
    """
    if not token:
        return text
    return text.replace(token, "***")
