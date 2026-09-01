/**
 * The client's mirror of the board vocabulary, and the honest refusal for a move.
 *
 * The four tuples are `api/src/handlers/cards.py`'s, and that file asks for exactly
 * this ("FOLLOW-UP (phase 10): the console should build its column list from this
 * tuple's order rather than repeating it"). JavaScript cannot import a Python tuple, so
 * the agreement is held by `tests/test_web_status_vocabulary.py`, which reads both.
 *
 * `moveRefusal` decides **nothing**. `cards.move` is the only enforcer -- a client that
 * picks its own column can put a card somewhere no run can follow it. What this
 * produces is the *reason* a move would be refused, before the request, so the button
 * can carry it as a readable refusal (see `ui.setRefusal`) instead of looking
 * available and then failing. The wording deliberately echoes the server's, because
 * the server's message is what gets toasted when the two disagree.
 */

import { isTerminal } from "./status.js";

// Left to right, the five columns the board draws. `ACTIVE` is stored and is not a
// column: it is what a won `PLANNING` claim leaves behind, and the projection is drawn
// in its place. A card whose column is not in here is drawn in the "Unplaced" strip
// rather than dropped -- see `views/board.js`.
export const BOARD_COLUMNS = ["BACKLOG", "PLANNING", "CODING", "VERIFYING", "DONE"];
// The operator's own value, the only thing the server stores.
export const STORED_COLUMNS = ["BACKLOG", "ACTIVE", "DONE"];
// Projections of the claimed run's status, computed on read and never stored.
export const DERIVED_COLUMNS = ["PLANNING", "CODING", "VERIFYING"];
// The only targets `POST /cards/{cardId}/move` accepts.
export const MOVE_TARGETS = ["PLANNING", "BACKLOG", "DONE"];

const [BACKLOG] = STORED_COLUMNS;
const [PLANNING] = DERIVED_COLUMNS;

// Built from the tuples rather than spelled out, so a column that changes sides cannot
// leave this sentence naming the wrong ones.
const PROJECTIONS = DERIVED_COLUMNS.filter((column) => !MOVE_TARGETS.includes(column));

/** The English label for a column. The vocabulary is SHOUTED; a board is not. */
export function columnLabel(column) {
  return column.charAt(0) + column.slice(1).toLowerCase().replace(/_/g, " ");
}

/** Whether a column is one of the five the board draws. */
export function isBoardColumn(column) {
  return BOARD_COLUMNS.includes(column);
}

/**
 * The human reason moving `card` to `target` would be refused, or `null`.
 *
 * `config` is the card's configuration (`configs.get(card.configId)`) and may be
 * missing while the cache is still loading, in which case the readiness check is
 * skipped rather than guessed -- the server is the enforcer, and a refusal invented
 * from a half-loaded cache would be a button that lies in the other direction.
 */
export function moveRefusal(card, target, config) {
  if (!MOVE_TARGETS.includes(target)) {
    return (
      `${PROJECTIONS.join(" and ")} are projections of the run's own status, not places a ` +
      `card can be put. Drop on ${columnLabel(PLANNING)} to start a run.`
    );
  }
  if (target === PLANNING) {
    if (config && config.status !== "READY") {
      return (
        `${config.repository || card.configId} is still ${config.status}; a run can only be ` +
        "dispatched against a READY repository."
      );
    }
    if ((card.storedColumn || BACKLOG) !== BACKLOG) {
      return `a run starts only from ${columnLabel(BACKLOG)}.`;
    }
    return null;
  }
  // `BACKLOG` is a park and `DONE` is the operator calling the work finished. Both are
  // refused while the run is live, for the reason the server gives: neither stops the
  // container, so the move would only lose the operator's handle on it. No offer to
  // cancel -- the console has no cancel control yet, and a button that is not there must
  // not be implied by a refusal message.
  if (card.taskId && card.status && !isTerminal(card.status)) {
    return (
      `the run is still ${card.status} -- a card cannot be moved while its run is live; ` +
      "it moves on its own when the run finishes."
    );
  }
  if ((card.storedColumn || BACKLOG) === target) {
    return `the card is already in ${columnLabel(target)}.`;
  }
  return null;
}
