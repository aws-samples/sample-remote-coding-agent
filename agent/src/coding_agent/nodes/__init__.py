"""The six graph nodes. A docstring only -- nothing is importable from here.

Two conventions hold across every module in this package, and both are what make the
phase testable rather than mocked:

**Call through the module object, never through a directly-imported name.** A node
does `subagents.run_role(...)`, `commands.run_command(...)`, `git_ops.changed_paths(...)`
and `github.create_pull_request(...)` -- never `from ..subagents import run_role`. A
test then monkeypatches one attribute on one module and the node under test is the
real node, with real branching, real guards and real error handling. With
`from ... import ...` the name is bound in the node's own namespace at import time and
the only way to substitute it is to patch the node module itself, which is how a suite
ends up asserting against its own mocks.

**Nothing is exported from this file.** No re-exports, no eager submodule imports:
`nodes/verify.py` must be importable (and reloadable) without dragging in the Claude
Agent SDK that `nodes/review.py` needs, because "verify imports nothing that could
form a verdict" is a property a test checks by reading the imports.
"""

from __future__ import annotations
