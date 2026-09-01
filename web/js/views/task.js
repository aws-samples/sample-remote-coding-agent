/**
 * One run: what it was asked to do, where it got to, and its activity feed as it arrives.
 *
 * Two timers, and the difference between them is the whole design of this view.
 *
 * * A `createPoll` over `GET /tasks/{taskId}` keeps the header honest. It backs off to
 *   30 s while nothing changes, so a finished run costs two requests a minute.
 * * A `createPulse` over `GET /tasks/{taskId}/activity` appends rows every 5 s and
 *   rewrites the status, the row count and the feed's own note **in place**. It never
 *   re-renders the table: a feed that rebuilt itself every five seconds would throw away
 *   hover, the focus, the scroll position and a selection an operator was reading, which
 *   is what makes a polled page feel hostile. That is also why nothing under `web/js/`
 *   builds a node from an HTML string -- the in-place rule is structural rather than a
 *   convention.
 *
 * Both stop when the feed reports `done`. The pulse stops itself only after a `done` page
 * that added nothing, because the last rows of a run are written in the same breath as its
 * terminal status; the header gets one more read after that and then stops too.
 *
 * There is no cancel button, and no message here offers one: the console has no cancel
 * control yet, and a button that is not there must not be implied.
 */

import * as activity from "../activity.js";
import * as api from "../api.js";
import * as configs from "../configs.js";
import * as status from "../status.js";
import * as ui from "../ui.js";

// `tasks._MAX_ACTIVITY_LIMIT` is 200; a page of 100 is two requests for a long run's
// backlog of rows and one for everything after that.
const FEED_PAGE = 100;

// The step outcomes `cards._step_entry` reads, which is the only vocabulary the feed's
// `steps` list carries. Anything else (including today's bare node name, which carries no
// outcome at all) is a step that was entered and not yet judged.
const STEP_FAILED = "FAILED";
const STEP_SKIPPED = "SKIPPED";

function cardHash(cardId) {
  return `#/cards/${encodeURIComponent(cardId)}`;
}

function boardHash(configId) {
  return `#/boards/${encodeURIComponent(configId)}`;
}

/** One `steps` entry as `{name, outcome}`. Two shapes on purpose, the same two
 * `cards._step_entry` reads: `store.set_status` appends a bare node name today, and a
 * mapping is what a per-step outcome will look like when the agent writes one. */
function stepEntry(entry) {
  if (entry && typeof entry === "object") {
    return {
      name: String(entry.step || ""),
      outcome: String(entry.status || "")
        .trim()
        .toUpperCase(),
    };
  }
  return { name: String(entry || ""), outcome: "" };
}

/** What one activity row is, in a word. `kind` is the feed's own discriminator
 * (`agent/src/coding_agent/activity.py`): `tool`, `text`, `result`, `node`. */
function eventOf(row) {
  if (row.kind === "tool") return row.tool || "tool";
  if (row.kind === "node") return `node ${row.phase || ""}`.trim();
  return String(row.kind || "");
}

/** And what it says. An unrecognised `kind` still renders its `detail` rather than an
 * empty cell -- the same degrade-don't-blank posture as an unknown status. */
function detailOf(row) {
  if (row.kind === "result") {
    const turns = `${row.num_turns ?? "?"} turns`;
    return row.is_error ? `${turns}, ended in an error` : turns;
  }
  if (row.kind === "node") {
    const iteration = `iteration ${row.iteration ?? 0}`;
    return row.seconds === undefined ? iteration : `${iteration}, ${row.seconds}s`;
  }
  return String(row.detail || "");
}

