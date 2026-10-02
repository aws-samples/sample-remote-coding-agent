/**
 * Every node the console draws, the one clock behind every timestamp, and the only
 * place a refusal is expressed.
 *
 * Three invariants live here rather than in the views, because a view that hand-rolls
 * any of them reintroduces an incident nobody would notice:
 *
 * 1. **One clock.** `relTime` registers each `<time>` node in a module-level `WeakRef`
 *    registry and a single chained tick rewrites them all. A board-only refresh timer
 *    leaves every other view printing a frozen number, and teaches each view to grow a
 *    clock of its own. `Date.parse(` and `new Date(` therefore appear in this file and
 *    nowhere else under `web/js/`, and the test pins both spellings so the rule cannot
 *    be evaded by switching constructors.
 * 2. **A refusal is readable.** `action` + `setRefusal` are the only writers of
 *    `aria-disabled`, and the `disabled` property is never used anywhere: a `disabled`
 *    button fires no click and takes no focus, so a reason carried in a tooltip is
 *    unreachable by touch and by keyboard alike. Hover is not an affordance.
 * 3. **No HTML strings.** Everything is built with `el()` and every string is assigned
 *    through `textContent`, so the activity pulse can write in place, and a card title
 *    or an activity `detail` cannot carry markup.
 */

import { describe } from "./status.js";

// 15 s: the shortest interval at which a "3 minutes ago" is ever wrong by a whole unit.
const CLOCK_TICK_MS = 15000;
const TOAST_MS = 6000;

// A first click arms, a second commits -- but not within the grace window, because one
// physical double-click must not satisfy both halves of a destructive confirmation.
export const ARM_GRACE_MS = 600;
// And an armed button that is walked away from disarms itself.
export const DISARM_MS = 6000;
// The child order of a danger panel, and therefore its tab order and its initial focus.
// `confirmDanger` maps over this array, so a refactor cannot put the danger button
// first without editing the constant the test reads.
export const CONFIRM_ORDER = ["prompt", "cancel", "danger"];

// -- nodes ---------------------------------------------------------------------------

/**
 * `el("div", {class: "card"}, ["title", node])`.
 *
 * `text` is a property rather than a child so the common single-string case reads as
 * one call, and every string path here ends at `textContent`.
 */
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2).toLowerCase(), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  append(node, children);
  return node;
}

/** Append one child, an array of them, or a string. Skips `null` so a view can inline
 * a condition (`[title, prUrl && link(prUrl)]`) without an `if`. */
export function append(parent, children) {
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child === null || child === undefined || child === false || child === "") continue;
    parent.append(typeof child === "object" ? child : document.createTextNode(String(child)));
  }
  return parent;
}

/**
 * Write a string into an existing node, in place.
 *
 * This is what the activity pulse uses. Rewriting one text node keeps hover, focus, an
 * open menu and a partly typed input alive across a 5 s refresh; re-rendering the
 * container throws all four away, which is what makes a polled page feel hostile.
 */
export function text(node, value) {
  node.textContent = value === null || value === undefined ? "" : String(value);
  return node;
}

/** Remove every child. `replaceChildren()`, never an assignment of an HTML string. */
export function empty(node) {
  node.replaceChildren();
  return node;
}

/** A status chip. The tone is a class, and the label is always present as text:
 * nothing here conveys state by colour alone. */
export function chip(status) {
  const { label, tone } = describe(status);
  const node = el("span", { class: `chip tone-${tone}` }, label);
  // The raw name in the tooltip, so an operator reading a screenshot of the board can
  // still tell `SUCCEEDED_UNVERIFIED` from `SUCCEEDED`.
  if (status) node.title = status;
  return node;
}

/** An external link. `noopener noreferrer` because these go to GitHub and to the AWS
 * console, and a `window.opener` handle to either is not something to hand out. */
export function link(href, label) {
  return el("a", { href, class: "link", target: "_blank", rel: "noopener noreferrer" }, label);
}

/** A titled panel. The console has no drawers and no modals: a panel is appended where
 * the thing it is about already is. */
export function panel(heading, children = [], props = {}) {
  return el("section", { class: "panel", ...props }, [
    heading ? el("h2", { class: "panel-title" }, heading) : null,
    el("div", { class: "panel-body" }, children),
  ]);
}

/** A persistent explanation: an error, a caveat, a "nothing here yet". Not a toast --
 * a toast is for something that just happened, a banner for something that is true. */
export function banner(tone, message, children = []) {
  return el("div", { class: `banner tone-${tone}`, role: tone === "bad" ? "alert" : null }, [
    el("p", { class: "banner-message" }, message),
    ...(Array.isArray(children) ? children : [children]),
  ]);
}

