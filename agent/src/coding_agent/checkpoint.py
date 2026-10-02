"""A graph checkpointer that outlives the container the graph was running in.

This is the whole of what makes the watchdog's resume worth anything. AgentCore
hands the same *deterministic* session id back the same session storage, `WORK_ROOT`
is on that mount, and this file lives at `<workDir>/checkpoint/graph.pkl`. So a task
whose runtime was reaped mid-run comes back at the node it died in: it does not
re-clone the repository and it does not re-plan, which is the three wasted hours the
mount exists to prevent.

`InMemorySaver` already keeps the entire graph state in three dicts, so the only
thing missing is writing them down. Note what is deliberately *not* used:
`InMemorySaver(factory=PersistentDict)`. It is the obvious-looking answer and it is
wrong twice over -- `factory` is called three times (`storage`, `writes`, `blobs`)
with one `filename`, so the three dicts overwrite each other's file, and
`PersistentDict` flushes only on `close()`, which a reaped container never reaches.
"""

from __future__ import annotations

import logging
import pickle  # nosec B403 -- accepted, and argued where it is read: see `_load`
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.memory import InMemorySaver

from .state import TaskState

log = logging.getLogger(__name__)


class FileCheckpointer(InMemorySaver):
    """`InMemorySaver` plus one pickle file, rewritten after every mutating call.

    Subclassing pins three attribute names -- `storage`, `writes`, `blobs` -- that
    langgraph does not promise to keep across the `>=1.0,<2` range we allow. The pin
    is a considered trade: implementing `BaseCheckpointSaver` from scratch is more
    code and more ways to be subtly wrong about pending writes, and the test asserts
    a *round-trip* rather than the attribute names, so an upstream rename fails
    loudly and locally instead of quietly losing every resume.
    """

    def __init__(self, path: Path) -> None:
        # `InMemorySaver.__init__` takes only keyword-only `serde` and `factory`, and
        # both defaults are what we want: the persistence here is entirely ours, and
        # in particular `factory` stays `defaultdict` (see the module docstring).
        super().__init__()
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load()

    def _dump(self) -> None:
        """Write all three dicts to `self.path` atomically, or leave it untouched.

        The nested `defaultdict`s become plain dicts first: `storage`'s default
        factory is a `lambda` and pickle cannot reach it, so pickling the live
        objects fails outright.

        Written to a sibling temp file and renamed, because a half-written checkpoint
        is worse than no checkpoint -- it turns a resumable task into an unreadable
        one. `Path.replace` is `os.replace`, so the rename is atomic within the
        directory; the temp file goes away if the write itself fails, so a full disk
        does not leave litter behind on the capped mount.
        """
        payload: dict[str, Any] = {
            "storage": {
                thread_id: {ns: dict(checkpoints) for ns, checkpoints in namespaces.items()}
                for thread_id, namespaces in self.storage.items()
            },
            "writes": {key: dict(task_writes) for key, task_writes in self.writes.items()},
            "blobs": dict(self.blobs),
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            with tmp.open("wb") as handle:
                pickle.dump(payload, handle, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.replace(self.path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

    def _load(self) -> None:
        """Read the file back into the live dicts, or cold-start.

        Rebuilt key by key through `self.storage[thread][ns].update(...)` rather than
        assigned: replacing the attributes with the plain dicts we pickled would
        strip the `defaultdict` behaviour every `put` on the base class relies on,
        and the first write after a resume would raise `KeyError`.

        A missing file is a cold start and says nothing -- that is every first run.
        A corrupt or truncated one is a warning and a cold start, never an exception:
        losing a resume costs a re-clone, while raising here kills the task outright.
        """
        if not self.path.exists():
            return
        try:
            with self.path.open("rb") as handle:
                # `pickle.load` on a file the container can write is arbitrary code
                # execution, and it is accepted here rather than fixed. `checkpoint_path`
                # is `work_root / task_id / checkpoint/graph.pkl`, so the only writers are
                # this task's own roles and this task's own `setupCommand`/`verifyCommand`
                # -- and `commands` restricts those commands' ENVIRONMENT, not the
                # filesystem, so they can reach this path. But running that repository's
                # commands is already arbitrary code execution *by design*: it is the whole
                # point of the product, and it happens before any resume reads this file.
                # A malicious checkpoint therefore grants an attacker nothing they do not
                # already have inside this container. JSON is not the fix: the payload is
                # LangGraph's own nested `defaultdict` structures plus whatever objects a
                # checkpoint carries, and it would not round-trip -- which is the same fact
                # the docstring above states from the other side, that rebuilding key by
                # key is what preserves the `defaultdict` behaviour every later `put`
                # relies on. `docs/security.md`'s "What remains exposed" covers this too.
                payload = pickle.load(handle)  # nosec B301 -- the exposure argued just above
            for thread_id, namespaces in payload["storage"].items():
                for checkpoint_ns, checkpoints in namespaces.items():
                    self.storage[thread_id][checkpoint_ns].update(checkpoints)
            for key, task_writes in payload["writes"].items():
                self.writes[key].update(task_writes)
            self.blobs.update(payload["blobs"])
        except Exception:
            # Broad on purpose. A truncated file raises `EOFError` or
            # `UnpicklingError`, one written by an older layout raises `KeyError`, and
            # one whose payload names a class this process cannot import raises
            # whatever that import raises -- there is no useful shorter list.
            log.warning(
                "could not read the graph checkpoint at %s; starting cold, so this "
                "task will re-clone and re-plan",
                self.path,
                exc_info=True,
            )
            self.storage.clear()
            self.writes.clear()
            self.blobs.clear()
            return
        log.info("resumed the graph checkpoint at %s (%d threads)", self.path, len(self.storage))

    def _sync(self) -> None:
        """Persist, and never let a persistence failure become a dead graph.

        The checkpoint is an optimisation for the *next* run of this task; the run in
        progress is correct without it. So a full mount or a vanished directory is a
        warning and the graph carries on.
        """
        try:
            self._dump()
        except Exception:
            log.warning("could not persist the graph checkpoint to %s", self.path, exc_info=True)

    # Only the three *sync* mutators are overridden, and that is complete: verified at
    # langgraph 1.2.11, `InMemorySaver.aput`, `aput_writes` and `adelete_thread` are
    # thin `return self.put(...)` delegations, so the async graph path -- the only one
    # the container runs -- already lands here. Do not add three more overrides for
    # them; every write would then be pickled twice.

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Store the checkpoint in memory, then write the whole of memory down."""
        result = super().put(config, checkpoint, metadata, new_versions)
        self._sync()
        return result

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Pending writes are persisted too: they are how an interrupted superstep resumes."""
        super().put_writes(config, writes, task_id, task_path)
        self._sync()

    def delete_thread(self, thread_id: str) -> None:
        """Forget a thread on disk as well, or the next `_load` would resurrect it."""
        super().delete_thread(thread_id)
        self._sync()


def checkpoint_path(state: TaskState) -> Path:
    """Where this task's checkpoint lives: `<workDir>/checkpoint/graph.pkl`.

    Beside the clone rather than inside it, so nothing a coding role does to the
    repository -- `git clean -xfd` included -- can destroy the record of how far the
    task got.
    """
    return Path(state["workDir"]) / "checkpoint" / "graph.pkl"
