/**
 * The board: one repository's cards, in the five columns `api/src/handlers/cards.py`
 * draws, with one poll behind them.
 *
 * Three things here are worth more than the rest of the file.
 *
 * 1. **The board decides nothing.** `transitions.moveRefusal` produces the *reason* a
 *    move would be refused so a control can carry it before the request, and
 *    `cards.move` is still the only enforcer. When the two disagree the server's
 *    sentence wins and is toasted verbatim.
 * 2. **An optimistic placement holds two values, not one.** The column the operator
 *    dropped on is what the board draws; the column that was posted is what a failure
 *    quotes. They agree at the moment of the drop and stop agreeing as soon as the
 *    server answers -- a `PLANNING` drop is *stored* as `ACTIVE` and the column that
 *    comes back is derived from the run's own status, which may already be `CODING`.
 * 3. **A card is never dropped.** A column this bundle does not draw (the `ACTIVE`
 *    fall-through `cards.py` documents, or a column added to the server and not here)
 *    lands in the "Unplaced" strip above the board with its raw value showing. A card
 *    that silently vanishes off a board is the failure nothing fails on.
 *
 * Every drop target has an equivalent button, so the board is fully operable from the
 * keyboard: drag-and-drop is the enhancement, never the only path.
 */

import * as activity from "../activity.js";
import * as api from "../api.js";
import * as configs from "../configs.js";
import * as status from "../status.js";
import * as transitions from "../transitions.js";
import * as ui from "../ui.js";

// `GET /cards`'s own maximum page. The route answers newest-first, so a truncated board
// is the newest N cards -- and it says so on screen, because a silent cap reads as "that
// is all of them".
const PAGE = 100;

// Named from the tuples rather than spelled out, the same way `cards.py` names them, so a
// column that changes sides cannot leave this file testing against a stale string.
const [BACKLOG, , DONE] = transitions.STORED_COLUMNS;
const [PLANNING] = transitions.DERIVED_COLUMNS;

const PENDING_REASON = "a move is already in flight for this card";

// What an empty column says. Never just "no cards": an empty column is either a fact
// about the runs (nothing is coding) or an invitation (add a card).
const COLUMN_EMPTY = {
  BACKLOG: "Nothing in the backlog yet -- write a card above.",
  PLANNING: "No run is planning.",
  CODING: "No run is writing code.",
  VERIFYING: "No run is being verified.",
  DONE: "Nothing finished yet.",
};

function cardHash(cardId) {
  return `#/cards/${encodeURIComponent(cardId)}`;
}

function taskHash(configId, taskId) {
  return `#/tasks/${encodeURIComponent(configId)}/${encodeURIComponent(taskId)}`;
}

