"""The pull-request call, through the one HTTP seam `github.py` exposes.

`github._request` is the only function in that module that opens a socket, so every test
here replaces that one attribute and everything else -- the payload, the 422 recovery, the
scrubbing, the body decoding -- still runs for real. No socket, no credential, no cassette.

Two of these protect things whose failure is silent. A PAT that survives into an exception
message is a leaked credential sitting in CloudWatch for the retention period, and a 422
recovery that does not recover is a resumed task that dies at the last node with the work
already pushed and no pull request to show for it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from coding_agent import github
from conftest import FAKE_PAT

_TOKEN = FAKE_PAT
_ALREADY_OPEN = {
    "errors": [{"code": "custom", "message": "A pull request already exists for owner:agent/t-1."}]
}


class _FakeGitHub:
    """A recording stand-in for `github._request`, driven by a queue of responses."""

    def __init__(self, *responses: tuple[int, Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, method: str, url: str, *, token: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, Any]:
        self.calls.append({"method": method, "url": url, "token": token, "payload": payload})
        return self.responses.pop(0) if self.responses else (500, {"message": "no more"})


def _open(fake: _FakeGitHub, monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> str:
    monkeypatch.setattr(github, "_request", fake)
    kwargs: dict[str, Any] = {
        "head": "agent/t-1",
        "base": "main",
        "title": "add a thing",
        "body": "## Verification\n\n- Command: `make test`\n",
        "draft": True,
        "token": _TOKEN,
    }
    return github.create_pull_request("owner/repo", **(kwargs | overrides))


def test_the_payload_is_what_github_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    """`draft` is passed through from the caller and never re-derived here.

    This function is the last node of the graph, and a `draft` it computed itself would be
    a second place able to promote an unverified run to a non-draft pull request.
    """
    fake = _FakeGitHub((201, {"html_url": "https://github.com/owner/repo/pull/7"}))

    url = _open(fake, monkeypatch)

    assert url == "https://github.com/owner/repo/pull/7"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://api.github.com/repos/owner/repo/pulls"
    assert call["payload"] == {
        "title": "add a thing",
        "head": "agent/t-1",
        "base": "main",
        "body": "## Verification\n\n- Command: `make test`\n",
        "draft": True,
    }
    # Passed as an argument, exactly as `submit` passes it -- never read from the
    # environment, which every coding subagent's `Bash` inherits.
    assert call["token"] == _TOKEN


@pytest.mark.parametrize("draft", [True, False])
def test_the_draft_flag_is_carried_through_untouched(
    draft: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Neither value is second-guessed on the way to the API."""
    fake = _FakeGitHub((201, {"html_url": "https://github.com/owner/repo/pull/1"}))
    _open(fake, monkeypatch, draft=draft)
    assert fake.calls[0]["payload"]["draft"] is draft


def test_an_already_open_pull_request_is_recovered_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resume path: the branch name is derived from the task id, so a restarted task
    hits its own first attempt's pull request.

    Recovering it is the difference between a resume that finishes and one that dies at the
    last node with the branch already pushed.
    """
    fake = _FakeGitHub(
        (422, _ALREADY_OPEN),
        (200, [{"html_url": "https://github.com/owner/repo/pull/3"}]),
    )

    assert _open(fake, monkeypatch) == "https://github.com/owner/repo/pull/3"

    listing = fake.calls[1]
    assert listing["method"] == "GET"
    # `owner:branch`, not `branch`. An unqualified `head` is accepted, returns an empty
    # list, and makes this recovery look broken rather than mis-called.
    assert "head=owner%3Aagent%2Ft-1" in listing["url"]
    assert "state=open" in listing["url"]


def test_a_422_about_something_else_is_not_recovered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other 422 from this endpoint is a real failure and must surface as one."""
    fake = _FakeGitHub((422, {"message": "No commits between main and agent/t-1"}))

    with pytest.raises(github.GitHubError) as raised:
        _open(fake, monkeypatch)

    assert raised.value.status == 422
    assert "No commits between" in raised.value.body
    assert len(fake.calls) == 1, "no pointless listing call for a 422 that is not about this"


def test_an_already_exists_422_with_no_open_pull_request_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty list means the 422 was about something else after all.

    The caller then raises with the body that actually explains it, which is more use than
    anything the recovery could say.
    """
    fake = _FakeGitHub((422, _ALREADY_OPEN), (200, []))
    with pytest.raises(github.GitHubError):
        _open(fake, monkeypatch)


def test_a_success_with_no_url_is_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 201 whose body carries no `html_url` has given `submit` nothing to record."""
    fake = _FakeGitHub((201, {"number": 9}))
    with pytest.raises(github.GitHubError):
        _open(fake, monkeypatch)


def test_the_token_never_reaches_an_exception_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """A traceback out of `submit` is written to CloudWatch, where it outlives the task.

    GitHub echoes a request back in some error bodies, so the body is scrubbed on every
    path that can surface it rather than only on the ones known to echo.
    """
    echoed = {"message": f"Bad credentials for Bearer {_TOKEN}", "documentation_url": "..."}
    fake = _FakeGitHub((401, echoed))

    with pytest.raises(github.GitHubError) as raised:
        _open(fake, monkeypatch)

    assert _TOKEN not in str(raised.value)
    assert _TOKEN not in raised.value.body
    assert "***" in raised.value.body, "scrubbed, not merely absent by luck"


def test_scrubbing_an_empty_token_does_not_redact_everything() -> None:
    """`"".replace` would otherwise be "found" between every pair of characters."""
    assert github._scrub("a body", "") == "a body"
    assert github._scrub(f"before {_TOKEN} after", _TOKEN) == "before *** after"


def test_a_html_error_page_does_not_replace_githubs_own_explanation() -> None:
    """A rate limiter or a proxy in front of the API answers with HTML, not JSON.

    A `JSONDecodeError` pointing at column 1 would hide the page that says what happened.
    """
    assert github._decode(b"<html>too many requests</html>") == {
        "message": "<html>too many requests</html>"
    }
    assert github._decode(b"") == {"message": ""}
    assert github._decode(b'"just a string"') == {"message": '"just a string"'}
    assert github._decode(b'{"html_url": "u"}') == {"html_url": "u"}


def test_a_long_body_is_bounded_and_says_so() -> None:
    """The body is only ever read by a human, out of a log line or a status row."""
    rendered = github._render({"message": "x" * 10_000})
    assert len(rendered) < 10_000
    assert "more characters truncated" in rendered


def test_only_a_transport_failure_raises_out_of_the_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An HTTP status is *returned*, because the 422 recovery has to read a failed body.

    `urllib` otherwise leaves that body locked inside the `HTTPError`.
    """
    import urllib.error

    class _FakeResponse:
        status = 201

        def read(self) -> bytes:
            return json.dumps({"html_url": "https://github.com/owner/repo/pull/2"}).encode()

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def _ok(*_: object, **__: object) -> _FakeResponse:
        return _FakeResponse()

    monkeypatch.setattr(github.urllib.request, "urlopen", _ok)
    status, decoded = github._request("POST", "https://api.github.com/x", token=_TOKEN)
    assert status == 201
    assert decoded == {"html_url": "https://github.com/owner/repo/pull/2"}

    def _boom(*_: object, **__: object) -> None:
        raise urllib.error.URLError(f"cannot reach https://api.github.com with {_TOKEN}")

    monkeypatch.setattr(github.urllib.request, "urlopen", _boom)
    with pytest.raises(github.GitHubError) as raised:
        github._request("POST", "https://api.github.com/x", token=_TOKEN)
    assert _TOKEN not in str(raised.value), "a transport error can quote the request back"
