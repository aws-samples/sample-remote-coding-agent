#!/usr/bin/env bash
# Turn an empty GitHub repository into one the remote coding agent can run tasks against.
#
# The agent never guesses how to build a repository. It needs a default branch to open pull
# requests against, and a root Makefile with `setup:` and `test:` targets to verify its work.
# An empty repository has neither, so tasks fail at the end with `HTTP 422 ... "base"` or come
# back as unverified drafts. This script writes a minimal Python project that declares both,
# proves `make setup` and `make test` pass on your machine, and pushes it after you confirm.
#
# It is the same Python template as the `/sample-rca:scaffold` Claude Code skill, for people
# who do not use Claude Code.
#
# Usage:
#   scripts/scaffold.sh <owner/name | https://github.com/owner/name | path/to/local/clone> [--yes]
#
# Given a GitHub name or URL, it clones into a temporary directory and deletes it afterwards.
# The push uses your normal git credentials (gh auth login, a credential helper, or SSH), not
# the fleet's GitHub App, which is not installed on this repository until it exists.
# --yes skips the confirmation before the push.
#
# Needs git, make and python3 on PATH. It never overwrites an existing file, never touches a
# repository that already has source in it, and never force-pushes.

set -euo pipefail

die() {
  printf 'scaffold: %s\n' "$@" >&2
  exit 1
}

usage() {
  die "usage: scripts/scaffold.sh <owner/name | https://github.com/owner/name | path/to/clone> [--yes]"
}

target=""
assume_yes=0
for arg in "$@"; do
  case "$arg" in
    --yes | -y) assume_yes=1 ;;
    -h | --help) usage ;;
    -*) die "unknown option: $arg" ;;
    *)
      [ -z "$target" ] || usage
      target=$arg
      ;;
  esac
done
[ -n "$target" ] || usage
[ "$assume_yes" -eq 1 ] || [ -t 0 ] \
  || die "no terminal to confirm the push on. Re-run with --yes to push without asking."

for tool in git make python3; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is not on PATH."
done

# --- 1. Find the repository -------------------------------------------------------------

if [ -d "$target" ]; then
  repo_dir=$(cd "$target" && pwd -P)
  git -C "$repo_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
    || die "$repo_dir is not a git repository. Clone the GitHub repository first."