export function mount(root, { configId }) {
  ui.empty(root);

  // The last thing the server said about each card, newest first. A pending move never
  // writes into these objects -- it is held beside them in `pending`, so a failure can
  // restore the server's version by simply dropping the pending entry.
  let cards = [];
  let truncated = false;
  let configsLoaded = false;
  let failure = null;
  // `cardId -> {postedTarget, shownColumn}`.
  const pending = new Map();
  // The card being dragged. `dataTransfer` is unreadable during `dragover` (protected
  // mode), and the refusal check needs the card to decide whether to highlight, so the id
  // is kept here between `dragstart` and `drop` as well as in the drag payload.
  let dragging = null;
  // The signature the board was last drawn from, so a poll that changes nothing does not
  // rebuild identical nodes -- see `apply`.
  let drawn = null;

  // -- the shell, built once ---------------------------------------------------------

  const title = ui.el("h2", { class: "board-title" });
  // The repository lifecycle is `configurations.CONFIG_STATUSES`, not the run vocabulary
  // in `status.js` -- a repository is not a run. The sidebar owns the full tone map; here
  // the status is written as text next to the name, which is what an operator needs while
  // a repository is still provisioning.
  const repoStatus = ui.el("span", { class: "muted" });
  const settings = ui.el(
    "a",
    { class: "btn", href: `#/register/${encodeURIComponent(configId)}` },
    "Settings",
  );
  const head = ui.el("div", { class: "board-head" }, [
    title,
    repoStatus,
    ui.el("span", { class: "spacer" }),
    settings,
  ]);

  const notes = ui.el("div", { class: "stack" });
  const unplacedHost = ui.el("div", {});
  const board = ui.el("div", { class: "board" });

  const composerText = ui.el("textarea", {
    rows: "2",
    "aria-label": "New card",
    placeholder: "What needs doing? The first line becomes the title.",
  });
  composerText.addEventListener("keydown", (event) => {
    // The composer sits open at the top of Backlog so adding a card is one click; the
    // keyboard shortcut makes it none.
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault();
      addCard();
    }
  });
  // The primary of the pair, because it is the fuller action: one field, two endings. It
  // is the only control on the board that carries `dispatchRefusal` -- see `refreshHead`.
  const addAndStartButton = ui.action("Add and start", {
    class: "btn btn-primary btn-small",
    onClick: addAndStart,
  });
  const composer = ui.el("div", { class: "composer" }, [
    composerText,
    ui.el("div", { class: "row" }, [
      ui.action("Add card", { class: "btn btn-secondary btn-small", onClick: addCard }),
      addAndStartButton,
      ui.el(
        "span",
        { class: "composer-hint" },
        "Add card leaves it in the backlog; Add and start claims it and runs it now. " +
          "Ctrl/Cmd + Enter does the cheap one.",
      ),
    ]),
  ]);

  const columns = transitions.BOARD_COLUMNS.map(buildColumn);
  board.append(...columns.map((slot) => slot.node));
  root.append(head, notes, unplacedHost, board);

  function buildColumn(column) {
    const live = transitions.DERIVED_COLUMNS.includes(column);
    const count = ui.el("span", { class: "column-count" });
    // Replaced on every draw. The composer and the drop listeners live on the body, which
    // is not, so neither a half-typed card nor a drag survives a redraw by accident.
    const host = ui.el("div", { class: "column-cards stack" });
    const body = ui.el("div", { class: "column-body" });
    if (column === BACKLOG) body.append(composer);
    body.append(host);
    dropTarget(body, column);

    const node = ui.el("section", { class: live ? "column is-live" : "column" }, [
      ui.el("div", { class: "column-head" }, [
        // Decorative: the column's name is written beside it, so the pulsing dot is not
        // carrying any information of its own.
        live ? ui.el("span", { class: "dot tone-live", "aria-hidden": "true" }) : null,
        ui.el("span", {}, transitions.columnLabel(column)),
        count,
      ]),
      body,
    ]);
    return { column, node, count, host };
  }

  // -- state ------------------------------------------------------------------------

  function byId(cardId) {
    return cards.find((card) => card.cardId === cardId) || null;
  }

  /** The bucket a card is drawn in: the optimistic placement while one is in flight, and
   * the server's derived column otherwise. */
  function shownColumn(card) {
    return pending.get(card.cardId)?.shownColumn ?? card.column;
  }

  /** Everything the board's appearance depends on, in one string. Compared against the
   * last draw so an unchanged poll costs nothing, and returned to the poll so an idle
   * board backs off to 30 s. */
  function signature() {
    const rows = cards.map((card) =>
      [card.cardId, shownColumn(card), card.status, card.prUrl, card.updatedAt].join("|"),
    );
    return `${rows.join(",")}#${truncated}`;
  }

  function replace(card) {
    if (byId(card.cardId)) cards = cards.map((row) => (row.cardId === card.cardId ? card : row));
    else cards = [card, ...cards];
  }

  function merge(page) {
    const incoming = page?.cards || [];
    // A card with a move in flight keeps the copy the board already has: the poll's
    // answer was read before the move landed, so applying it would snap the card back to
    // where it was for one tick and then forward again.
    const held = new Map(
      cards.filter((card) => pending.has(card.cardId)).map((card) => [card.cardId, card]),
    );
    const seen = new Set(incoming.map((card) => card.cardId));
    cards = incoming.map((card) => held.get(card.cardId) || card);
    for (const [cardId, card] of held) if (!seen.has(cardId)) cards.unshift(card);
    truncated = Boolean(page?.nextCursor);
  }

  // -- refusals ---------------------------------------------------------------------

  function refusalFor(card, target) {
    if (pending.has(card.cardId)) return PENDING_REASON;
    return transitions.moveRefusal(card, target, configs.get(card.configId || configId));
  }

  /** Whether a run can be dispatched at all right now. Asked of `moveRefusal` with a
   * notional backlog card rather than repeating the readiness sentence: a run started
   * from the composer is dispatched under exactly the conditions a `PLANNING` drop is. */
  function dispatchRefusal() {
    const notional = { configId, storedColumn: BACKLOG };
    return transitions.moveRefusal(notional, PLANNING, configs.get(configId));
  }

  function liveRun(card) {
    return Boolean(card.taskId) && Boolean(card.status) && !status.isTerminal(card.status);
  }

  // -- writes -----------------------------------------------------------------------

  async function move(card, target) {
    // The posted target and the drawn bucket are recorded apart because they are not the
    // same fact: one is what the server was asked for, the other is where the operator
    // just put the card. See the header.
    pending.set(card.cardId, { postedTarget: target, shownColumn: target });
    render();
    try {
      const updated = await api.moveCard(card.cardId, target);
      // Wholesale, not field by field: the answer carries both `column` (derived from the
      // run) and `storedColumn` (the operator's own value), and a merge would keep
      // whichever of the two the local copy happened to have.
      replace(updated);
      pending.delete(card.cardId);
      render();
      poll.wake();
    } catch (error) {
      const posted = pending.get(card.cardId)?.postedTarget ?? target;
      // Drop the pending entry and the last server-known card is what draws again -- it
      // was never overwritten.
      pending.delete(card.cardId);
      render();
      ui.toast(`the move to ${posted} was refused: ${error.message}`, "bad");
    }
  }

  async function addCard() {
    const description = composerText.value.trim();
    if (!description) {
      ui.toast("a card needs a description -- the first line becomes its title", "warn");
      composerText.focus();
      return;
    }
    try {
      // No `title`: `cards.create` takes the first non-empty line, so the composer is one
      // field rather than two.
      const created = await api.createCard({ configId, description });
      composerText.value = "";
      replace(created);
      render();
      poll.wake();
    } catch (error) {
      ui.toast(error.message, "bad");
    }
  }

  async function addAndStart() {
    const prompt = composerText.value.trim();
    if (!prompt) {
      ui.toast("a run needs a prompt -- the first line becomes the card's title", "warn");
      composerText.focus();
      return;
    }
    try {
      await api.createTask({ configId, prompt });
      composerText.value = "";
      // No navigation: `POST /tasks` creates the card too, so the next poll draws it in
      // Planning with its chip already linking to the run.
      poll.wake();
      ui.toast("the run has started -- its card is in Planning", "good");
    } catch (error) {
      ui.toast(error.message, "bad");
    }
  }

  async function remove(card) {
    try {
      await api.deleteCard(card.cardId);
      cards = cards.filter((row) => row.cardId !== card.cardId);
      pending.delete(card.cardId);
      render();
      poll.wake();
    } catch (error) {
      ui.toast(error.message, "bad");
    }
  }

  // -- drag and drop ----------------------------------------------------------------

  function dropTarget(node, column) {
    node.addEventListener("dragover", (event) => {
      // Always accepted, even when the move would be refused: a drop that is quietly
      // ignored teaches nothing, and the refusal is the message worth having.
      event.preventDefault();
      const card = dragging ? byId(dragging) : null;
      node.classList.toggle("is-over", Boolean(card) && !refusalFor(card, column));
    });
    node.addEventListener("dragleave", () => node.classList.remove("is-over"));
    node.addEventListener("drop", (event) => {
      event.preventDefault();
      node.classList.remove("is-over");
      const card = byId(event.dataTransfer?.getData("text/plain") || dragging || "");
      if (card) attemptMove(card, column);
    });
  }

  function attemptMove(card, column) {
    const refusal = refusalFor(card, column);
    if (refusal) {
      ui.toast(refusal, "warn");
      return;
    }
    move(card, column);
  }

  // -- rendering --------------------------------------------------------------------

  function moveAction(label, card, target, extra = {}) {
    return ui.action(label, {
      class: `btn btn-small ${extra.class || ""}`.trim(),
      refusal: refusalFor(card, target),
      onClick: () => move(card, target),
    });
  }

  function deleteAction(card) {
    const node = ui.armedButton("Delete", {
      subject: card.title || card.cardId,
      verb: "delete",
      onConfirm: () => remove(card),
    });
    node.classList.add("btn-small");
    // `cards.delete` refuses this too, with `RunInFlight`. The reason is carried here as
    // well so the button says why before it is pressed rather than after.
    ui.setRefusal(
      node,
      pending.has(card.cardId)
        ? PENDING_REASON
        : liveRun(card)
          ? `the run is still ${card.status} -- deleting the card would not stop the ` +
            "container, it would only lose your handle on it."
          : null,
    );
    return node;
  }

  function cardNode(card) {
    const held = pending.get(card.cardId);
    const stored = card.storedColumn || BACKLOG;
    const chip = ui.chip(card.status);

    const node = ui.el("article", { class: "card", draggable: "true" }, [
      ui.el("a", { class: "card-title", href: cardHash(card.cardId) }, card.title || card.cardId),
      ui.el("div", { class: "card-meta" }, [
        // The chip is the link to the run: watching a run is one click from the board.
        card.taskId ? ui.el("a", { href: taskHash(configId, card.taskId) }, chip) : chip,
        ui.relTime(card.updatedAt),
      ]),
      held
        ? ui.el(
            "p",
            { class: "card-note" },
            `moving to ${transitions.columnLabel(held.postedTarget)}...`,
          )
        : null,
      ui.el("div", { class: "card-actions" }, [
        // A button is only drawn where it means something: `Start run` on a card that is
        // not in Backlog would carry a refusal an operator can do nothing about, and the
        // refusals that *are* actionable (not ready, run still live) are the ones left on
        // screen.
        stored === BACKLOG
          ? moveAction("Start run", card, PLANNING, { class: "btn-primary" })
          : moveAction("Park", card, BACKLOG),
        stored === DONE ? null : moveAction("Done", card, DONE),
        card.prUrl ? ui.link(card.prUrl, "Open PR") : null,
        deleteAction(card),
      ]),
    ]);
    if (held) node.setAttribute("data-pending", "true");

    node.addEventListener("dragstart", (event) => {
      dragging = card.cardId;
      node.classList.add("is-dragging");
      if (event.dataTransfer) {
        event.dataTransfer.setData("text/plain", card.cardId);
        event.dataTransfer.effectAllowed = "move";
      }
    });
    node.addEventListener("dragend", () => {
      dragging = null;
      node.classList.remove("is-dragging");
    });
    return node;
  }

  /** A card whose column the board does not draw. Loud, above the board, with the raw
   * value -- the alternative is a card that is simply not there. */
  function unplacedNode(rows) {
    return ui.el("div", { class: "unplaced" }, [
      ui.el("p", { class: "unplaced-title" }, `Unplaced (${rows.length})`),
      ui.el(
        "p",
        { class: "card-note" },
        "these cards report a column this console does not draw. They are shown here " +
          "rather than dropped; the raw value is on each card.",
      ),
      ...rows.map((card) => {
        const node = cardNode(card);
        node.append(ui.el("p", { class: "card-note" }, `column: ${shownColumn(card)}`));
        return node;
      }),
    ]);
  }

  function renderNotes() {
    const config = configs.get(configId);
    ui.empty(notes);
    if (configsLoaded && !config) {
      notes.append(
        ui.banner("bad", `no repository with the id ${configId} is registered any more.`),
      );
    }
    if (config && config.status !== "READY") {
      notes.append(
        ui.banner(
          "warn",
          `${config.repository || configId} is ${config.status}. Cards can be written now; a ` +
            "run can only be dispatched against a READY repository.",
        ),
      );
    }
    if (truncated) {
      // A silent cap reads as "that is all of them".
      notes.append(
        ui.el(
          "p",
          { class: "faint" },
          `the newest ${cards.length} cards are shown -- older cards not shown.`,
        ),
      );
    }
    if (failure) notes.append(ui.el("p", { class: "faint tone-bad" }, failure));
  }

  function refreshHead() {
    ui.text(title, configs.repository(configId));
    ui.text(repoStatus, configs.get(configId)?.status || "");
    // One control now, not two: `Add card` writes a Backlog card against a repository in
    // any state, so only `Add and start` carries the readiness refusal.
    ui.setRefusal(addAndStartButton, dispatchRefusal());
  }

  function render() {
    const buckets = new Map(transitions.BOARD_COLUMNS.map((column) => [column, []]));
    const unplaced = [];
    for (const card of cards) {
      const column = shownColumn(card);
      if (buckets.has(column)) buckets.get(column).push(card);
      else unplaced.push(card);
    }

    for (const slot of columns) {
      const rows = buckets.get(slot.column);
      ui.text(slot.count, rows.length);
      ui.empty(slot.host);
      if (!rows.length) {
        const note = COLUMN_EMPTY[slot.column] || "Nothing here.";
        slot.host.append(ui.el("p", { class: "column-empty" }, note));
      }
      for (const card of rows) slot.host.append(cardNode(card));
    }

    ui.empty(unplacedHost);
    if (unplaced.length) unplacedHost.append(unplacedNode(unplaced));

    refreshHead();
    renderNotes();
    drawn = signature();
  }

  // -- the poll ---------------------------------------------------------------------

  const poll = activity.createPoll({
    load: () => api.listCards(configId, { limit: PAGE }),
    apply: (page) => {
      const recovered = failure !== null;
      failure = null;
      merge(page);
      const next = signature();
      // Only redraw when something actually changed. A poll that rebuilds identical nodes
      // every six seconds disarms an armed Delete button and takes the focus off whatever
      // the operator was reaching for -- the same reason the activity pulse writes in
      // place instead of re-rendering.
      if (next !== drawn) render();
      else if (recovered) renderNotes();
      return next;
    },
    onError: (error) => {
      // A banner rather than a toast: this is something that is true until it is not,
      // and a failing poll would otherwise toast every six seconds.
      failure = error.message;
      renderNotes();
    },
    immediate: true,
  });

  render();
  configs
    .load()
    .then(() => {
      configsLoaded = true;
      refreshHead();
      renderNotes();
    })
    .catch((error) => {
      failure = error.message;
      renderNotes();
    });

  return {
    destroy() {
      poll.stop();
      ui.empty(root);
    },
  };
}
