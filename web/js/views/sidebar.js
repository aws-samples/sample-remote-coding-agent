/**
 * The persistent left rail: every registered repository, one click from its board.
 *
 * It is not a routed view -- `app.js` mounts it once at boot and calls `highlight()` on
 * every navigation -- but it has the same `mount(root) -> {destroy}` shape as the four
 * that are routed, so there is one lifecycle contract in the console rather than two.
 *
 * A repository that is still provisioning shows its status, because that is the one
 * thing an operator wants from this list in the first two minutes after registering.
 */

import * as activity from "../activity.js";
import * as auth from "../auth.js";
import * as configs from "../configs.js";
import * as ui from "../ui.js";

// The configuration lifecycle, which is `configurations.CONFIG_STATUSES` and is *not*
// the task vocabulary in `status.js`: a repository is not a run. `DELETING` is `warn`
// rather than `bad` -- teardown is something an operator asked for.
const CONFIG_TONES = {
  PENDING: "idle",
  CREATING: "live",
  READY: "good",
  UPDATING: "live",
  FAILED: "bad",
  DELETING: "warn",
};

function boardHash(configId) {
  return `#/boards/${encodeURIComponent(configId)}`;
}

export function mount(root) {
  ui.empty(root);

  const list = ui.el("ul", { class: "repo-list" });
  const account = ui.el("div", { class: "sidebar-account" });

  root.append(
    ui.el("div", { class: "sidebar-head" }, [
      ui.el("h1", { class: "brand" }, "Remote coding agent"),
      ui.el("a", { class: "btn btn-primary btn-block", href: "#/register" }, "+ Add repository"),
      // Always reachable, not just on the empty state: the GitHub App is one object shared
      // by every repository, so an operator has reason to come back to it long after the
      // first setup -- to install it on a repository they have just created, or to see which
      // app this deployment is actually using.
      ui.el("a", { class: "btn btn-quiet btn-block btn-small", href: "#/connect" }, "GitHub App"),
    ]),
    ui.el("nav", { class: "sidebar-nav", "aria-label": "Repositories" }, list),
    account,
  );

  function render() {
    ui.empty(list);
    const items = configs.list();
    if (!items.length) {
      list.append(
        ui.el("li", { class: "repo-empty" }, "No repositories yet. Add one to get a board."),
      );
      return;
    }
    for (const config of items) {
      const tone = CONFIG_TONES[config.status] || "idle";
      list.append(
        ui.el("li", { class: "repo-row", "data-config": config.configId }, [
          ui.el("a", { class: "repo-link", href: boardHash(config.configId) }, [
            // The dot is decorative: the status it stands for is written next to it
            // whenever it is not `READY`, so nothing here is colour alone.
            ui.el("span", { class: `dot tone-${tone}`, "aria-hidden": "true" }),
            ui.el("span", { class: "repo-name" }, config.repository || config.configId),
            config.status === "READY"
              ? null
              : ui.el("span", { class: `repo-status tone-${tone}` }, config.status),
          ]),
        ]),
      );
    }
    if (configs.isTruncated()) {
      // A silent cap reads as "that is all of them".
      const note = `only the first ${items.length} repositories are shown`;
      list.append(ui.el("li", { class: "repo-empty" }, note));
    }
    highlight(window.location.hash);
  }

  function renderAccount() {
    ui.empty(account);
    // Decoded from the ID token, never fetched: the email is a label, and a round trip
    // to Cognito for it would be a request that can fail for no benefit.
    const email = auth.claims().email;
    account.append(
      email ? ui.el("p", { class: "account-email", title: email }, email) : null,
      ui.action("Sign out", { class: "btn btn-quiet btn-block", onClick: () => auth.signOut() }),
    );
  }

  /** Mark the row whose board is on screen. `aria-current="page"` as well as a class,
   * because the highlight is otherwise a background colour and nothing else. */
  function highlight(hash) {
    const match = /^#\/boards\/([^/]+)/.exec(hash || "");
    const current = match ? decodeURIComponent(match[1]) : null;
    for (const row of list.querySelectorAll(".repo-row")) {
      const isCurrent = row.getAttribute("data-config") === current;
      row.classList.toggle("is-current", isCurrent);
      const anchor = row.querySelector(".repo-link");
      if (isCurrent) anchor.setAttribute("aria-current", "page");
      else anchor.removeAttribute("aria-current");
    }
  }

  renderAccount();
  configs
    .load()
    .then(render)
    .catch((error) => {
      ui.empty(list);
      list.append(ui.el("li", { class: "repo-empty tone-bad" }, error.message));
    });

  // A repository provisions in 8-12 minutes and its status is what this list is
  // for while it does, so the rail carries the one poll that is not a view's. It backs
  // off to 30 s like every other, and skips itself while the tab is hidden.
  const poll = activity.createPoll({
    load: () => configs.load({ force: true }),
    apply: (items) => {
      render();
      return items.map((config) => `${config.configId}:${config.status}`).join(",");
    },
  });

  return {
    destroy() {
      poll.stop();
      ui.empty(root);
    },
    refresh: render,
    highlight,
  };
}