export function mount(root, { configId, taskId }) {
  ui.empty(root);

  let task = null;
  let failure = null;
  let feedFailure = null;
  // The number of feed rows already shown. `nextCursor` is a row count rather than an
  // opaque token (`tasks.activity_page`), so a poller only ever carries an integer.
  let cursor = 0;
  let shown = 0;
  // Set on the first `done` page. The header poll stops on its next answer, so a terminal
  // status that arrived a moment before `prUrl` did is still followed by one more read.
  let drained = false;
  // The signature the summary was last drawn from -- an unchanged answer must not rebuild
  // the region, for the same reason the feed appends rather than re-renders.
  let drawn = null;
  let headPoll = null;

  // -- the shell, built once ---------------------------------------------------------

  const crumb = ui.el("div", { class: "row" });
  const notes = ui.el("div", { class: "stack" });
  const summary = ui.el("div", { class: "stack" });

  // The three nodes the pulse writes into. They are built once and never replaced: a
  // rebuilt chip is a rebuilt node, and the point of the pulse is that nothing in the
  // feed's region moves under the operator.
  const feedStatus = ui.el("span", { class: "chip tone-idle" }, "--");
  const feedCount = ui.el("span", { class: "faint" }, "0 rows");
  const feedNote = ui.el("span", { class: "faint" }, "waiting for the first page...");
  const rows = ui.el("tbody", {});
  const feed = ui.panel("Activity", [
    ui.el("div", { class: "row" }, [
      feedStatus,
      feedCount,
      feedNote,
      ui.el("span", { class: "spacer" }),
      ui.el(
        "span",
        { class: "composer-hint" },
        "appended in place every 5 seconds; it stops when the run is finished and the " +
          "feed is drained",
      ),
    ]),
    ui.el("div", { class: "table-wrap" }, [
      ui.el("table", {}, [
        ui.el("thead", {}, [
          ui.el("tr", {}, [
            ui.el("th", { scope: "col" }, "When"),
            ui.el("th", { scope: "col" }, "Node"),
            ui.el("th", { scope: "col" }, "Event"),
            ui.el("th", { scope: "col" }, "Detail"),
          ]),
        ]),
        rows,
      ]),
    ]),
  ]);

  root.append(crumb, notes, summary, feed);

  // -- in-place writes ----------------------------------------------------------------

  /** Rewrite a chip without replacing it: the tone is a class and the label is text, so
   * both can be assigned to a node that is already on screen and already hovered. */
  function applyChip(node, name) {
    const { label, tone } = status.describe(name);
    node.className = `chip tone-${tone}`;
    if (name) node.title = name;
    return ui.text(node, label);
  }

  function rowNode(row) {
    return ui.el("tr", { class: row.is_error ? "activity-row is-error" : "activity-row" }, [
      ui.el("td", { class: "activity-when" }, ui.relTime(row.at)),
      ui.el("td", { class: "activity-kind" }, String(row.node || "")),
      ui.el("td", { class: "activity-kind" }, eventOf(row)),
      ui.el("td", { class: "activity-detail" }, detailOf(row)),
    ]);
  }

  // -- the summary --------------------------------------------------------------------

  function signature() {
    if (!task) return "none";
    const verify = task.verifyResult || {};
    return [
      task.status,
      task.prUrl,
      task.resumeCount,
      task.updatedAt,
      (task.steps || []).length,
      verify.exitCode,
      verify.verified,
      verify.vetoed,
      task.logs?.consoleUrl,
    ].join("|");
  }

  function kv(pairs) {
    const node = ui.el("div", { class: "kv" });
    for (const [key, value] of pairs) {
      if (value === null || value === undefined || value === "") continue;
      node.append(ui.el("span", { class: "kv-key" }, key), ui.el("div", { class: "kv-value" }, value));
    }
    return node;
  }

  /** The `steps` list, in the order the run appended it. Not built from a fixed list of
   * node names: a run that was resumed enters a step twice and the feed's own order is
   * the only record of that, so a fixed timeline would show the second pass as the
   * first. */
  function timeline() {
    const entries = (task.steps || []).map(stepEntry).filter((entry) => entry.name);
    if (!entries.length) {
      return ui.el("p", { class: "faint" }, "no step has been recorded yet.");
    }
    const live = !status.isTerminal(task.status);
    return ui.el(
      "div",
      { class: "timeline" },
      entries.map((entry, index) => {
        const last = index === entries.length - 1;
        let extra = "";
        if (entry.outcome === STEP_FAILED) extra = " is-failed";
        else if (entry.outcome === STEP_SKIPPED) extra = " is-skipped";
        else if (last && live) extra = " is-current";
        return ui.el("span", { class: `timeline-step${extra}` }, [
          entry.name,
          // The outcome is written out rather than left to the border colour: nothing here
          // conveys state by colour alone.
          entry.outcome ? ui.el("span", { class: "faint" }, entry.outcome.toLowerCase()) : null,
        ]);
      }),
    );
  }

  /** The three-way verification signal, and it is deliberately not collapsed into one
   * word. `verified` is "a real command ran and was judged green", `warning` is "nothing
   * ran, or nothing judged it", and `vetoed` is the reviewer's one-way veto -- a view that
   * showed only a tick would undo exactly that (invariant 6). */
  function verification() {
    const verify = task.verifyResult;
    if (!verify || typeof verify !== "object") {
      return ui.el("p", { class: "faint" }, "verification has not run yet.");
    }
    return ui.el("div", { class: "stack" }, [
      verify.vetoed
        ? ui.banner(
            "bad",
            "the reviewer vetoed this change. A veto is one-way: nothing downstream can " +
              "overturn it, and the pull request stays a draft.",
          )
        : null,
      verify.warning ? ui.banner("warn", String(verify.warning)) : null,
      kv([
        ["command", verify.command ? ui.el("code", {}, String(verify.command)) : null],
        [
          "exit code",
          verify.exitCode === null || verify.exitCode === undefined
            ? "not recorded"
            : String(verify.exitCode),
        ],
        ["summary", verify.summary ? String(verify.summary) : null],
        ["vetoed", verify.vetoed ? "yes" : "no"],
      ]),
    ]);
  }

  function renderSummary() {
    ui.empty(summary);
    if (!task) {
      summary.append(ui.el("p", { class: "faint" }, "loading the run..."));
      return;
    }
    const logs = task.logs || {};
    summary.append(
      ui.el("div", { class: "row-between" }, [
        ui.el("div", { class: "row" }, [
          ui.chip(task.status),
          ui.el("h2", { class: "board-title" }, configs.repository(configId)),
          ui.relTime(task.updatedAt),
        ]),
        ui.el("div", { class: "row" }, [
          task.prUrl ? ui.link(task.prUrl, "Open pull request") : null,
          // The log stream, derived from stored attributes by `observability.log_locations`
          // -- an empty object while the repository has no runtime yet, which is a missing
          // link rather than a broken one.
          logs.consoleUrl ? ui.link(logs.consoleUrl, "CloudWatch logs") : null,
        ]),
      ]),
      ui.panel("Prompt", [ui.el("pre", {}, String(task.prompt || ""))]),
      ui.panel("Where it got to", [
        timeline(),
        kv([
          ["run", ui.el("span", { class: "mono" }, task.taskId || taskId)],
          [
            "card",
            task.cardId
              ? ui.el("a", { href: cardHash(task.cardId) }, task.cardId)
              : "none -- an old run, from before every run had a card",
          ],
          ["source", task.source ? String(task.source) : null],
          [
            "resumed",
            // A resume is the watchdog picking a run back up, and it is worth saying out
            // loud: a run on its third attempt reads very differently from a fresh one.
            `${task.resumeCount || 0} time(s)`,
          ],
          ["feed", `${task.activityCount ?? 0} row(s) recorded`],
          ["session", logs.sessionId ? ui.el("span", { class: "mono" }, logs.sessionId) : null],
        ]),
      ]),
      ui.panel("Verification", [verification()]),
    );
    drawn = signature();
  }

  function renderCrumb() {
    ui.empty(crumb);
    crumb.append(
      ui.el("a", { class: "btn btn-quiet btn-small", href: boardHash(configId) }, "Back to board"),
      ui.el("span", { class: "muted" }, configs.repository(configId)),
    );
  }

  function renderNotes() {
    ui.empty(notes);
    if (failure) notes.append(ui.banner("bad", failure));
    if (feedFailure) notes.append(ui.banner("warn", `the activity feed: ${feedFailure}`));
  }

  // -- the two timers -----------------------------------------------------------------

  headPoll = activity.createPoll({
    load: () => api.getTask(configId, taskId),
    apply: (fresh) => {
      const recovered = failure !== null;
      failure = null;
      task = fresh;
      const next = signature();
      if (next !== drawn) renderSummary();
      if (recovered) renderNotes();
      if (drained && status.isTerminal(task.status)) {
        // The feed has reported `done` and this read confirms it. A terminal run's header
        // cannot change again, so the poll stops itself -- the property a repeating timer
        // does not have, and the reason both timers here are chained.
        headPoll.stop();
      }
      return next;
    },
    onError: (error) => {
      failure = error.message;
      renderNotes();
    },
    immediate: true,
  });

  const pulse = activity.createPulse({
    load: () => api.getActivity(configId, taskId, { cursor, limit: FEED_PAGE }),
    apply: (page) => {
      const recovered = feedFailure !== null;
      feedFailure = null;
      const fresh = page?.rows || [];
      // Appended, never replaced. Everything above this row is exactly the node it was
      // five seconds ago, which is what keeps hover, focus and a selection alive.
      for (const row of fresh) rows.append(rowNode(row));
      cursor = page?.nextCursor ?? cursor;
      shown += fresh.length;

      applyChip(feedStatus, page?.status);
      ui.text(feedCount, `${shown} row(s)`);
      ui.text(
        feedNote,
        page?.done
          ? "the run is finished and the feed is drained."
          : fresh.length >= FEED_PAGE
            ? "more rows are waiting; the next refresh appends them."
            : "live.",
      );
      if (recovered) renderNotes();
      if (page?.done && !drained) {
        // The header is read once more before it stops: `prUrl` and the terminal status are
        // written in the same breath and the order between them is not this view's to know.
        drained = true;
        headPoll.wake();
      }
    },
    onError: (error) => {
      // A banner rather than a toast: the pulse fires every five seconds, and a toast per
      // failure would bury the screen in the same sentence.
      feedFailure = error.message;
      renderNotes();
    },
  });

  renderCrumb();
  renderSummary();
  // The pulse arms itself rather than firing at once, and five seconds of an empty table
  // reads as a run that recorded nothing. `wake()` is the same code path the timer takes.
  pulse.wake();
  configs
    .load()
    .then(() => {
      renderCrumb();
      if (task) renderSummary();
    })
    .catch(() => {
      // The run view works without the configuration cache: it only loses the
      // repository's name, and `configs.repository` falls back to the id.
    });

  return {
    destroy() {
      headPoll.stop();
      pulse.stop();
      ui.empty(root);
    },
  };
}