/** An empty state. Always says what to do next, never just "no items". */
export function emptyState(message, children = []) {
  const rest = Array.isArray(children) ? children : [children];
  return el("div", { class: "empty-state" }, [el("p", {}, message), ...rest]);
}

// -- toasts --------------------------------------------------------------------------

/**
 * A transient message in `#toasts`, which is `role="status" aria-live="polite"` in the
 * markup -- so every toast is announced. That is why an armed button toasts its own
 * prompt: a screen reader hears the sentence the label carries.
 */
export function toast(message, tone = "info") {
  const host = document.getElementById("toasts");
  if (!host || !message) return null;
  const wash = tone === "info" ? "idle" : tone;
  const node = el("div", { class: `toast tone-${wash}` }, String(message));
  host.append(node);
  setTimeout(() => node.remove(), TOAST_MS);
  return node;
}

// -- the one clock -------------------------------------------------------------------

// `{ref: WeakRef<HTMLTimeElement>, at: epoch ms}`. A `WeakRef` so a view that is torn
// down without ceremony still lets its timestamps be collected.
const timestamps = [];
let clockTimer = null;

function relative(at) {
  const seconds = Math.round((Date.now() - at) / 1000);
  const ago = Math.abs(seconds);
  const suffix = seconds < 0 ? " from now" : " ago";
  if (ago < 45) return "just now";
  if (ago < 90) return `a minute${suffix}`;
  if (ago < 3600) return `${Math.round(ago / 60)} minutes${suffix}`;
  if (ago < 7200) return `an hour${suffix}`;
  if (ago < 86400) return `${Math.round(ago / 3600)} hours${suffix}`;
  if (ago < 172800) return `a day${suffix}`;
  return `${Math.round(ago / 86400)} days${suffix}`;
}

function tick() {
  clockTimer = null;
  // While hidden the tick re-arms without touching the DOM: a background tab reflowing
  // a hundred timestamps every 15 s is work with nobody watching it.
  if (!document.hidden) {
    for (let index = timestamps.length - 1; index >= 0; index -= 1) {
      const node = timestamps[index].ref.deref();
      // Pruned on `deref()` returning undefined, and deliberately NOT on whether the
      // node is in the document: the first render happens before the node is appended,
      // so a document-membership prune would drop every timestamp the moment it was
      // created and the board would print one frozen number per card.
      if (node === undefined) timestamps.splice(index, 1);
      else node.textContent = relative(timestamps[index].at);
    }
  }
  // Stops itself when the registry empties. That self-stopping property is the same
  // reason the pollers in `activity.js` are chained `setTimeout` rather than a
  // repeating timer.
  if (timestamps.length) clockTimer = setTimeout(tick, CLOCK_TICK_MS);
}

function startClock() {
  if (clockTimer === null) clockTimer = setTimeout(tick, CLOCK_TICK_MS);
}

document.addEventListener("visibilitychange", () => {
  // Back from a hidden tab every timestamp is stale by however long the tab was away,
  // so catch up now rather than at the next scheduled tick.
  if (!document.hidden && timestamps.length) {
    if (clockTimer !== null) clearTimeout(clockTimer);
    tick();
  }
});

/**
 * A `<time>` element that keeps itself current: "4 minutes ago", with the absolute
 * timestamp in its tooltip and in `datetime` for anything machine-readable.
 *
 * Every ISO string the API emits is UTC, so nothing here has to reason about zones,
 * and views never sort by time -- the API returns cards and tasks newest-first and the
 * activity feed arrives in feed order.
 */
export function relTime(iso) {
  const node = el("time", { class: "rel", datetime: iso || null });
  const at = Date.parse(iso);
  if (Number.isNaN(at)) {
    // An absent or unparseable timestamp shows what it actually is. A dash is honest;
    // "just now" would be a lie about a field that was empty.
    node.textContent = iso ? String(iso) : "--";
    return node;
  }
  node.title = new Date(at).toLocaleString();
  node.textContent = relative(at);
  timestamps.push({ ref: new WeakRef(node), at });
  startClock();
  return node;
}

// -- refusal that can be read --------------------------------------------------------

/**
 * A button whose refusal is readable.
 *
 * `opts.refusal` is the reason the action is not available right now, or `null`. The
 * click listener checks `aria-disabled` **first** and toasts the reason before the
 * action can run, so the control still fires a click, still takes focus, and still
 * announces why -- which is the whole point of not using the `disabled` property.
 */
