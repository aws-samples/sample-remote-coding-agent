/**
 * Connecting this deployment to GitHub: the one-time setup every repository depends on.
 *
 * This page exists because the alternative is a page of instructions. A GitHub App needs a
 * callback URL that only AgentCore can issue, and AgentCore needs a client id and secret
 * that only GitHub can issue, so done by hand it is: create the provider, copy a URL, fill
 * in a form on GitHub, copy two values back, paste them somewhere. Every one of those steps
 * is a place to transpose a character and discover it twenty minutes later.
 *
 * So it is three buttons and the API does the rest:
 *
 * 1. **Create the GitHub App.** `POST /github-app` mints the credential provider and hands
 *    back a *manifest* carrying its callback URL. This view POSTs that manifest to GitHub in
 *    the operator's own browser -- it has to be their session, because the app will belong
 *    to them -- and GitHub redirects back here with a single-use `code`.
 *    `POST /github-app/conversions` turns that code into the app and stores its credentials
 *    in the token vault. The operator types nothing.
 * 2. **Install it** on the repositories they intend to register.
 * 3. **Add a repository**, which is `#/register`.
 *
 * **Why the `code` arrives in `location.search` and not in the hash.** GitHub appends
 * `?code=...` to the manifest's `redirect_url`, and a query string goes *before* a fragment
 * in a URL -- so a `redirect_url` ending in `#/connect` would come back as
 * `...#/connect?code=...`, where the code is part of the fragment and `URLSearchParams` on
 * `location.search` never sees it. The manifest therefore names the bare origin, this view
 * reads `location.search`, and `app.js` routes here when it sees a code at boot.
 *
 * The code is cleared out of the address bar with `history.replaceState` as soon as it has
 * been exchanged, so a reload does not re-send a code GitHub has already spent.
 */

import * as api from "../api.js";
import * as configs from "../configs.js";
import * as ui from "../ui.js";

// GitHub's own page for an app's installations, derived server-side from the slug -- this
// view never builds a github.com URL of its own except the manifest form target, which the
// API hands it.
const REPOSITORY_ACCESS_HELP =
  "Install it on the repositories you intend to register, and only those: the app's " +
  "installation list is what bounds which repositories the agent can reach. " +
  "“Only select repositories” is the right answer here.";

function kv(key, value) {
  return [ui.el("div", { class: "kv-key" }, key), ui.el("div", { class: "kv-value" }, value)];
}

/**
 * The `code` GitHub appended to its redirect, or `""`.
 *
 * Exported for the router, which has to answer "is this boot a return from GitHub?" before
 * it knows which view to mount.
 */
export function manifestCode(search = window.location.search) {
  return new URLSearchParams(String(search || "")).get("code") || "";
}

/** Take the spent code out of the address bar without reloading or adding a history entry. */
function clearCode() {
  const url = new URL(window.location.href);
  url.searchParams.delete("code");
  url.searchParams.delete("state");
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
}

/**
 * POST the manifest to GitHub as a real form submission, in this tab.
 *
 * A `fetch` cannot do this: the request has to carry the operator's github.com session
 * cookies and end in GitHub rendering its own "Create GitHub App" confirmation, which is a
 * navigation and not a response this code may read. So it is a form, built and submitted
 * from script -- no inline handler and no inline style, so the console's CSP needs nothing
 * added.
 */
function submitManifest({ manifestFormUrl, manifest, state }) {
  const form = ui.el("form", {
    method: "POST",
    action: `${manifestFormUrl}?state=${encodeURIComponent(state)}`,
  });
  form.append(
    ui.el("input", { type: "hidden", name: "manifest", value: JSON.stringify(manifest) }),
  );
  // Appended because a detached form cannot be submitted; it is never visible, because the
  // submit navigates this tab away in the same tick.
  document.body.append(form);
  form.submit();
}

