"""Does this GitHub token actually have write access to this repository?

Asked over `urllib.request` alone: two hosts' worth of calls against one API do not earn
a dependency in a bundle the whole control plane ships (invariant 29), and
`agent/src/coding_agent/github.py` -- the same answer for the agent image -- is in the
agent package and not importable from a Lambda.

**Read-only checks are not evidence.** Selecting a repository grants `Metadata: read`
automatically, so `GET /repos/{owner}/{repo}` answers 200 for a token with no `Contents`
permission at all, and `GET .../git/ref/heads/{branch}` and `GET .../contents/{path}`
need only `Contents: read`. A token was declared good three times on those checks and
still failed the push. So `check_can_push` does the thing that settled it: it creates a
throwaway git ref and deletes it again, and on a refusal it reports GitHub's own
sentence plus the `x-accepted-github-permissions` header, which states exactly which
permission the call wanted.

Every request goes through `_request`, which is the only function in this module that
opens a socket. A test fakes GitHub by monkeypatching that one module attribute, and
everything else here -- the payload, the decoding, the scrubbing -- still runs for real.

Two rules this module keeps deliberately:

* **It imports nothing from `_common`.** `test_api_fakes.load_handlers` reloads the
  handler modules inside `mock_aws` and `importlib.reload(_common)` builds a *new*
  `ApiError` class, so a module left holding the old one raises an exception the freshly
  decorated `@handler` does not catch. This module is not on that reload list -- which is
  what lets one autouse fixture patch `_request` for a whole test file -- so it must hold
  no `_common` symbol: its own two exceptions, its own logger (by name, not by import),
  its own `_scrub`. `configurations.py` is the only place a probe outcome becomes an
  `ApiError`.
* **A failure to reach a verdict is the caller's cue to degrade, not to refuse.**
  `PushDenied` means GitHub answered and the answer was no; `ProbeUnavailable` means the
  probe never found out. A control plane that cannot register a repository while GitHub
  has an incident is worse than one that finds out late.

The token appears in exactly one place, the `Authorization` header: never in a URL,
never in a log line, never in an exception message.
"""

from __future__ import annotations

import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol

# The same logger `_common` configures, taken by name rather than imported -- see the
# `_common` rule above.
LOG = logging.getLogger("handlers")

_API_ROOT = "https://api.github.com"

# Pinned rather than omitted: the header is how GitHub keeps a breaking change out of an
# already-deployed bundle.
_API_VERSION = "2022-11-28"

# GitHub answers a request with no `User-Agent` with a 403, which reads as a bad token
# and sends you looking in entirely the wrong place.
_USER_AGENT = "rca-control-plane"

# Short and explicit. Three calls, so a 12-second worst case against the 30 seconds
# `ApiStack` gives every non-GET route -- which is what keeps this change out of the
# infra stack.
_TIMEOUT_SECONDS = 4.0

# GitHub's own statement of which permission a call wanted. Public because
# `configurations.py` puts it in the 400: on the refusal this module exists for it reads
# `contents=write`, which is the sentence that ends the argument.
ACCEPTED_PERMISSIONS_HEADER = "x-accepted-github-permissions"

# Prefixed so the `create`/`delete` webhook events this probe fires are identifiable,
# and suffixed with random hex so the ref cannot collide with a real branch.
_PROBE_REF_PREFIX = "rca-write-probe-"

# A bound on anything that reaches a response body or a log line.
_MAX_MESSAGE = 500

# The statuses that mean the throwaway ref is gone: deleted, already absent, or never
# created.
_DELETED = frozenset({200, 204, 404, 422})


class PushDenied(Exception):  # noqa: N818 -- the name is the verdict, not an "Error"
    """GitHub answered, and the answer was "this token cannot push here".

    Carries GitHub's own text rather than a diagnosis of our own: passing the vendor's
    accurate sentence straight through is the whole point of the probe. `status` and
    `accepted_permissions` go into the 400 beside it, because "403" and
    `contents=write` together say what the message alone does not.

    **A registration failure.**
    """

    def __init__(self, github_message: str, *, status: int, accepted_permissions: str = "") -> None:
        self.github_message = github_message
        self.status = status
        self.accepted_permissions = accepted_permissions
        super().__init__(f"HTTP {status}: {github_message}")


