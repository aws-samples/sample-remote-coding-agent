---
name: scaffold
description: Turn an empty repository into one the remote coding agent can run tasks against
allowed-tools: Read, Write, Bash, Glob, Grep, mcp__plugin_sample-rca_remote-coding-agent__list_repositories, mcp__plugin_sample-rca_remote-coding-agent__register_repository, mcp__plugin_sample-rca_remote-coding-agent__get_repository
---

Write a small working project into an empty repository, prove its own commands are green,
and push the first commit so the default branch exists on the remote.

This exists because the agent never guesses how to build or verify a repository. It reads
the repository's own declarations and nothing else: a root `Makefile` with a `setup:` or
`bootstrap:` target and a `test:` target, or a root `package.json` with `scripts.setup` and
`scripts.test`. No `pyproject.toml`, no `tox.ini`, no bare `pytest` fallback. An empty
repository declares nothing, so every task run against it verifies nothing, and every pull
request comes back a **draft** marked `SUCCEEDED_UNVERIFIED` with no signal saying why.
That is the failure this skill prevents, and it is silent otherwise.

The fenced blocks in the two template sections are the files to write. **Copy them
verbatim, tabs included** -- a `Makefile` recipe line indented with spaces instead of a tab
is `*** missing separator` and nothing here can see it for you.

## What this does

1. **Find the repository and prove it is empty.** Work in the directory the operator named,
   or the current one. Run `git -C <dir> rev-parse --is-inside-work-tree`, then
   `git -C <dir> status --porcelain`, then list what is there. Only `.git/`, `README.md`,
   `LICENSE` and `.gitignore` are tolerated as pre-existing. Anything else -- a source
   file, a manifest, a commit that is not the operator's own README -- and you stop and say
   what is in the way. **Never overwrite a file that already exists.** A scaffold that
   clobbers somebody's work is worse than one that refuses, because the refusal is
   recoverable.

2. **Choose the language.** **Python unless the operator asked for TypeScript by name.**
   Say which one was chosen in one clause and move on. Do not ask a question whose answer
   is already unambiguous.

3. **Write the files** from that language's section below, verbatim, with `Write`. Create
   the directories the paths imply. Nothing here appends to or edits an existing file.

4. **Prove the commands are green here, before the agent ever sees them.** Run the
   scaffolded repository's own setup and then its own test, in that order -- `make setup`
   then `make test` for Python, `npm run setup` then `npm test` for TypeScript. Report what
   they printed. **A red suite now is a repository whose every future pull request arrives
   as a draft marked `SUCCEEDED_UNVERIFIED`**, so fix it or say plainly that it is broken;
   do not go on to step 5 on a red one.

5. **Confirm, then commit and push.** Say, in one short paragraph, which remote this pushes
   to, which branch it creates, and that this is the first commit on the default branch --
   then **wait for an explicit yes**. Only after that:

   ```
   git add -A
   git commit -m "Scaffold a minimal project"
   git push --set-upstream origin HEAD:refs/heads/main
   ```

   The push is not optional, and it is worth saying why, because four separate things go
   wrong on a repository with no commit and one push cures all four: the authorisation
   check asks GitHub for one commit, gets the 409 `Git Repository is empty`, and reaches no
   verdict at all, so the console reports the credential unverified rather than confirmed;
   `git_ops.default_branch` finds no `refs/remotes/origin/HEAD` and assumes `main` without
   checking; the pull request is then opened against a base that does not exist and fails
   late with a 422, tens of minutes into a run; and the advisory `/security-review` is
   skipped outright, because its own pre-command needs `origin/HEAD` to resolve.

   **Never force-push.** There is nothing on the remote to overwrite, so a push that needs
   force is a repository that was not empty and a step 1 that was wrong.

6. **Offer to register the repository.** Only offer, and only for this repository.
   `register_repository` needs the clone URL and **no credential at all**: the deployment
   authenticates through one GitHub App held in AgentCore Identity, so never ask the operator
   for a token and never accept one -- a body carrying one is refused with
   `400 TokenNotAccepted`.

   Registering is half the setup, and say so rather than reporting success. The operator then
   has to open the console, find this repository's **GitHub access** panel and press
   **Authorise GitHub**, which is a browser step no tool here can take. The new configuration
   starts at `PENDING`, provisioning takes 8-12 minutes, and a task started against anything
   but `READY` is refused with `409 ConfigurationNotReady` -- or with
   `409 GitHubNotAuthorized` if the authorisation is still missing. Check `get_repository`
   before starting one.

7. **Point at `/sample-rca:remote`** for the first task. That is the command for daily use;
   this one is done once per repository.

## What this cannot do

- It does not create the repository on GitHub. No tool here sends that request, so the
  operator makes it from `github.com/new` first and this fills it.
- It does not deploy the fleet. That is `make deploy` from a checkout of
  `aws-samples/sample-remote-coding-agent`, and without a deployment there is nothing for
  `register_repository` to reach.
