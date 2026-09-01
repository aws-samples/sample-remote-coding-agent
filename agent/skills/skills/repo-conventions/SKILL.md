---
name: repo-conventions
description: Use at the start of any implementation task in an unfamiliar repository, and again before adding a file, a dependency or a test — read the repository's own contributor documentation and match the idiom already in the tree instead of importing a house style.
---

# Repository conventions

Read before you edit. Every repository has already decided most of what you are
about to decide, and the decision is written down.

## Read these first

- `CLAUDE.md` (or `AGENTS.md`) — what is expensive to work out from the source, and
  the commands that verify the tree.
- `CONTRIBUTING.md` and `README.md` — how a change is expected to arrive.
- The project manifest — `pyproject.toml`, `package.json`, `go.mod`, `Cargo.toml`.
  It names the dependencies you already have, the test runner and the lint config.

## Then match what is there

- Naming: follow the modules and symbols next to the ones you are adding, not your
  own preference. A file that reads like the file beside it is reviewable.
- Error handling: raise the exception type this package already raises, and log at
  the level it already logs at. Do not introduce a second error convention.
- Tests: put the new test where the existing tests for that module live, name it the
  way its neighbours are named, and use the fixtures and helpers already provided
  rather than new ones.
- Comments: match the density around you, and explain *why*. If you change
  behaviour a comment describes, update the comment in the same edit.
- Prefer editing an existing file over adding a new one.

## Dependencies

Add one only when nothing already present will do, and only with an upper bound at
the next major version (`foo>=1.4,<2`). An unbounded floor lets a major release land
and turn a green checkout red with nothing in the diff to explain it. Record the
reason for the new dependency where the change is described.
