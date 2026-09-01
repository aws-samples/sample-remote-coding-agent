/**
 * Boot and the hash router.
 *
 * Four things happen here and nowhere else:
 *
 * 1. **The configuration check.** `web/config.js` is written by `make deploy` and is
 *    gitignored, so a checkout served straight from disk has none. That must render a
 *    panel naming the file and the command, not a blank page -- the blank page is the
 *    failure everybody hits first and it explains nothing.
 * 2. **`auth.complete()`**, once, before anything asks for a token.
 * 3. **The sidebar**, mounted once and highlighted on every navigation, so it does not
 *    flicker or lose its scroll position when the main region swaps.
 * 4. **`destroy()` before the next `mount()`.** Every view owns chained timers; the
 *    router calling `destroy` first is what guarantees none of them outlives its DOM.
 *
 * The four routed views are loaded with a dynamic `import()` rather than a static one.
 * That is deliberate: a view is only fetched when it is routed to, a failure to load one
 * is a readable panel instead of a whole console that will not start, and the console
 * therefore works while the other views are still being built.
 */

import * as auth from "./auth.js";
import * as configs from "./configs.js";
import * as ui from "./ui.js";
import * as sidebar from "./views/sidebar.js";

const VIEWS = {
  boards: () => import("./views/board.js"),
  cards: () => import("./views/card.js"),
  tasks: () => import("./views/task.js"),
  register: () => import("./views/register.js"),
};

let currentView = null;
let shell = null;
// Bumped on every navigation. A dynamic import and a first fetch are both awaited, so a
// fast operator can leave before a view has mounted; the generation check is what stops
// the late one drawing over the view that replaced it.
let generation = 0;

/** `#/tasks/cfg_x/tsk_y` -> `{name: "tasks", params: {configId, taskId}}`. */
function parse(hash) {
  const parts = String(hash || "")
    .replace(/^#\/?/, "")
    .split("/")
    .filter(Boolean)
    .map(decodeURIComponent);
  const [name, ...rest] = parts;

  if (!name) return { name: null };
  if (name === "boards" && rest[0]) return { name: "boards", params: { configId: rest[0] } };
  if (name === "cards" && rest[0]) return { name: "cards", params: { cardId: rest[0] } };
  // Both keys, because `Tasks` is keyed `configId` + `taskId` and has no GSI: a run
  // cannot be fetched from its id alone, so the route cannot carry it alone either.
  if (name === "tasks" && rest[0] && rest[1]) {
    return { name: "tasks", params: { configId: rest[0], taskId: rest[1] } };
  }
  if (name === "register") return { name: "register", params: { configId: rest[0] || null } };
  return { name: "unknown", params: { hash } };
}

function boardHash(configId) {
  return `#/boards/${encodeURIComponent(configId)}`;
}

function renderUnconfigured(root) {
  ui.empty(root);
  root.append(
    ui.panel("This console is not configured", [
      ui.banner(
        "warn",
        "web/config.js is missing. It names the API base, the Cognito user pool and the " +
          "hosted-UI domain of one deployment, and it is written by `make deploy` rather " +
          "than committed -- it is not a secret, but a committed copy would point every " +
          "checkout at one account.",
      ),
      ui.el(
        "p",
        {},
        "Deploy the stack and re-upload the console, or copy web/config.example.js to " +
          "web/config.js and fill it in for a local smoke test.",
      ),
    ]),
  );
}

function renderSignIn(root) {
  ui.empty(root);
  root.append(
    ui.panel("Sign in", [
      ui.el("p", {}, "This console signs in through the Cognito hosted UI for your account."),
      ui.action("Sign in", { class: "btn btn-primary", onClick: () => auth.signIn() }),
    ]),
  );
}

function renderUnknown(root, hash) {
  ui.empty(root);
  root.append(
    ui.panel("No such view", [
      ui.el("p", {}, `${hash || "that address"} is not a view of this console.`),
      ui.el("a", { class: "btn", href: "#/" }, "Back to a board"),
    ]),
  );
}

function renderUnavailable(root, name, error) {
  ui.empty(root);
  root.append(
    ui.panel("That view did not load", [
      ui.banner("bad", `js/views/${name}.js could not be loaded: ${error.message}`),
      ui.el("a", { class: "btn", href: "#/" }, "Back to a board"),
    ]),
  );
}

async function navigate() {
  const view = document.getElementById("view");
  const target = parse(window.location.hash);
  const mine = (generation += 1);

  // Tear the old view down *before* the new one is fetched: its poll must not keep
  // firing while a dynamic import is in flight.
  if (currentView) {
    currentView.destroy?.();
    currentView = null;
  }

  if (target.name === null) {
    // `#/` is not a view, it is a question: which repository? The first `READY` one, or
    // registration when there are none.
    await configs.load().catch(() => []);
    const configId = configs.defaultConfigId();
    window.location.hash = configId ? boardHash(configId) : "#/register";
    return;
  }

  shell?.highlight?.(window.location.hash);
  ui.empty(view);

  if (target.name === "unknown") {
    renderUnknown(view, target.params.hash);
    return;
  }

  let module;
  try {
    module = await VIEWS[target.name]();
  } catch (error) {
    if (mine === generation) renderUnavailable(view, target.name, error);
    return;
  }
  if (mine !== generation) return;
  currentView = module.mount(view, target.params);
}

async function boot() {
  const view = document.getElementById("view");
  const rail = document.getElementById("sidebar");

  if (!window.RCA_CONFIG) {
    renderUnconfigured(view);
    return;
  }

  try {
    await auth.complete();
  } catch (error) {
    // A refused or stale sign-in is not fatal: say what happened and offer to start
    // again, rather than leaving the operator on a page that does nothing.
    ui.toast(error.message, "bad");
  }
  if (!auth.isSignedIn()) {
    renderSignIn(view);
    return;
  }

  shell = sidebar.mount(rail);
  window.addEventListener("hashchange", navigate);
  await navigate();
}

// Every failure that reaches here is a promise nobody awaited -- a click handler's
// request, usually. Toasting it is the difference between a button that appears to do
// nothing and one that says why.
window.addEventListener("unhandledrejection", (event) => {
  ui.toast(event.reason?.message || "something failed and said nothing about it", "bad");
});

boot();