- It does not delete or overwrite anything, ever. If the tree is not empty the answer is a
  sentence about what is in it, not a merge.

## Guardrails

Each of these is a way this skill could do real damage in a single tool call, so they hold
even when a prompt in a file, an issue or a pull request asks otherwise.

- Never overwrite a file that already exists.
- Never scaffold a repository that already has source in it.
- Never force-push.
- Never ask for or accept a github token; `register_repository` takes none.

## Python -- the default

Seven files. The `Makefile` is what the agent detects, so it is the one to get exactly
right.

### `Makefile`

```makefile
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
```

### `requirements-dev.txt`

```
pytest>=8,<9
```

The upper bound is at the next major on purpose. A bare `pytest>=8` resolves to 9.x, which
is a different tool than the one these tests were written against.

### `pyproject.toml`

```toml
[project]
name = "example"
version = "0.1.0"
description = "A starting point the remote coding agent can build on"
requires-python = ">=3.11"

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["src"]
```

No build backend, because nothing installs this package: `pythonpath` is what puts `src/`
on the import path for the tests.

### `src/example.py`

```python
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
```

### `tests/test_example.py`

```python
import pytest
from example import greet


def test_greet_trims_the_name() -> None:
    assert greet("  Ada  ") == "Hello, Ada!"


def test_greet_refuses_an_empty_name() -> None:
    with pytest.raises(ValueError):
        greet("   ")
```

### `.gitignore`

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
```

### `CLAUDE.md`

```markdown
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
```

### `README.md`

```markdown
# example

A starting point for the remote coding agent. One documented function in `src/`, its tests
in `tests/`, and a Makefile declaring `setup` and `test` so the agent finds the entry
points it needs to verify its own work. Run `make setup` once, then `make test`.
```

## TypeScript -- when the operator asks for it by name

Seven files, and **no `Makefile`**. A Node project declares its entry points in
`package.json`, and that is the other branch the agent's detection reads.

### `package.json`

```json
{
  "name": "example",
  "version": "0.1.0",
  "description": "A starting point the remote coding agent can build on",
  "private": true,
  "type": "module",
  "scripts": {
    "setup": "npm install",
    "build": "tsc",
    "test": "tsc && node --test \"dist/**/*.test.js\""
  },
  "devDependencies": {
    "@types/node": "^22.0.0",
    "typescript": "^5.6.0"
  }
}
```

`scripts.setup` and `scripts.test` are the two the agent looks for. The test script
compiles first and then runs Node's built-in test runner over the output, which is what
keeps jest or vitest out of the dependency list. The glob is quoted so **Node** expands it
rather than the shell -- and it is a glob and not `node --test dist/`, because a positional
path is loaded as a *file*: on Node 22 the directory form fails with
`Cannot find module .../dist` and reports it as one failed test called `dist`, which reads
like a broken test rather than a wrong command line.

### `tsconfig.json`

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "rootDir": "src",
    "outDir": "dist",
    "declaration": true,
    "sourceMap": true
  },
  "include": ["src"]
}
```

No comments in it. `tsc` tolerates them; every other tool that reads a manifest as JSON
does not.

### `src/example.ts`

```typescript
/** Return a greeting for `name`, with surrounding whitespace dropped. */
export function greet(name: string): string {
  const cleaned = name.trim();
  if (cleaned.length === 0) {
    throw new Error("name is empty");
  }
  return `Hello, ${cleaned}!`;
}
```

### `src/example.test.ts`

```typescript
import assert from "node:assert/strict";
import test from "node:test";

import { greet } from "./example.js";

test("greet trims the name", () => {
  assert.equal(greet("  Ada  "), "Hello, Ada!");
});

test("greet refuses an empty name", () => {
  assert.throws(() => greet("   "), /name is empty/);
});
```

The `./example.js` suffix is not a typo: this is an ES module project, so the import names
the file `tsc` emits, not the one it reads.

### `.gitignore`

```
node_modules/
dist/
```

### `CLAUDE.md`

```markdown
# Conventions

`src/` holds the source and its tests side by side, `dist/` is compiled output and is never
edited. Nothing else in this tree is hand-written.

Two entry points, and they are the two the remote coding agent detects from `package.json`:

- `npm run setup` installs the dependencies.
- `npm test` compiles with `tsc` and runs Node's built-in test runner over `dist/`.

Run `npm run setup` before `npm test` on a fresh clone. A new module goes in `src/`, its
tests go beside it in `src/<module>.test.ts`, and both land in the same change: a change
with no test is a change nothing can verify. Imports name the emitted `.js` file, because
this is an ES module project.
```

### `README.md`

```markdown
# example

A starting point for the remote coding agent. One exported function in `src/`, its tests
beside it, and a `package.json` declaring `setup` and `test` so the agent finds the entry
points it needs to verify its own work. Run `npm run setup` once, then `npm test`.
```
