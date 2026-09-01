/**
 * One card: its text, the run it claims, and the two moves an operator has.
 *
 * Three things here are worth explaining.
 *
 * 1. **The editor is never overwritten from under the operator.** A poll answers every
 *    six seconds and the fields are only re-seeded while they still hold exactly what
 *    the server last said -- so a half-typed description survives a refresh, and a
 *    change made in another tab still lands the moment this one is pristine again. The
 *    same reason the activity pulse writes in place rather than re-rendering.
 * 2. **The card's runs are found by filtering, and the bound is on screen.** `Tasks` is
 *    keyed `configId` + `taskId` and has no card index, so there is no route that asks
 *    "which runs belong to this card". The honest answer is the repository's newest
 *    runs, filtered -- and a filtered answer with a bound nobody is told about reads as
 *    the whole truth, so the sentence under the list says how far the search went.
 * 3. **The board decides nothing.** `transitions.moveRefusal` produces the *reason* a
 *    move would be refused so a control can carry it before the request; `cards.move`
 *    is still the only enforcer, and its sentence is what gets toasted when the two
 *    disagree.
 */

import * as activity from "../activity.js";
import * as api from "../api.js";
import * as configs from "../configs.js";
import * as status from "../status.js";
import * as transitions from "../transitions.js";
import * as ui from "../ui.js";

// `GET /tasks`'s own default page, and therefore how far back this view looks for the
// card's runs. Deliberately not the route's maximum: a card's runs are the newest ones or
// they are history, and the sentence on screen names the number actually searched.
const RUN_SEARCH = 25;

// Named from the tuples the way `cards.py` names them, so a column that changes sides
// cannot leave this file testing against a stale string.
const [BACKLOG, , DONE] = transitions.STORED_COLUMNS;
const [PLANNING] = transitions.DERIVED_COLUMNS;

function boardHash(configId) {
  return `#/boards/${encodeURIComponent(configId)}`;
}

function taskHash(configId, taskId) {
  return `#/tasks/${encodeURIComponent(configId)}/${encodeURIComponent(taskId)}`;
}