export function mount(root) {
  ui.empty(root);
  const body = ui.el("div", { class: "stack" });
  const notes = ui.el("div", { class: "stack" });
  let destroyed = false;

  root.append(
    ui.el("div", { class: "board-head" }, [
      ui.el("h2", { class: "board-title" }, "Connect GitHub"),
    ]),
    notes,
    body,
  );

  function fail(error) {
    ui.empty(notes);
    // Verbatim: `ManifestConversionFailed` says the code is single-use and expired, which
    // is the one thing an operator needs to know to recover, and no wording here improves it.
    notes.append(ui.banner("bad", error.message));
  }

  function renderBusy(message) {
    ui.empty(body);
    body.append(ui.panel("Working", [ui.el("p", {}, message)]));
  }

  async function start() {
    ui.empty(notes);
    renderBusy("Preparing the GitHub App…");
    try {
      // `window.location.origin` and not a configured value: this is the origin the operator
      // is actually looking at, which is the one AgentCore has to be willing to return their
      // browser to. The API registers it on the workload identity and refuses anything that
      // is not https (or localhost).
      const prepared = await api.connectGitHubApp(window.location.origin);
      if (destroyed) return;
      submitManifest(prepared);
    } catch (error) {
      if (destroyed) return;
      fail(error);
      await refresh();
    }
  }

  async function finish(code) {
    renderBusy("GitHub created the app. Storing its credentials…");
    try {
      await api.convertGitHubApp(code);
      clearCode();
      ui.toast("GitHub is connected. Next: install the app on a repository.", "good");
    } catch (error) {
      clearCode();
      fail(error);
    }
    if (!destroyed) await refresh();
  }

  function renderDisconnected() {
    ui.empty(body);
    body.append(
      ui.panel("Step 1 — create the GitHub App", [
        ui.el(
          "p",
          {},
          "This deployment authenticates to GitHub through a GitHub App that belongs to " +
            "you. Pressing the button below takes you to GitHub with the app already " +
            "described -- its name, its callback URL and the two permissions it asks for " +
            "(contents: write and pull requests: write). Confirm it there and you come " +
            "straight back; nothing is copied or pasted.",
        ),
        ui.el(
          "p",
          { class: "field-hint" },
          "No credential is stored in this deployment. The app's client secret goes into " +
            "AgentCore Identity's token vault and is never readable again, by this console " +
            "or by the API.",
        ),
        ui.el("div", { class: "row" }, [
          ui.action("Create the GitHub App on GitHub", {
            class: "btn btn-primary",
            onClick: start,
          }),
        ]),
      ]),
    );
  }

  function renderConnected(status) {
    ui.empty(body);
    const disconnect = ui.armedButton("Disconnect GitHub", {
      subject: status.appSlug || "this GitHub App",
      verb: "disconnect",
      onConfirm: async () => {
        const answer = await api.disconnectGitHubApp();
        ui.toast(
          answer.githubAppRemains
            ? "Disconnected. The GitHub App itself still exists in your GitHub account -- " +
              "delete it there if you are finished with it."
            : "Disconnected.",
          "good",
        );
        if (!destroyed) await refresh();
      },
    });

    body.append(
      ui.panel("Step 2 — install the app on a repository", [
        ui.banner("good", `${status.appSlug} is connected to this deployment.`),
        ui.el("p", {}, REPOSITORY_ACCESS_HELP),
        ui.el("div", { class: "row" }, [
          ui.el(
            "a",
            {
              class: "btn btn-primary",
              href: status.installUrl,
              target: "_blank",
              rel: "noopener noreferrer",
            },
            "Install on GitHub",
          ),
          ui.el("a", { class: "btn", href: "#/register" }, "Step 3 — add a repository"),
        ]),
        ui.el(
          "p",
          { class: "field-hint" },
          "Installing and authorising are two different things, and both are needed. " +
            "Installing grants the app access to a repository; authorising -- which happens " +
            "once per repository, on its own page after you register it -- is what gives the " +
            "agent a credential to act with.",
        ),
      ]),
      ui.panel("This deployment's GitHub App", [
        ui.el("div", { class: "kv" }, [
          ...kv(
            "App",
            status.appUrl
              ? ui.el(
                  "a",
                  { href: status.appUrl, target: "_blank", rel: "noopener noreferrer" },
                  status.appSlug,
                )
              : status.appSlug || "--",
          ),
          ...kv("App id", ui.el("span", { class: "mono" }, status.appId || "--")),
          ...kv("Owner", status.appOwner || "--"),
          // Printed because it is the one value a GitHub App settings page has to agree
          // with, and the one an operator would otherwise have no way to check.
          ...kv(
            "Callback URL",
            ui.el("span", { class: "mono truncate" }, status.callbackUrl || "--"),
          ),
          ...kv("Credential provider", ui.el("span", { class: "mono" }, status.providerName || "--")),
        ]),
      ]),
      ui.panel("Disconnect", [
        ui.el(
          "p",
          {},
          "Deleting the credential provider revokes every repository's authorisation at " +
            "once, because the tokens live in its vault entry and nowhere else. Registered " +
            "repositories keep their runtimes and stop being able to push until a new app is " +
            "connected and each one is authorised again.",
        ),
        ui.el("div", { class: "row" }, [disconnect]),
      ]),
    );
  }

  async function refresh() {
    try {
      const status = await api.getGitHubApp();
      if (destroyed) return;
      if (status.connected) renderConnected(status);
      else renderDisconnected();
    } catch (error) {
      if (destroyed) return;
      ui.empty(body);
      body.append(
        ui.panel("Could not read the GitHub connection", [ui.banner("bad", error.message)]),
      );
    }
  }

  const code = manifestCode();
  if (code) {
    // The return leg from GitHub. Exchanged immediately and before anything is rendered that
    // could tempt a second click: the code is single-use.
    void finish(code).then(() => configs.invalidate());
  } else {
    void refresh();
  }

  return {
    destroy() {
      destroyed = true;
      ui.empty(root);
    },
  };
}