else
  case "$target" in
    *://* | git@*) url=$target ;;
    */*) url="https://github.com/${target}.git" ;;
    *) usage ;;
  esac
  tmp_dir=$(mktemp -d)
  trap 'rm -rf "$tmp_dir"' EXIT
  printf 'scaffold: cloning %s\n' "$url"
  git clone --quiet "$url" "$tmp_dir/repo" \
    || die "could not clone $url. Check the name and your git credentials."
  repo_dir=$tmp_dir/repo
fi

remote_url=$(git -C "$repo_dir" remote get-url origin 2>/dev/null) \
  || die "$repo_dir has no 'origin' remote to push to."

# --- 2. Prove it is empty ---------------------------------------------------------------

# Only a README, a LICENSE and a .gitignore may already be there, which is what GitHub's
# "create repository" checkboxes add. Anything else is somebody's work, and this script
# refuses rather than mixing a template into it.
blocked=""
for entry in "$repo_dir"/* "$repo_dir"/.[!.]* "$repo_dir"/..?*; do
  [ -e "$entry" ] || continue
  name=$(basename "$entry")
  case "$name" in
    .git | README.md | LICENSE | .gitignore) ;;
    *) blocked="$blocked $name" ;;
  esac
done
[ -z "$blocked" ] || die \
  "$remote_url is not empty. It already has:$blocked" \
  "This script only fills an empty repository (README.md, LICENSE and .gitignore are fine)." \
  "Use a new empty repository, or add a Makefile with 'setup' and 'test' targets yourself."

if git -C "$repo_dir" rev-parse --verify --quiet HEAD >/dev/null; then
  # The repository was created with a README, LICENSE or .gitignore, so its default branch
  # exists already. Commit on top of it, which is a normal fast-forward push.
  branch=$(git -C "$repo_dir" symbolic-ref --short HEAD 2>/dev/null) \
    || die "$repo_dir is on a detached HEAD. Check out the default branch first."
else
  # No commits at all. Make sure the remote agrees, so a stale local `git init` cannot
  # push a second, unrelated history next to branches that already exist.
  heads=$(git -C "$repo_dir" ls-remote --heads origin 2>/dev/null) \
    || die "could not reach $remote_url. Check your git credentials."
  [ -z "$heads" ] || die \
    "$remote_url already has branches, but $repo_dir has no commits." \
    "Clone the repository fresh and run this again."
  branch=main
fi

# --- 3. Write the files -----------------------------------------------------------------

printf 'scaffold: writing a minimal Python project into %s\n' "$repo_dir"

# Writes stdin to a path under the repository unless that path already exists.
write_file() {
  local path=$1
  if [ -e "$repo_dir/$path" ]; then
    cat >/dev/null
    printf '  kept   %s (already there)\n' "$path"
    return
  fi
  mkdir -p "$(dirname "$repo_dir/$path")"
  cat >"$repo_dir/$path"
  printf '  wrote  %s\n' "$path"
}

# The recipe lines below are indented with TABS. Make requires it, and spaces fail with
# `*** missing separator`.
write_file Makefile <<'EOF'
.PHONY: setup test

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)

# The `pwd -P` is load-bearing: the remote coding agent creates `.venv` as a SYMLINK onto a
# per-task directory before it runs setup, and `python -m venv` refuses to build into a
# symlink -- it dies with `ValueError: Unable to create directory`, and `--clear` does not
# help. Resolving the link first is the whole fix; do not simplify it away.
# Keyed on pip rather than on the interpreter, because a half-built virtualenv has the
# binary and not the installer.
setup:
	test -x .venv/bin/pip || python3 -m venv "$$(cd .venv 2>/dev/null && pwd -P || echo .venv)"
	.venv/bin/python -m pip install --quiet --upgrade pip
	.venv/bin/python -m pip install --quiet -r requirements-dev.txt

test:
	$(PYTHON) -m pytest -q
EOF

write_file requirements-dev.txt <<'EOF'
pytest>=8,<9
EOF

write_file pyproject.toml <<'EOF'
[project]
name = "example"
version = "0.1.0"
description = "A starting point the remote coding agent can build on"
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
EOF

write_file src/example.py <<'EOF'
"""One documented function, so the tree has something real in it from the first commit."""


def greet(name: str) -> str:
    """Return a greeting for `name`, with surrounding whitespace dropped.

    Small on purpose. The point of this file is that `make test` has something to prove
    green before the first task runs, not that the function is interesting.
    """
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("name is empty")
    return f"Hello, {cleaned}!"
EOF

write_file tests/test_example.py <<'EOF'
import pytest
from example import greet


def test_greet_trims_the_name() -> None:
    assert greet("  Ada  ") == "Hello, Ada!"


def test_greet_refuses_an_empty_name() -> None:
    with pytest.raises(ValueError):
        greet("   ")
EOF

write_file CLAUDE.md <<'EOF'
# Conventions

`src/` holds the source and `tests/` holds the tests. Nothing else in this tree is
hand-written.

Two entry points, and they are the two the remote coding agent detects from the Makefile:

- `make setup` builds `.venv` and installs the dev requirements. It is idempotent, so
  running it on a warm checkout is close to a no-op.
- `make test` runs pytest over `tests/`.

Run `make setup` before `make test` on a fresh clone. A new module goes in `src/`, its
tests go in `tests/test_<module>.py`, and both land in the same change: a change with no
test is a change nothing can verify.
EOF

write_file README.md <<'EOF'
# example

A starting point for the remote coding agent. One documented function in `src/`, its tests
in `tests/`, and a Makefile declaring `setup` and `test` so the agent finds the entry
points it needs to verify its own work. Run `make setup` once, then `make test`.
EOF

# A .gitignore that GitHub created is kept, but it must ignore the virtualenv and caches,
# or step 5 would commit them. Missing lines are appended; nothing is removed.
ignore_lines='.venv/
__pycache__/
*.pyc
.pytest_cache/'
if [ -e "$repo_dir/.gitignore" ]; then
  added=0
  while IFS= read -r line; do
    if ! grep -qxF "$line" "$repo_dir/.gitignore"; then
      [ "$added" -eq 1 ] || { [ -z "$(tail -c1 "$repo_dir/.gitignore")" ] || echo >>"$repo_dir/.gitignore"; }
      printf '%s\n' "$line" >>"$repo_dir/.gitignore"
      added=1
    fi
  done <<<"$ignore_lines"
  if [ "$added" -eq 1 ]; then
    printf '  added  missing entries to .gitignore\n'
  else
    printf '  kept   .gitignore (already there)\n'
  fi
else
  printf '%s\n' "$ignore_lines" | write_file .gitignore
fi

# --- 4. Prove the commands pass here, before the agent relies on them ------------------

printf 'scaffold: running make setup\n'
make -C "$repo_dir" setup || die "make setup failed, so nothing was committed or pushed."
printf 'scaffold: running make test\n'
make -C "$repo_dir" test || die "make test failed, so nothing was committed or pushed."

# --- 5. Confirm, then commit and push ---------------------------------------------------

printf '\nscaffold: ready to commit and push to %s as branch %s.\n' "$remote_url" "$branch"
if [ "$assume_yes" -ne 1 ]; then
  read -r -p "Push now? [y/N] " answer
  case "$answer" in
    y | Y | yes | YES) ;;
    *)
      if [ -n "${tmp_dir:-}" ]; then
        die "nothing was pushed, and the temporary clone was deleted."
      fi
      die "nothing was pushed. The files are still in $repo_dir."
      ;;
  esac
fi

git -C "$repo_dir" add -A
git -C "$repo_dir" commit --quiet -m "Scaffold a minimal project" \
  || die "git commit failed. Set your identity with 'git config --global user.name' and user.email."
git -C "$repo_dir" push --quiet --set-upstream origin "HEAD:refs/heads/$branch" \
  || die "git push failed. Nothing was force-pushed; check your git credentials."

slug=$(printf '%s' "$remote_url" | sed -E 's#^(https://github\.com/|git@github\.com:)##; s#\.git$##')
printf '\nscaffold: pushed %s to %s.\n' "$branch" "$slug"
# The full URL, not the slug: the console's repository field accepts only
# https://github.com/owner/repo or git@github.com:owner/repo.git, and refused the shorthand
# this line used to print. The install comes first because registering an uninstalled
# repository succeeds and then cannot be authorised.
printf 'Next:\n'
printf '  1. Install the GitHub App on %s: the console'"'"'s GitHub App page, then Install on GitHub.\n' "$slug"
printf '  2. Register https://github.com/%s in the console with + Add repository.\n' "$slug"
printf '  3. Wait for READY, then start a task.\n'

