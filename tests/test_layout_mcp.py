"""Our `mcp/` directory must not shadow the MCP SDK on PyPI."""

from __future__ import annotations

from pathlib import Path

import mcp.server
import rca_mcp
from conftest import REPO_ROOT


def test_the_mcp_sdk_is_not_shadowed_by_our_own_mcp_directory() -> None:
    assert not (REPO_ROOT / "mcp/__init__.py").exists(), (
        "Do not create mcp/__init__.py. Under `python -m pytest` the repository root "
        "is sys.path[0], so a regular top-level `mcp` package there beats the "
        "installed SDK outright and every `import mcp.server` fails with "
        "ModuleNotFoundError: No module named 'mcp.server' -- which reads as a "
        "missing dependency, not as a shadowed one. Without the __init__.py, `mcp/` "
        "is only a namespace portion and the real SDK in site-packages wins."
    )
    # `REPO_ROOT / "mcp"`, NOT `REPO_ROOT`: `make setup` puts `.venv` inside the
    # repository, so a resolved site-packages path *is* under REPO_ROOT and the
    # obvious spelling fails on every developer machine while passing in the agent's
    # container, where `.venv` is a symlink onto /tmp that `.resolve()` follows out of
    # the tree. The claim is only ever "our own mcp/ did not win", so scope it there.
    assert not Path(mcp.__file__).resolve().is_relative_to(REPO_ROOT / "mcp"), (
        f"the resolved `mcp` package must come from site-packages, not from this "
        f"repository's own mcp/ directory; got {mcp.__file__}"
    )


def test_the_mcp_package_imports_from_mcp_src() -> None:
    assert Path(rca_mcp.__file__).resolve() == REPO_ROOT / "mcp/src/rca_mcp/__init__.py"
