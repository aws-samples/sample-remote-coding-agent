"""The MCP server Claude Code loads over stdio, and its SigV4 API client.

Three modules, in dependency order:

- `client.py` -- the only HTTP boundary: endpoint discovery from the CloudFormation
  stack, the `~/.rca/endpoint.json` cache, SigV4 signing, and one `httpx` call.
  `ALLOWED_METHODS` is `{"GET", "POST"}`, so teardown is unreachable from this package.
- `format.py` -- pure renderers, and a pinned copy of the control plane's
  repository-URL parser. No boto3, no httpx, no I/O.
- `server.py` -- the transport-free tool registry, `call_tool`, and the stdio adapter
  (`build_server`/`serve`/`main`, the `rca-mcp` console script).

This package imports nothing at all: `import rca_mcp` must not pull in boto3 or httpx,
so `format.py`'s tests stay cheap and a broken credential chain cannot break an import.
"""

from __future__ import annotations