export function action(label, opts = {}) {
  const node = el("button", { type: "button", class: opts.class || "btn" }, label);
  node.addEventListener("click", (event) => {
    if (node.getAttribute("aria-disabled") === "true") {
      event.preventDefault();
      toast(node.title || "that is not possible right now", "warn");
      return;
    }
    opts.onClick?.(event);
  });
  setRefusal(node, opts.refusal ?? null);
  // A refusal owns the tooltip, so an informational one is only set when there is none.
  if (opts.title && !node.hasAttribute("title")) node.title = opts.title;
  return node;
}

/**
 * Refuse a control, or stop refusing it.
 *
 * The reason goes in `title` as well as `aria-disabled`, because the click handler
 * `action` installed reads it back from there when it toasts -- one string, one owner,
 * so a refusal cannot be shown in the tooltip and a different one in the toast.
 */
export function setRefusal(node, reason) {
  if (reason) {
    node.setAttribute("aria-disabled", "true");
    node.title = reason;
  } else {
    node.removeAttribute("aria-disabled");
    node.removeAttribute("title");
  }
  return node;
}

/**
 * Mark a control as working, or stop.
 *
 * `setRefusal` alone is not enough feedback for a slow action, and that is measured rather
 * than assumed: it dims the control through `[aria-disabled="true"]` and says why in a
 * tooltip, which an operator mid-click never sees. On a button whose work ends in a page
 * navigation the dimming also lasted only as long as the fetch, so the control went back to
 * looking idle while the browser was still leaving -- and the operator clicked again.
 *
 * So this rewrites the **label** as well, which is the part that is visible without hovering,
 * and keeps `aria-busy` for anything reading the page aloud. Passing `null` restores the
 * original label, so a caller does not have to remember it.
 */
export function setBusy(node, label) {
  if (label) {
    if (!node.dataset.idleLabel) node.dataset.idleLabel = node.textContent || "";
    node.textContent = label;
    node.setAttribute("aria-busy", "true");
    setRefusal(node, label);
    return node;
  }
  if (node.dataset.idleLabel) {
    node.textContent = node.dataset.idleLabel;
    delete node.dataset.idleLabel;
  }
  node.removeAttribute("aria-busy");
  setRefusal(node, null);
  return node;
}

// -- destructive confirmation --------------------------------------------------------

/**
 * Two clicks on one button: the first arms it and rewrites the label, the second
 * commits.
 *
 * A window of `ARM_GRACE_MS` after arming ignores the second click, so one physical
 * double-click cannot delete anything; `DISARM_MS` later an armed button that was
 * walked away from reverts. The armed label names the subject, and arming toasts the
 * same sentence into the live region so it is heard as well as seen.
 */
export function armedButton(label, { subject, verb = "delete", onConfirm }) {
  const prompt = `Confirm: ${verb} “${subject}”`;
  let armedAt = 0;
  let disarmTimer = null;

  const node = action(label, {
    class: "btn btn-danger",
    onClick: () => {
      if (node.getAttribute("data-armed") !== "true") {
        node.setAttribute("data-armed", "true");
        text(node, prompt);
        toast(prompt, "warn");
        armedAt = Date.now();
        disarmTimer = setTimeout(disarm, DISARM_MS);
        return;
      }
      if (Date.now() - armedAt < ARM_GRACE_MS) {
        toast("that was one double-click, not two decisions -- click again to confirm", "warn");
        return;
      }
      disarm();
      onConfirm();
    },
  });

  function disarm() {
    if (disarmTimer !== null) clearTimeout(disarmTimer);
    disarmTimer = null;
    node.removeAttribute("data-armed");
    text(node, label);
  }

  return node;
}

/**
 * The panel around an armed button: what is about to happen, in a sentence that names
 * the subject, with Cancel ahead of the danger button.
 *
 * The children are built by mapping over `CONFIRM_ORDER`, so DOM order -- and with it
 * tab order and the initial focus -- is a constant rather than a property of the order
 * somebody happened to write the `append` calls in.
 */
export function confirmDanger({ subject, verb, detail, onConfirm, onCancel }) {
  const node = el("div", { class: "confirm", role: "group", "aria-label": `${verb} ${subject}` });
  const build = {
    prompt: () => el("p", { class: "confirm-prompt" }, `${verb} ${subject}? ${detail}`),
    cancel: () =>
      action("Cancel", {
        class: "btn",
        onClick: () => {
          node.remove();
          onCancel?.();
        },
      }),
    danger: () => armedButton(`${verb} ${subject}`, { subject, verb, onConfirm }),
  };

  const built = {};
  for (const part of CONFIRM_ORDER) {
    built[part] = build[part]();
    node.append(built[part]);
  }
  // Deferred by one turn because the caller appends this panel after we return, and a
  // node that is not in the document yet cannot take focus.
  setTimeout(() => built.cancel.focus(), 0);
  return node;
}
