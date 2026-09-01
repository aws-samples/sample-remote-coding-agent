"""`agent/` is on `pythonpath` as a src layout, not as a top-level directory."""

from __future__ import annotations

from pathlib import Path

import coding_agent
from conftest import REPO_ROOT


def test_the_agent_package_imports_from_agent_src() -> None:
    assert Path(coding_agent.__file__).resolve() == REPO_ROOT / "agent/src/coding_agent/__init__.py"
    assert isinstance(coding_agent.__version__, str)
