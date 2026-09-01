/**
 * Pinned duplication of the container's `coding_agent.state.TaskStatus`.
 *
 * This is the console's copy of a vocabulary that lives in another deployment unit,
 * and the two cannot import each other, so `tests/test_web_status_vocabulary.py`
 * parses this file **line by line** and compares it with the Python literal. That
 * makes the formatting below load-bearing: one entry per line, in `TaskStatus`'s own
 * order, `label` then `tone` then `terminal`. A reformat that wraps an entry over two
 * lines fails the suite -- which is the cheap failure. The expensive one is a status
 * the agent writes and this file has never heard of, drawn as a blank chip.
 *
 * `SUCCEEDED_UNVERIFIED` is `warn` and never `good` (invariant 6): the verification
 * veto is one-way, and a draft PR that looks green in the console undoes it. Only
 * `SUCCEEDED` may be `good`, and the test asserts that too -- including that the
 * stylesheet does not quietly resolve `--tone-warn` to the same colour.
 */

export const TONES = ["idle", "live", "good", "warn", "bad"];

export const STATUS = {
  QUEUED:               { label: "Queued",               tone: "idle", terminal: false },
  DISPATCHED:           { label: "Dispatched",           tone: "live", terminal: false },
  CHECKING_OUT:         { label: "Checking out",         tone: "live", terminal: false },
  PLANNING:             { label: "Planning",             tone: "live", terminal: false },
  CODING:               { label: "Coding",               tone: "live", terminal: false },
  VERIFYING:            { label: "Verifying",            tone: "live", terminal: false },
  REVIEWING:            { label: "Reviewing",            tone: "live", terminal: false },
  SUBMITTING:           { label: "Submitting",           tone: "live", terminal: false },
  SUCCEEDED:            { label: "Succeeded",            tone: "good", terminal: true },
  SUCCEEDED_UNVERIFIED: { label: "Draft, not verified",  tone: "warn", terminal: true },
  FAILED_VERIFICATION:  { label: "Failed verification",  tone: "bad",  terminal: true },
  FAILED:               { label: "Failed",               tone: "bad",  terminal: true },
  CANCELLED:            { label: "Cancelled",            tone: "idle", terminal: true },
};

/** Whether the run has stopped. The poller's stop condition, and the watchdog's. */
export function isTerminal(status) {
  return STATUS[status]?.terminal === true;
}

/**
 * How to draw a status, including one this bundle has never heard of.
 *
 * An unknown name renders itself rather than blanking the chip -- the same
 * degrade-don't-500 posture `cards._STATUS_COLUMN` takes on the server. The console is
 * deployed separately from the agent, so it *will* be a version behind at some point,
 * and a board that goes blank is a worse failure than one that shows `RESEARCHING`.
 */
export function describe(status) {
  return STATUS[status] ?? { label: status || "No run", tone: "idle", terminal: false };
}
