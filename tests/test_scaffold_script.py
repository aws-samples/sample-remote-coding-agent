"""`scripts/scaffold.sh`'s closing guidance, which is the operator's next three actions.

Read as text rather than run: the script clones and pushes, and its interesting output is
the last thing it prints after a push no test may make.
"""

from __future__ import annotations

from conftest import REPO_ROOT

_SCRIPT = REPO_ROOT / "scripts" / "scaffold.sh"


def _closing() -> str:
    text = _SCRIPT.read_text(encoding="utf-8")
    return text[text.index("printf 'Next:") :]


def test_the_next_steps_start_with_installing_the_github_app() -> None:
    """Registering a repository the app is not installed on succeeds and then cannot be
    authorised, and the old one-line "Next:" skipped the install entirely."""
    closing = _closing()
    assert "GitHub App" in closing and "Install" in closing
    assert closing.index("Install") < closing.index("+ Add repository")


def test_the_next_steps_name_the_url_the_console_accepts() -> None:
    """The console's repository field refuses `owner/repo`; the line printed exactly that."""
    assert "https://github.com/%s" in _closing()