export function mount(root, { cardId }) {
  ui.empty(root);

  // The last thing the server said about this card. Every write replaces it wholesale --
  // `PATCH` and `POST /move` both answer with the merged view (`_with_column`), so a
  // field-by-field merge here would keep whichever half the local copy happened to have.
  let card = null;
  let runs = [];
  let searched = 0;
  let runsTruncated = false;
  let failure = null;
  // Whether the first answer has landed. The runs list is loaded from it once, and after
  // that only when a run appears that this view did not start itself.
  let loaded = false;
  // The signature the detail region was last drawn from: an unchanged poll must not
  // rebuild the action row, because that disarms an armed Delete and takes the focus off
  // whatever the operator was reaching for.
  let drawn = null;
  // What the server last said the text was. The editor is re-seeded from a poll only
  // while it still matches these, so an edit in progress is never thrown away.
  let serverTitle = "";
  let serverDescription = "";

  // -- the shell, built once ---------------------------------------------------------

  const crumb = ui.el("div", { class: "row" });
  const detail = ui.el("div", { class: "stack" });
  const notes = ui.el("div", { class: "stack" });
  const runsHost = ui.el("div", { class: "stack" });

  const titleField = ui.el("input", {
    type: "text",
    "aria-label": "Title",
    // `cards._TITLE_MAX`. A soft bound: the server truncates a generated title and this
    // stops an operator typing past the length their own title will be stored at.
    maxlength: "120",
  });
  const descriptionField = ui.el("textarea", { rows: "8", "aria-label": "Description" });
  const save = ui.action("Save", { class: "btn btn-primary", onClick: saveText });
  const revert = ui.action("Revert", {
    class: "btn",
    onClick: () => {
      seedEditor({ force: true });
      ui.toast("the fields are back to what the server has", "info");
    },
  });
  const editor = ui.panel("Text", [
    ui.el("div", { class: "form" }, [
      ui.el("label", { class: "field" }, [
        ui.el("span", { class: "field-label" }, "Title"),
        titleField,
      ]),
      ui.el("label", { class: "field" }, [
        ui.el("span", { class: "field-label" }, "Description"),
        descriptionField,
        ui.el(
          "span",
          { class: "field-hint" },
          "the title and the description are the only editable fields; a column changes " +
            "through a move, which is what claims the card and dispatches the run.",
        ),
      ]),
      ui.el("div", { class: "row" }, [save, revert]),
    ]),
  ]);

  root.append(crumb, notes, detail, editor, runsHost);

  // -- reads --------------------------------------------------------------------------

  function configId() {
    return card?.configId || null;
  }

  function refusalFor(target) {
    return transitions.moveRefusal(card, target, configs.get(configId()));
  }

  function liveRun() {
    return Boolean(card?.taskId) && Boolean(card?.status) && !status.isTerminal(card.status);
  }

  /** Everything the detail region's appearance depends on, in one string. */
  function signature() {
    if (!card) return "none";
    return [
      card.cardId,
      card.title,
      card.storedColumn,
      card.column,
      card.status,
      card.taskId,
      card.prUrl,
      card.updatedAt,
      configs.get(configId())?.status,
    ].join("|");
  }

  function pristine() {
    return titleField.value === serverTitle && descriptionField.value === serverDescription;
  }

  /** Re-seed the fields from the server's copy, but only while nothing is half-typed --
   * `force` is the Revert button, which is the operator asking for it. */
  function seedEditor({ force = false } = {}) {
    const nextTitle = card?.title || "";
    const nextDescription = card?.description || "";
    if (force || pristine()) {
      titleField.value = nextTitle;
      descriptionField.value = nextDescription;
    }
    serverTitle = nextTitle;
    serverDescription = nextDescription;
  }

  // -- writes -------------------------------------------------------------------------

  async function saveText() {
    const body = {};
    if (titleField.value.trim() !== serverTitle) body.title = titleField.value.trim();
    if (descriptionField.value !== serverDescription) body.description = descriptionField.value;
    if (!Object.keys(body).length) {
      // `cards.patch` answers `EmptyPatch` for this, but a round trip to be told nothing
      // changed is a request that did not need making.
      ui.toast("nothing has changed, so there is nothing to save", "warn");
      return;
    }
    try {
      card = await api.patchCard(cardId, body);
      seedEditor({ force: true });
      render();
      ui.toast("saved", "good");
      poll.wake();
    } catch (error) {
      ui.toast(error.message, "bad");
    }
  }

  async function move(target) {
    try {
      const updated = await api.moveCard(cardId, target);
      const dispatched = updated.taskId && updated.taskId !== card?.taskId;
      card = updated;
      seedEditor();
      render();
      poll.wake();
      // A `PLANNING` move is a dispatch, so the run it started is a row this list does
      // not have yet.
      if (dispatched) loadRuns();
    } catch (error) {
      ui.toast(`the move to ${target} was refused: ${error.message}`, "bad");
    }
  }

  async function remove() {
    const where = configId();
    try {
      await api.deleteCard(cardId);
      ui.toast("the card is deleted; its runs are kept as history", "good");
      // Back to the board rather than to a panel about a card that is not there any
      // more. The run history survives the card on purpose (`cards.delete` says so).
      window.location.hash = where ? boardHash(where) : "#/";
    } catch (error) {
      ui.toast(error.message, "bad");
    }
  }

  // -- rendering ----------------------------------------------------------------------

  function moveAction(label, target, extra = {}) {
    return ui.action(label, {
      class: `btn ${extra.class || ""}`.trim(),
      refusal: refusalFor(target),
      onClick: () => move(target),
    });
  }

  function deleteAction() {
    const node = ui.armedButton("Delete", {
      subject: card.title || card.cardId,
      verb: "delete",
      onConfirm: remove,
    });
    // `cards.delete` refuses this too, with `RunInFlight`. The reason is carried here as
    // well so the button says why before it is pressed rather than after.
    ui.setRefusal(
      node,
      liveRun()
        ? `the run is still ${card.status} -- deleting the card would not stop the ` +
            "container, it would only lose your handle on it."
        : null,
    );
    return node;
  }

  function runChip(taskId, taskStatus) {
    const chip = ui.chip(taskStatus);
    // The chip is the link to the run: watching a run is one click from anywhere it
    // appears.
    return taskId && configId() ? ui.el("a", { href: taskHash(configId(), taskId) }, chip) : chip;
  }

  function renderDetail() {
    ui.empty(detail);
    if (!card) return;
    const stored = card.storedColumn || BACKLOG;
    detail.append(
      ui.el("div", { class: "row-between" }, [
        ui.el("h2", { class: "board-title" }, card.title || card.cardId),
        ui.el("span", { class: "muted mono" }, card.cardId),
      ]),
      ui.el("div", { class: "row" }, [
        runChip(card.taskId, card.status),
        ui.el("span", { class: "muted" }, `${transitions.columnLabel(card.column)} column`),
        // Both halves, because they are not the same fact: `column` is the projection of
        // the run's own status and `storedColumn` is the operator's own value. Without the
        // second one a card whose run has not started is indistinguishable from a parked one.
        stored === card.column
          ? null
          : ui.el("span", { class: "faint" }, `stored as ${transitions.columnLabel(stored)}`),
        ui.relTime(card.updatedAt),
      ]),
      ui.el("div", { class: "row" }, [
        stored === BACKLOG
          ? moveAction("Start run", PLANNING, { class: "btn-primary" })
          : moveAction("Park", BACKLOG),
        stored === DONE ? null : moveAction("Done", DONE),
        card.prUrl ? ui.link(card.prUrl, "Open PR") : null,
        ui.el("span", { class: "spacer" }),
        deleteAction(),
      ]),
    );
  }

  function renderCrumb() {
    ui.empty(crumb);
    const where = configId();
    if (!where) return;
    crumb.append(
      ui.el("a", { class: "btn btn-quiet btn-small", href: boardHash(where) }, "Back to board"),
      ui.el("span", { class: "muted" }, configs.repository(where)),
    );
  }

  function renderNotes() {
    ui.empty(notes);
    if (failure) notes.append(ui.banner("bad", failure));
    const config = configs.get(configId());
    if (config && config.status !== "READY") {
      notes.append(
        ui.banner(
          "warn",
          `${config.repository || config.configId} is ${config.status}. The card can be ` +
            "edited now; a run can only be dispatched against a READY repository.",
        ),
      );
    }
  }

  function renderRuns() {
    ui.empty(runsHost);
    const rows = runs.map((task) =>
      ui.el("div", { class: "row" }, [
        runChip(task.taskId, task.status),
        ui.el("span", { class: "mono truncate" }, task.taskId),
        task.prUrl ? ui.link(task.prUrl, "Open PR") : null,
        ui.relTime(task.updatedAt || task.createdAt),
      ]),
    );
    const bound = ui.el(
      "p",
      { class: "faint" },
      // A filtered answer whose bound is not stated reads as the whole truth. This one is
      // a filter over one page, so the page is what gets named.
      `the newest ${searched} runs of this repository were searched` +
        (runsTruncated ? "; older runs were not." : "."),
    );
    runsHost.append(
      ui.panel(
        `Runs of this card (${runs.length})`,
        rows.length
          ? [ui.el("div", { class: "stack" }, rows), bound]
          : [
              ui.emptyState(
                card && (card.storedColumn || BACKLOG) === BACKLOG
                  ? "No run yet. Start run dispatches one against this card."
                  : "No run of this card was found in the page searched.",
              ),
              bound,
            ],
      ),
    );
  }

  function render() {
    renderCrumb();
    renderDetail();
    renderNotes();
    drawn = signature();
  }

  async function loadRuns() {
    const where = configId();
    if (!where) return;
    try {
      const page = await api.listTasks(where, { limit: RUN_SEARCH });
      const tasks = page?.tasks || [];
      searched = tasks.length;
      runsTruncated = Boolean(page?.nextCursor);
      // No index from a card to its runs exists, so this is a filter -- see the header.
      runs = tasks.filter((task) => task.cardId === cardId);
      renderRuns();
    } catch (error) {
      searched = 0;
      runs = [];
      ui.empty(runsHost);
      runsHost.append(
        ui.banner("bad", `the runs of this card could not be listed: ${error.message}`),
      );
    }
  }

  // -- the poll -----------------------------------------------------------------------

  const poll = activity.createPoll({
    load: () => api.getCard(cardId),
    apply: (fresh) => {
      const recovered = failure !== null;
      failure = null;
      const previousTask = card?.taskId;
      card = fresh;
      seedEditor();
      const next = signature();
      if (next !== drawn) render();
      else if (recovered) renderNotes();
      if (!loaded) {
        loaded = true;
        loadRuns();
      } else if (card.taskId && card.taskId !== previousTask) {
        // A run that appeared without this view asking for it -- another tab, or a drop on
        // the board -- is a row the list does not have.
        loadRuns();
      }
      return next;
    },
    onError: (error) => {
      // A banner rather than a toast: a failing poll would otherwise toast every six
      // seconds, and this is something that stays true until it is not.
      failure = error.message;
      renderNotes();
    },
    immediate: true,
  });

  // Drawn empty first so the region has its shape before the first answer lands; the
  // poll's `immediate` read is the load, so there is no second request for the same card.
  render();
  renderRuns();
  configs
    .load()
    .then(() => {
      renderCrumb();
      renderNotes();
      renderDetail();
    })
    .catch(() => {
      // The card view works without the configuration cache -- it only loses the
      // repository's name and the readiness banner, and `moveRefusal` skips the
      // readiness check rather than guessing it.
    });

  return {
    destroy() {
      poll.stop();
      ui.empty(root);
    },
  };
}
