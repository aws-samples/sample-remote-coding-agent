"""`api/` is a Lambda bundle: importable off `pythonpath`, but not a wheel."""

from __future__ import annotations

from pathlib import Path

import handlers
from conftest import REPO_ROOT


def test_the_api_package_imports_from_api_src() -> None:
    assert Path(handlers.__file__).resolve() == REPO_ROOT / "api/src/handlers/__init__.py"


def test_api_declares_dependencies_in_requirements_not_a_pyproject() -> None:
    assert (REPO_ROOT / "api/requirements.txt").exists()
    assert not (REPO_ROOT / "api/pyproject.toml").exists(), (
        "api/ is `pip install -r`'d into a CDK asset, not built as a wheel; a "
        "pyproject.toml here would claim otherwise"
    )
