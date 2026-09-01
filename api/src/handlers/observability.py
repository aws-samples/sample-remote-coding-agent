"""Where a task's logs are, derived from what is already stored.

Pure functions, no boto3, no I/O -- `tests/test_api_grant_contract.py` asserts this
module's AST contains no boto3 at all. That is what keeps note 30 true: a read handler
never gains a `logs:*` or `bedrock-agentcore:*` permission to look a log location up
live, so the naming scheme is a code change rather than a data migration, and a
compromised read route cannot enumerate the fleet.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

# Invariant 14: `create_agent_runtime` creates the DEFAULT endpoint itself. There is
# never a custom one, so this is the only qualifier anything in this repository sends.
ENDPOINT_NAME = "DEFAULT"

# botocore's own `SessionType` minimum, read off the vendored service model. It is
# enforced *client-side*, so a short id is a `ParamValidationError` before any request
# leaves the Lambda -- which is why it reads as a client bug rather than a service one.
SESSION_ID_MIN_LENGTH = 33

LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes"

# The service's own `MountPath` pattern. botocore does *not* validate `pattern`
# client-side, so `/mnt/a/b` is only a server-side `ValidationException`; the
# provisioner checks it here instead.
_MOUNT_PATH_PATTERN = re.compile(r"^/mnt/[a-zA-Z0-9._-]+/?$")


def session_id(task_id: str) -> str:
    """The one derivation of a task's AgentCore session id (invariant 18).

    Deterministic -- no time, no randomness -- because the session id is what remounts
    the same workspace on a resume and what makes a task's CloudWatch events findable
    after the fact. A second definition means a resumed task gets a second session,
    which means two branches and two pull requests for one task.

    Padded with a hash of the id rather than a constant, so it stays >= 33 characters
    for *any* non-empty task id (ours are ~20) while remaining a function of the id
    alone.
    """
    if not task_id:
        raise ValueError("task_id is required to derive a session id")
    return f"{task_id}-{hashlib.sha256(task_id.encode()).hexdigest()[:32]}"


def runtime_log_group(agent_runtime_id: str, endpoint_name: str = ENDPOINT_NAME) -> str:
    return f"{LOG_GROUP_PREFIX}/{agent_runtime_id}-{endpoint_name}"


def runtime_log_stream(session: str) -> str:
    return f"[runtime-logs]{session}"


def _escape(value: str) -> str:
    """The CloudWatch console's double escaping: percent-encode, then encode the
    percent signs themselves. `/aws/...` becomes `$252Faws$252F...` and
    `[runtime-logs]` becomes `$255Bruntime-logs$255D`. A link built with single
    escaping opens the console on an empty log-group list."""
    return quote(value, safe="").replace("%", "$25")


def console_url(region: str, log_group: str, log_stream: str | None = None) -> str:
    url = (
        f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
        f"#logsV2:log-groups/log-group/{_escape(log_group)}"
    )
    if log_stream:
        url = f"{url}/log-events/{_escape(log_stream)}"
    return url


def log_locations(config: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, str]:
    """Everything a console needs to open this task's logs, derived from stored
    attributes only. A configuration that is still provisioning has no runtime and
    therefore no logs; that is an empty dict, not a 500."""
    runtime_id = config.get("agentRuntimeId")
    if not runtime_id:
        return {}
    session = task.get("sessionId") or session_id(task["taskId"])
    region = config.get("region") or os.environ.get("AWS_REGION", "")
    group = runtime_log_group(str(runtime_id))
    stream = runtime_log_stream(str(session))
    return {
        "logGroup": group,
        "logStream": stream,
        "consoleUrl": console_url(str(region), group, stream),
        "sessionId": str(session),
    }


def is_valid_mount_path(path: str) -> bool:
    """Whether AgentCore will accept `path` as a `sessionStorage.mountPath`: exactly one
    level under `/mnt`. `/mnt/workspace/work` is a valid *work root* and an invalid
    mount path, which is precisely the confusion this function exists to catch before
    `CreateAgentRuntime` reports it as a bare `ValidationException`."""
    return bool(_MOUNT_PATH_PATTERN.match(path))