class ProbeUnavailable(Exception):  # noqa: N818 -- the name is the verdict, not an "Error"
    """The probe could not reach a verdict.

    A transport error, a timeout, a 5xx, an empty repository with no commit to branch
    from, or any status the probe does not recognise. **Not a registration failure** --
    the caller stores the token with a warning, because refusing to register a
    repository during a GitHub incident is the worse outcome.
    """


def check_can_push(repository: str, *, token: str) -> None:
    """Return `None` once a push to `repository` (`owner/repo`) has been *proved*.

    Raises `PushDenied` or `ProbeUnavailable` otherwise, and returns for no other
    reason: the caller has two distinct actions to take and a boolean would make it
    guess which.

    Three calls. `GET /commits?per_page=1` for a SHA to branch from -- one call rather
    than the two `GET /repos/{o}/{r}` for `default_branch` then
    `GET .../git/ref/heads/{branch}` would take, and a 200 also confirms the repository
    is visible to this token. Then `POST /git/refs`, which needs `Contents: write` and
    nothing weaker, and is therefore the call that decides. Then `DELETE` of the same
    ref, best effort.
    """
    query = urllib.parse.urlencode({"per_page": 1})
    status, decoded, headers = _request(
        "GET", f"{_API_ROOT}/repos/{repository}/commits?{query}", token=token
    )
    if status in (401, 403, 404):
        # Bad credentials, or a repository this token cannot see at all. Either way the
        # operator has to change the token, so it is a refusal rather than a degrade.
        raise PushDenied(
            _github_message(decoded),
            status=status,
            accepted_permissions=_accepted_permissions(headers),
        )
    if status != 200:
        # Includes GitHub's 409 "Git Repository is empty": there is no commit to branch
        # from, so write capability is unprovable here rather than absent. A 5xx and any
        # unrecognised status land here too.
        raise ProbeUnavailable(
            f"HTTP {status} listing the commits of {repository}: {_github_message(decoded)}"
        )
    sha = _first_sha(decoded)
    if not sha:
        raise ProbeUnavailable(f"the commit list of {repository} carried no sha to branch from")

    ref_name = _probe_ref_name()
    status, decoded, headers = _request(
        "POST",
        f"{_API_ROOT}/repos/{repository}/git/refs",
        token=token,
        payload={"ref": f"refs/heads/{ref_name}", "sha": sha},
    )
    if status in (403, 404):
        # The case the read checks miss, and the one this module exists for. The header
        # reads `contents=write` here, which is GitHub naming the missing permission.
        raise PushDenied(
            _github_message(decoded),
            status=status,
            accepted_permissions=_accepted_permissions(headers),
        )
    if status == 422:
        # GitHub accepted the permission and rejected the payload -- still proof of
        # `Contents: write`, and there is no ref to clean up.
        LOG.info(
            "the write probe on %s was accepted and the ref refused (422); treating that as proved",
            repository,
        )
        return
    if status != 201:
        raise ProbeUnavailable(
            f"HTTP {status} creating a ref on {repository}: {_github_message(decoded)}"
        )

    _delete_probe_ref(repository, ref_name, token=token)


def _delete_probe_ref(repository: str, ref_name: str, *, token: str) -> None:
    """Remove the throwaway ref. Best effort, and never raises.

    The push is already proved by the time this runs, so a failure here is a stray ref
    and not a reason to refuse a registration. The warning names the ref so an operator
    can delete it by hand.
    """
    try:
        status = _request(
            "DELETE", f"{_API_ROOT}/repos/{repository}/git/refs/heads/{ref_name}", token=token
        )[0]
    except ProbeUnavailable as exc:
        LOG.warning("could not delete the write-probe ref %s on %s: %s", ref_name, repository, exc)
        return
    if status not in _DELETED:
        LOG.warning(
            "the write-probe ref %s on %s was not deleted (HTTP %s); it can be removed by hand",
            ref_name,
            repository,
            status,
        )


class _Headers(Protocol):
    """The case-insensitive `get` this module needs from a response's headers."""

    def get(self, name: str, /) -> str | None: ...


