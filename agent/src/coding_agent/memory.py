"""`.agent/memory/` -- the notes that carry a finding from one iteration to the next.

Everything else about an iteration is thrown away: the role's session ends, its
transcript goes to the log and the next role starts from an empty context. This
directory is the exception, so it is the only channel by which "the suite needs
`make setup` first" reaches the coder who is about to waste twenty minutes on it.

One file per writer, and every role is told to **append** rather than rewrite. An
earlier entry is evidence -- it is what somebody already paid to discover -- and a
rewrite silently costs the next iteration the same discovery again. One file per
writer is the mechanical half of the same rule: two roles appending to one path lose
one of the two entries, because an append is a rewrite of the whole file.
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

# The three directories every role expects to exist. `spec` and `review` are here and
# not in the nodes that write them because a role told to write `.agent/review/...`
# should never have to create the parent itself -- a role that gets an ENOENT
# improvises a path instead, and the node that reads the file back finds nothing.
_TASK_DIRS = (".agent/memory", ".agent/spec", ".agent/review")


def memory_dir(repo: Path) -> Path:
    """Where the per-role notes live, inside the clone so a role can just edit them."""
    return repo / ".agent/memory"


def ensure_dirs(repo: Path) -> None:
    """Create the task's `.agent/` scaffolding. Idempotent, and safe on a resume."""
    for relative in _TASK_DIRS:
        (repo / relative).mkdir(parents=True, exist_ok=True)


def note_path(repo: Path, role: str) -> Path:
    """The file `role` owns and is the only writer of."""
    return memory_dir(repo) / f"{role}.md"


def read_digest(repo: Path, *, max_chars: int = 8000) -> str:
    """Every note, concatenated, for pasting into a role's prompt.

    Sorted by filename so the same set of notes produces the same digest twice --
    directory order is arbitrary, and a prompt that reshuffles between iterations
    defeats prompt caching for no benefit. Each section is headed with the file's own
    name (`## plan.md`), which doubles as the instruction: that is the path a role
    appends its own findings to.

    Over `max_chars` the digest is cut with an explicit `…(truncated)` marker, because
    a role has to be able to tell "there was more" from "that is all there is" -- a
    silent cut reads as a complete history and invites the wrong conclusion.

    Returns `""` when the directory is absent: a first iteration has no notes, and that
    is not a condition worth an exception at every call site.
    """
    directory = memory_dir(repo)
    if not directory.is_dir():
        return ""

    sections: list[str] = []
    for path in sorted(directory.glob("*.md"), key=lambda p: p.name):
        try:
            body = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            # The notes live in a clone we do not control, so a note can be a dangling
            # symlink or unreadable. A missing digest is a worse prompt; a raise here
            # would be a dead task.
            log.warning("skipping unreadable memory note %s: %s", path, exc)
            continue
        if body:
            sections.append(f"## {path.name}\n\n{body}")

    digest = "\n\n".join(sections)
    if len(digest) > max_chars:
        return digest[:max_chars].rstrip() + "\n\n…(truncated)"
    return digest
