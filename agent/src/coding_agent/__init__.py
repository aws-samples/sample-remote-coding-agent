"""The AgentCore Runtime package: the `plan -> code -> verify -> review` graph.

Deliberately empty of imports. `tests/test_layout_agent.py` imports `coding_agent` to
assert the src layout resolves, and importing submodules here would make that test
pay for langgraph and the Claude Agent SDK -- and would make every module in the
package unreloadable in isolation.
"""

from __future__ import annotations

__version__ = "0.1.0"
