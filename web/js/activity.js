/**
 * The two timers that make the console feel alive, and the only ones besides the clock
 * in `ui.js`.
 *
 * Both are chained `setTimeout`, and the repeating-timer API is deliberately absent from
 * this whole directory (a test asserts its name appears nowhere). That is not a style
 * preference: a chained poll decides for itself whether there will be a next tick, so
 * `stop()` is reliable and a view's timer cannot outlive its DOM. A repeating timer
 * keeps firing into a torn-down view, and the requests it makes still cost money and
 * still write into nodes nobody is looking at. It also stacks: a period shorter than
 * the request it makes overlaps itself, which is how a slow board ends up making four
 * concurrent reads of the same page.
 *
 * Both skip the request entirely while `document.hidden` -- re-arming without fetching
 * rather than fetching and not rendering -- and both wake on `visibilitychange`, so a
 * tab that has been in the background for an hour is current within a moment of being
 * looked at rather than up to 30 s later.
 */

// 6 s while something is changing, doubling to 30 s while nothing is. A board with no
// live run is the common case and does not deserve ten requests a minute.
export const MIN_POLL_MS = 6000;
export const MAX_POLL_MS = 30000;
// The activity feed of a live run. Fixed rather than backed off: this is the one place
// an operator is watching lines appear, and a feed that slows down looks stalled.
export const PULSE_MS = 5000;

/**
 * A backing-off poll.
 *
 * `load()` fetches, `apply(payload)` renders and may return a signature string. The
 * delay doubles while the signature is unchanged and resets to `minMs` the moment it
 * changes or `wake()` is called -- so a user action (a move, a new card) gets a fast
 * confirmation and an idle board settles down. When `apply` returns nothing the whole
 * payload is used as the signature, which is right for a small JSON page and wrong for
 * a large one, so a view that polls something big returns its own.
 *
 * `onError` is called with the failure; the poll itself backs off and keeps going,
 * because a poll that stops on the first error is a page that silently goes stale.
 */
export function createPoll({
  load,
  apply,
  onError,
  minMs = MIN_POLL_MS,
  maxMs = MAX_POLL_MS,
  immediate = false,
}) {
  let delay = minMs;
  let timer = null;
  let stopped = false;
  let previous = null;

  function arm(ms) {
    if (stopped) return;
    timer = setTimeout(run, ms);
  }

  async function run() {
    timer = null;
    if (document.hidden) {
      arm(delay);
      return;
    }
    try {
      const payload = await load();
      // The view may have been destroyed while the request was in flight; writing into
      // its nodes now would resurrect a page the router has already replaced.
      if (stopped) return;
      const returned = apply(payload);
      const signature = typeof returned === "string" ? returned : JSON.stringify(payload ?? null);
      delay = signature === previous ? Math.min(delay * 2, maxMs) : minMs;
      previous = signature;
    } catch (error) {
      if (stopped) return;
      delay = Math.min(delay * 2, maxMs);
      onError?.(error);
    }
    arm(delay);
  }

  /** Poll now, and treat what comes back as a change. What a user action calls. */
  function wake() {
    if (stopped) return;
    delay = minMs;
    if (timer !== null) clearTimeout(timer);
    timer = null;
    run();
  }

  function onVisible() {
    if (!document.hidden) wake();
  }

  function stop() {
    stopped = true;
    if (timer !== null) clearTimeout(timer);
    timer = null;
    document.removeEventListener("visibilitychange", onVisible);
  }

  document.addEventListener("visibilitychange", onVisible);
  if (immediate) run();
  else arm(delay);
  return { stop, wake };
}

/**
 * The activity pulse: every `PULSE_MS`, append what is new.
 *
 * `apply(payload)` must write `textContent` in place and append rows -- it must never
 * re-render its container. That is what keeps hover, a focused input and an open menu
 * alive across a refresh of a feed that is growing every few seconds, and it is why
 * nothing under `web/js/` builds a node from an HTML string.
 *
 * Stops when the route reports `done: true` *and* the page it returned was empty, so
 * the feed is drained to the end first: the last rows of a run are written in the same
 * breath as its terminal status, and stopping on `done` alone loses them.
 */
export function createPulse({ load, apply, onError, everyMs = PULSE_MS }) {
  let timer = null;
  let stopped = false;

  function arm() {
    if (stopped) return;
    timer = setTimeout(run, everyMs);
  }

  async function run() {
    timer = null;
    if (document.hidden) {
      arm();
      return;
    }
    try {
      const payload = await load();
      if (stopped) return;
      apply(payload);
      if (payload?.done && (payload.rows?.length ?? 0) === 0) {
        // A terminal task's feed cannot grow, and this page added nothing to it. Stop
        // itself -- the property a repeating timer does not have.
        stop();
        return;
      }
    } catch (error) {
      if (stopped) return;
      onError?.(error);
    }
    arm();
  }

  function wake() {
    if (stopped) return;
    if (timer !== null) clearTimeout(timer);
    timer = null;
    run();
  }

  function onVisible() {
    if (!document.hidden) wake();
  }

  function stop() {
    stopped = true;
    if (timer !== null) clearTimeout(timer);
    timer = null;
    document.removeEventListener("visibilitychange", onVisible);
  }

  document.addEventListener("visibilitychange", onVisible);
  arm();
  return { stop, wake };
}