def _request(
    method: str,
    url: str,
    *,
    token: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, Any, _Headers]:
    """Perform one API call and return `(status, decoded body, response headers)`.

    The single HTTP seam of this module, and deliberately the only one: a test
    monkeypatches `github_probe._request` and no test in the suite needs a socket, a
    credential or a recorded cassette.

    An HTTP error status is *returned* rather than raised, because the refusal path has
    to read the body -- and the headers -- of a failed response, and `urllib` otherwise
    leaves both locked inside the `HTTPError`. Only a transport failure raises, since
    there is no status to hand back. `urllib`'s headers are already case-insensitive on
    both the response and the error, so nothing here normalises them.
    """
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        # The token lives here and only here. Not in the URL: a query string is logged
        # verbatim by every proxy between this Lambda and the API.
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _API_VERSION,
        "User-Agent": _USER_AGENT,
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    # Every caller builds `url` from `_API_ROOT`, so this can only fail on a future one that
    # does not -- which is exactly the caller worth stopping, since `urlopen` honours `file:`
    # and every other scheme `urllib` was built with, and the header above carries the
    # operator's PAT to whatever host the URL names. `ProbeUnavailable` rather than
    # `PushDenied`: nothing was asked of GitHub, so nothing was refused.
    if not url.startswith(f"{_API_ROOT}/"):
        raise ProbeUnavailable(f"refusing to request {url}: not under {_API_ROOT}")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        # The scheme is `https` and the host is GitHub's, both pinned by the guard above
        # rather than by the convention that every caller builds on `_API_ROOT`.
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:  # nosec B310 -- scheme and host pinned by the guard above
            return response.status, _decode(response.read()), response.headers
    except urllib.error.HTTPError as exc:
        return exc.code, _decode(exc.read()), exc.headers
    except (urllib.error.URLError, TimeoutError) as exc:
        # `str(exc)` can quote the URL back, so it is scrubbed like any body even though
        # the token was never in it.
        raise ProbeUnavailable(f"{method} {url} failed: {_scrub(str(exc), token)}") from exc


def _decode(raw: bytes) -> Any:
    """The response body as JSON, or a one-key dict carrying it verbatim.

    A rate limiter or a proxy in front of the API answers with HTML, and a decode error
    there would replace GitHub's own explanation with a `JSONDecodeError` pointing at
    column 1. The empty body a `204` carries decodes to `None`, which every reader here
    tolerates.
    """
    text = raw.decode("utf-8", "replace")
    try:
        decoded = json.loads(text or "null")
    except json.JSONDecodeError:
        return {"message": text}
    return decoded if isinstance(decoded, dict | list | type(None)) else {"message": text}


def _first_sha(decoded: Any) -> str:
    """The `sha` of the first commit in a `GET /commits` body, or `""`."""
    if not isinstance(decoded, list):
        return ""
    first = next((item for item in decoded if isinstance(item, Mapping)), {})
    sha = first.get("sha")
    return str(sha) if sha else ""


def _github_message(decoded: Any) -> str:
    """GitHub's own sentence about a response, bounded.

    The top-level `message` plus any `errors[].message`, because on `git/refs` the
    top-level text is generic and the array is where the specific reason lives. This is
    the value that becomes the operator-facing 400: reporting it verbatim, immediately,
    is what this whole probe buys over finding out inside a container 25 minutes later.
    """
    if not isinstance(decoded, Mapping):
        return ""
    parts = []
    top = decoded.get("message")
    if isinstance(top, str) and top:
        parts.append(top)
    errors = decoded.get("errors")
    if isinstance(errors, list):
        parts.extend(
            error["message"]
            for error in errors
            if isinstance(error, Mapping)
            and isinstance(error.get("message"), str)
            and error["message"]
        )
    text = "; ".join(parts)
    if len(text) <= _MAX_MESSAGE:
        return text
    return f"{text[:_MAX_MESSAGE]}...({len(text) - _MAX_MESSAGE} more characters truncated)"


def _accepted_permissions(headers: _Headers) -> str:
    """`x-accepted-github-permissions`, or `""`. See `ACCEPTED_PERMISSIONS_HEADER`."""
    return str(headers.get(ACCEPTED_PERMISSIONS_HEADER) or "")


def _scrub(text: str, token: str) -> str:
    """Remove the token from anything about to be logged, raised or returned.

    Returns `text` untouched for an empty token, so the empty string is never "found"
    between every pair of characters.
    """
    if not token:
        return text
    return text.replace(token, "***")


def _probe_ref_name() -> str:
    return f"{_PROBE_REF_PREFIX}{secrets.token_hex(6)}"
