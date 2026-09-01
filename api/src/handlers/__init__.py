"""Control-plane Lambda handlers: configurations, tasks, cards and dispatch.

Three HTTP modules (`configurations`, `cards`, `tasks`), three the queue and the
schedule drive (`provisioner`, `dispatcher`, `watchdog`), and three shared
(`_common`, `observability`, `runs`). `handlers` is a regular package and shadows
nothing on PyPI, so unlike `mcp/` it may carry an `__init__.py`.

This module imports no boto3, deliberately: `boto3.resource("dynamodb")` raises
`NoRegionError` at *construction*, and `tests/test_layout_api.py` imports `handlers`
at module level -- i.e. at collection time, before any fixture has set a region.
"""

from __future__ import annotations
