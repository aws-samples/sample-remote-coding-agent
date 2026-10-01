/**
 * Registering a repository, and everything about one afterwards.
 *
 * One module for two shapes because they are two halves of one thing: `#/register` is the
 * form that creates a configuration, `#/register/<configId>` is the settings for one that
 * exists (and is where the board header's **Settings** goes). Neither is a modal -- the
 * console has no drawers, so both are panels in the main region.
 *
 * Three rules this view exists to honour.
 *
 * 1. **The API is the only validator.** There is no URL grammar here, deliberately:
 *    `configurations._repository_from_url` accepts two forms and answers with a sentence
 *    written for the operator. A second copy of that regex in this file would drift from the
 *    server's, and the drift would show up as a console refusing a URL the API would have
 *    taken. The `InvalidRepositoryUrl` message is shown verbatim.
 * 2. **An emptied setting is `null`, not `""`.** `configurations.patch` REMOVEs on `null`,
 *    which puts `verifyCommand`, `setupCommand` or `defaultBranch` back to per-run
 *    detection, while a stored empty string is a *declared* "run nothing". Clearing a
 *    field in this form therefore sends `null` and the hint under it says so.
 * 3. **No credential is ever typed here.** Registration takes a repository URL and nothing
 *    else: the GitHub credential comes from this deployment's GitHub App through AgentCore
 *    Identity, so what a repository needs after registering is an *authorisation*, which is
 *    the panel at the bottom of its settings page. There is no token field in this file and
 *    there must not be one -- a field that accepted a PAT would be storing a credential the
 *    rest of the system has no place to put.
 */

import * as activity from "../activity.js";
import * as api from "../api.js";
import * as configs from "../configs.js";
import * as ui from "../ui.js";

// `configurations.EDITABLE_FIELDS`, same names and same order. These three are the whole
// of `PATCH /configurations/{configId}`: `repositoryUrl` is deliberately not editable (you
// re-register rather than repointing a configuration at a different repository) and
// `githubToken` has its own route.
const EDITABLE = [
  {
    name: "verifyCommand",
    label: "Verify command",
    placeholder: "make test",
    hint:
      "left empty, each run detects one from the checkout. An empty value is not " +
      "“run nothing” -- to declare that, put a command that exits 0.",
  },
  {
    name: "setupCommand",
    label: "Setup command",
    placeholder: "make setup",
    hint: "runs before verification in every run that verifies.",
  },
  {
    name: "defaultBranch",
    label: "Default branch",
    placeholder: "main",
    hint: "left empty, each run reads the repository's own default branch.",
  },
];

// Three tones, not the rail's six: this view says READY, being-worked-on, or broken. The
// full lifecycle map belongs to the sidebar, which is the list that has to distinguish
// `CREATING` from `UPDATING` at a glance.
function statusTone(status) {
  if (status === "READY") return "good";
  if (status === "FAILED") return "bad";
  return "warn";
}

function boardHash(configId) {
  return `#/boards/${encodeURIComponent(configId)}`;
}

function field(label, control, hint) {
  return ui.el("label", { class: "field" }, [
    ui.el("span", { class: "field-label" }, label),
    control,
    hint ? ui.el("span", { class: "field-hint" }, hint) : null,
  ]);
}

function kv(key, value) {
  return [ui.el("div", { class: "kv-key" }, key), ui.el("div", { class: "kv-value" }, value)];
}

// Both forms the API accepts, reduced to `owner/repo`. Deliberately permissive and
// deliberately NOT a validator: the API is the only validator (see `urlField`), so a value
// this cannot parse leaves the link pointing at the bare token page rather than refusing
// anything. A trailing `.git` and a trailing slash are the two shapes a paste actually
// carries.
const REPO_SLUG = /^(?:https?:\/\/github\.com\/|git@github\.com:)([^/]+)\/([^/]+?)(?:\.git)?\/?$/;

export function repositorySlug(value) {
  const match = REPO_SLUG.exec(String(value || "").trim());
  return match ? `${match[1]}/${match[2]}` : "";
}

export function mount(root, { configId }) {
  ui.empty(root);
  // The same module answers both routes, and each half is a plain `{destroy}` so the
  // router's teardown contract does not have to know which one it got.
  return configId ? mountSettings(root, configId) : mountRegister(root);
}

// -- registering a repository ----------------------------------------------------------

function mountRegister(root) {
  const notes = ui.el("div", { class: "stack" });

  // `type="text"`, not `type="url"`: the browser's own validator rejects
  // `git@github.com:owner/repo.git`, which the API accepts, and a control that refuses a
  // legal value before the request is client-side validation by the back door.
  const urlField = ui.el("input", {
    type: "text",
    "aria-label": "Repository URL",
    autocomplete: "off",
    spellcheck: "false",
    placeholder: "https://github.com/owner/repo",
  });

  const optional = {};
  for (const spec of EDITABLE) {
    optional[spec.name] = ui.el("input", {
      type: "text",
      autocomplete: "off",
      spellcheck: "false",
      "aria-label": spec.label,
      placeholder: spec.placeholder,
    });
  }

  const submit = ui.action("Register repository", {
    class: "btn btn-primary",
    onClick: register,
  });

  function renderFailure(error) {
    ui.empty(notes);
    // Verbatim. `InvalidRepositoryUrl` names both accepted forms, `TokenNotAccepted` says
    // why a credential is not wanted here, and `GitHubAppNotConnected` names the step that
    // has not been done -- wording this view could only make worse.
    notes.append(
      ui.banner("bad", error.message, [
        // `RepositoryAlreadyRegistered` names the configuration that holds it, and the
        // useful next click is that repository's board rather than a re-read of the error.
        error.details?.configId
          ? ui.el(
              "a",
              { class: "btn btn-small", href: boardHash(error.details.configId) },
              "Open that repository's board",
            )
          : null,
        // The one refusal with a page to go to rather than a value to change.
        error.code === "GitHubAppNotConnected"
          ? ui.el("a", { class: "btn btn-small", href: "#/connect" }, "Connect GitHub")
          : null,
      ]),
    );
  }

  async function register() {
    const repositoryUrl = urlField.value.trim();
    if (!repositoryUrl) {
      // Presence, not shape: the API refuses an absent field with `MissingField` and a
      // round trip to be told a field is empty is a request that did not need making.
      ui.toast("a repository URL is required", "warn");
      return;
    }

    const body = { repositoryUrl };
    for (const spec of EDITABLE) {
      const value = optional[spec.name].value.trim();
      // Absent rather than empty, matching `configurations.create`: an omitted field falls
      // back to per-run detection and an empty string would be a declaration.
      if (value) body[spec.name] = value;
    }

    ui.setRefusal(submit, "the registration is already in flight");
    try {
      const config = await api.createConfiguration(body);
      configs.invalidate();
      await configs.load().catch(() => []);
      ui.toast(
        `${config.repository} is registered. Its runtime, execution role and memory are ` +
          "provisioning, which takes 8-12 minutes. Authorise it on its settings page while " +
          "you wait -- the two are independent.",
        "good",
      );
      // Straight to the settings page rather than the board, because there is one action
      // left to do and that is where it is. Registration answers 201 with the item, so the
      // id is known without a second read.
      window.location.hash = `#/register/${encodeURIComponent(config.configId)}`;
    } catch (error) {
      renderFailure(error);
    } finally {
      ui.setRefusal(submit, null);
    }
  }

  root.append(
    ui.el("div", { class: "board-head" }, [ui.el("h2", { class: "board-title" }, "Add repository")]),
    notes,
    ui.panel("Repository", [
      ui.el("div", { class: "form" }, [
        field(
          "Repository URL",
          urlField,
          "https://github.com/owner/repo or git@github.com:owner/repo.git. Trying this out? " +
            "Make an empty repository of your own first -- github.com/new -- rather than " +
            "pointing it at something you mind receiving pull requests. The API is the only " +
            "validator, so whichever form it accepts is the form this field accepts.",
        ),
        // Said here rather than discovered later: an operator who expects to paste a token
        // looks for the field, and its absence needs an explanation at the point they look.
        ui.banner("info", "No token to paste.", [
          ui.el(
            "p",
            {},
            "This deployment authenticates through its GitHub App, so registering takes the " +
              "repository URL and nothing else. Two things have to be true before a run can " +
              "push: the app must be installed on this repository, and this repository must " +
              "be authorised. Both are one click each on the next page.",
          ),
          ui.el("a", { class: "btn btn-small", href: "#/connect" }, "The GitHub App"),
        ]),
        // The three optional fields are folded away, and that is the whole point: they were
        // presented at the same level as the required one, so a first-time operator read
        // four inputs to fill and hunted for values that are better left empty. `<details>` and
        // not a scripted toggle -- it needs no JavaScript, so it cannot get out of step with
        // `ui.setRefusal`, and it survives the no-inline CSP with nothing added.
        ui.el("details", { class: "field-group" }, [
          ui.el("summary", {}, "Advanced — leave blank to detect from the repository"),
          ui.el(
            "p",
            { class: "field-hint" },
            "Every run reads these off the checkout: a Makefile `test:`/`setup:` target, or " +
              "`scripts.test`/`scripts.setup` in package.json. Set one only to override what " +
              "detection would find.",
          ),
          ...EDITABLE.map((spec) => field(spec.label, optional[spec.name], spec.hint)),
        ]),
        ui.el("div", { class: "row" }, [submit]),
      ]),
    ]),
  );

  return {
    destroy() {
      ui.empty(root);
    },
  };
}

// -- one repository's settings ---------------------------------------------------------

function mountSettings(root, configId) {
  // Seeded from the cache so the page has a name and a status before the first read
  // lands, then replaced wholesale by every answer: `PATCH` returns the merged item, so a
  // field-by-field merge here would keep whichever half the local copy happened to have.
  let config = configs.get(configId);
  let failure = null;
  // What the server last said each editable field was, so a poll can tell a pristine
  // field (safe to re-seed) from one the operator is halfway through typing.
  const server = {};
  // The signature the facts grid was last drawn from. The controls are built once and only
  // ever have their refusals rewritten, because rebuilding them would disarm an armed
  // Deregister and take the focus off whatever was being reached for.
  let drawn = null;

  const heading = ui.el("h2", { class: "board-title" });
  const statusText = ui.el("span", { class: "muted" });
  const notes = ui.el("div", { class: "stack" });
  const facts = ui.el("div", { class: "kv" });
  const dangerHost = ui.el("div", { class: "stack" });

  const inputs = {};
  for (const spec of EDITABLE) {
    inputs[spec.name] = ui.el("input", {
      type: "text",
      autocomplete: "off",
      spellcheck: "false",
      "aria-label": spec.label,
      placeholder: spec.placeholder,
    });
  }
  const save = ui.action("Save settings", { class: "btn btn-primary", onClick: saveSettings });

  // The authorisation panel's own state, read from `GET .../github-status` rather than from
  // the configuration item. It is deliberately not stored on the item: the token vault is
  // the only authority on whether a repository is authorised, and a cached flag would be a
  // second, staler answer to the same question.
  let auth = null;
  let authFailure = null;
  const authHost = ui.el("div", { class: "stack" });
  const authorize = ui.action("Authorise GitHub", {
    class: "btn btn-primary",
    onClick: () => startAuthorize(false),
  });
  const reauthorize = ui.action("Re-authorise", {
    class: "btn",
    onClick: () => startAuthorize(true),
  });

  const runtime = ui.action("Update runtime", {
    class: "btn btn-secondary",
    onClick: updateRuntime,
  });
  const deregister = ui.action("Deregister", { class: "btn btn-danger", onClick: openConfirm });

  function name() {
    return config?.repository || configId;
  }

  // -- reads --------------------------------------------------------------------------

  /** Everything the facts grid shows, in one string. */
  function signature() {
    if (!config) return "none";
    return [
      config.status,
      config.statusReason,
      config.repositoryUrl,
      config.agentRuntimeId,
      config.agentRuntimeVersion,
      config.memoryId,
      config.containerUri,
      config.updatedAt,
    ].join("|");
  }

  function pristine(spec) {
    return inputs[spec.name].value === (server[spec.name] ?? "");
  }

  /** Re-seed the three fields from the server's copy, but only while nothing is
   * half-typed -- the same rule the card editor follows, for the same reason: a six-second
   * poll must not eat a verify command that is being typed. */
  function seed({ force = false } = {}) {
    for (const spec of EDITABLE) {
      const next = config?.[spec.name] ?? "";
      if (force || pristine(spec)) inputs[spec.name].value = next;
      server[spec.name] = next;
    }
  }

  // -- writes -------------------------------------------------------------------------

  async function saveSettings() {
    const body = {};
    for (const spec of EDITABLE) {
      const value = inputs[spec.name].value.trim();
      if (value === (server[spec.name] ?? "")) continue;
      // `null` clears the field back to per-run detection; an empty string would be
      // stored, and a stored empty command means "run nothing".
      body[spec.name] = value === "" ? null : value;
    }
    if (!Object.keys(body).length) {
      // `configurations.patch` answers `EmptyPatch` for this on purpose, but the round
      // trip is avoidable.
      ui.toast("nothing has changed, so there is nothing to save", "warn");
      return;
    }
    try {
      config = await api.patchConfiguration(configId, body);
      // The rail draws the same item from its own cache, so a write here has to drop it.
      configs.invalidate();
      seed({ force: true });
      render();
      const cleared = Object.entries(body)
        .filter(([, value]) => value === null)
        .map(([key]) => key);
      ui.toast(
        cleared.length
          ? `saved; ${cleared.join(" and ")} is back to per-run detection`
          : "saved; the next run of this repository uses it",
        "good",
      );
      poll.wake();
    } catch (error) {
      ui.toast(error.message, "bad");
    }
  }

  /**
   * Start the 3LO leg, or report that it is already done.
   *
   * The authorisation URL is opened in a **new tab** rather than navigating this one: the
   * operator comes back to a page that is still here and still polling, and this tab is the
   * one that detects the result. It is also single-use and expires in ten minutes, so it is
   * fetched at click time and never held.
   *
   * `force` is the Re-authorise path -- the vault answers an already-authorised id with a
   * token and no URL, so without it there is no way to replace an authorisation made by the
   * wrong GitHub account.
   */
  async function startAuthorize(force) {
    const button = force ? reauthorize : authorize;
    ui.setRefusal(button, "asking GitHub for an authorisation URL");
    try {
      const answer = await api.authorizeGitHub(configId, { force });
      if (answer.authorized) {
        ui.toast("this repository is already authorised.", "good");
      } else {
        window.open(answer.authorizationUrl, "_blank", "noopener,noreferrer");
        ui.toast(
          "GitHub is open in a new tab. Approve it there, then come back -- this page " +
            "notices on its own.",
          "info",
        );
      }
    } catch (error) {
      ui.toast(error.message, "bad");
    } finally {
      ui.setRefusal(button, null);
      await refreshAuth();
    }
  }

  /** Re-read the authorisation verdict. Never throws: a failure is a banner, not a crash. */
  async function refreshAuth() {
    try {
      auth = await api.githubStatus(configId);
      authFailure = null;
    } catch (error) {
      authFailure = error.message;
    }
    renderAuth();
  }

  async function updateRuntime() {
    try {
      await api.runtimeUpdate(configId);
      configs.invalidate();
      ui.toast(
        "a runtime update is queued. It is a 202, not a done: the provisioner compares " +
          "the live runtime with the intended one and only writes UPDATING if something " +
          "actually differs.",
        "good",
      );
      poll.wake();
    } catch (error) {
      ui.toast(error.message, "bad");
    }
  }

  async function removeConfig() {
    try {
      await api.deleteConfiguration(configId);
      configs.invalidate();
      ui.toast(
        `${name()} is being deregistered. The runtime, the execution role and the secret ` +
          "go first and the configuration item is removed last, so the status stays " +
          "DELETING until the teardown finishes.",
        "good",
      );
      // Back to `#/`, which picks another repository or the registration form. Staying
      // here would be a settings page for an item that is on its way out.
      window.location.hash = "#/";
    } catch (error) {
      ui.toast(error.message, "bad");
    }
  }

  function openConfirm() {
    // One panel at a time: a second one would give two armed buttons for one repository.
    if (dangerHost.children.length) {
      ui.toast("the confirmation is already open below", "info");
      return;
    }
    dangerHost.append(
      ui.confirmDanger({
        subject: name(),
        verb: "Deregister",
        detail:
          "Its AgentCore runtime, execution role and GitHub secret are deleted. The " +
          "Cards and Tasks tables are retained, so this repository's cards and its run " +
          "history survive -- but they are keyed by this configuration id, and " +
          "registering the repository again creates a new one.",
        onConfirm: removeConfig,
      }),
    );
  }

  // -- rendering ----------------------------------------------------------------------

  function renderNotes() {
    ui.empty(notes);
    if (failure) notes.append(ui.banner("bad", `this repository could not be read: ${failure}`));
    if (!config) return;
    if (config.status !== "READY") {
      notes.append(
        ui.banner(
          statusTone(config.status),
          `${name()} is ${config.status}.` +
            (config.statusReason ? ` ${config.statusReason}` : "") +
            " Settings can be edited at any status; a run needs a READY repository.",
        ),
      );
    }
  }

  /**
   * The GitHub access panel: three states, each with exactly one action.
   *
   * The states are not cosmetic -- they are the three ways this can be wrong, and they have
   * different fixes. **Not authorised** is fixed here, by authorising. **Authorised but
   * cannot push** means the app is not installed on this repository, which is fixed on
   * GitHub and nowhere else -- so that case carries GitHub's own refusal and the install
   * link. **Authorised and can push** is the finished state and says so plainly, because
   * "nothing on this panel" reads as "something is still missing".
   *
   * `canPush === null` is the fourth, honest case: GitHub could not be reached, so the panel
   * says the check did not run rather than implying a verdict.
   */
  function renderAuth() {
    ui.empty(authHost);
    if (authFailure) {
      authHost.append(ui.banner("bad", `could not read the GitHub status: ${authFailure}`));
      return;
    }
    if (!auth) {
      authHost.append(ui.el("p", { class: "field-hint" }, "Checking the GitHub credential…"));
      return;
    }
    if (!auth.authorized) {
      authHost.append(
        ui.banner("warn", auth.reason || "this repository is not authorised yet"),
        ui.el(
          "p",
          {},
          "Authorising opens GitHub in a new tab and asks you to approve this deployment's " +
            "GitHub App once for this repository. The credential it issues is held by " +
            "AgentCore Identity, refreshed automatically, and never stored here. Runs are " +
            "refused until it is done.",
        ),
        ui.el("div", { class: "row" }, [authorize]),
      );
      return;
    }
    if (auth.canPush === false) {
      authHost.append(
        ui.banner("bad", "Authorised, but the app cannot write to this repository."),
        ui.el(
          "p",
          {},
          "That almost always means the GitHub App is not installed on this repository. " +
            "Installing and authorising are separate steps and both are needed.",
        ),
        ui.el("p", { class: "field-hint mono" }, `GitHub says: ${auth.reason || "--"}`),
        ui.el("div", { class: "row" }, [
          auth.installUrl
            ? ui.el(
                "a",
                {
                  class: "btn btn-primary",
                  href: auth.installUrl,
                  target: "_blank",
                  rel: "noopener noreferrer",
                },
                "Install the app on this repository",
              )
            : null,
          reauthorize,
        ]),
      );
      return;
    }
    authHost.append(
      ui.banner(
        auth.canPush === null ? "warn" : "good",
        auth.canPush === null
          ? "Authorised. GitHub could not be reached, so the write check did not run."
          : "Authorised, and a write to this repository was confirmed.",
      ),
      ui.el(
        "p",
        { class: "field-hint" },
        "Re-authorise only to change which GitHub account the agent acts as. Deregistering " +
          "this repository ends its authorisation; Disconnect on the GitHub App page ends " +
          "every repository's at once.",
      ),
      ui.el("div", { class: "row" }, [reauthorize]),
    );
  }

  function renderFacts() {
    ui.empty(facts);
    if (!config) return;
    facts.append(
      ...kv("Repository", config.repository || "--"),
      ...kv("Clone URL", config.repositoryUrl || "--"),
      ...kv("Configuration", ui.el("span", { class: "mono" }, config.configId || configId)),
      ...kv("Status", ui.el("span", { class: `chip tone-${statusTone(config.status)}` }, config.status || "unknown")),
      ...kv(
        "Runtime",
        config.agentRuntimeId
          ? ui.el(
              "span",
              { class: "mono" },
              `${config.agentRuntimeId}${config.agentRuntimeVersion ? ` v${config.agentRuntimeVersion}` : ""}`,
            )
          : "not provisioned yet",
      ),
      ...kv(
        "Memory",
        typeof config.memoryId === "string" && config.memoryId
          ? ui.el("span", { class: "mono truncate" }, config.memoryId)
          : "none: registered before memory, or provisioning did not create one",
      ),
      ...kv("Container", ui.el("span", { class: "mono truncate" }, config.containerUri || "--")),
      ...kv("Updated", ui.relTime(config.updatedAt)),
    );
  }

  /** The refusals, rewritten in place on every answer. */
  function applyRefusals() {
    const status = config?.status;
    ui.setRefusal(
      runtime,
      status === "READY"
        ? null
        : `${name()} is ${status || "not loaded yet"} -- a runtime update needs a READY ` +
            "repository, because replacing a runtime that is still being built would race " +
            "the build. The API refuses this too, with ConfigurationNotReady.",
    );
    ui.setRefusal(
      deregister,
      status === "DELETING"
        ? `${name()} is already being deregistered; the provisioner removes the ` +
            "configuration when the runtime, the memory and the role are gone."
        : null,
    );
  }

  function render() {
    ui.text(heading, name());
    ui.text(statusText, config?.status || "");
    renderNotes();
    applyRefusals();
    const next = signature();
    if (next !== drawn) {
      renderFacts();
      drawn = next;
    }
  }

  // -- the poll -----------------------------------------------------------------------

  // This page is where a registration is watched from until it is READY, and it is the
  // page that dispatches a runtime update -- so both of the things it shows change without
  // it asking. The poll backs off to 30 s once nothing is moving, and the payload is one
  // small item so the default signature (the JSON itself) is the right one.
  const poll = activity.createPoll({
    load: () => api.getConfiguration(configId),
    apply: (fresh) => {
      const before = config?.status;
      config = fresh;
      failure = null;
      seed();
      render();
      // The rail reads the same status out of its own cache; dropping it on a change means
      // the sidebar's next tick agrees with this page rather than contradicting it.
      if (fresh?.status !== before) configs.invalidate();
    },
    onError: (error) => {
      // A banner, not a toast: a failing poll would toast every six seconds, and this is
      // something that stays true until it is not.
      failure = error.message;
      renderNotes();
    },
    immediate: true,
  });

  root.append(
    ui.el("div", { class: "row" }, [
      ui.el("a", { class: "btn btn-quiet btn-small", href: boardHash(configId) }, "Back to board"),
      ui.el("a", { class: "btn btn-quiet btn-small", href: "#/register" }, "+ Add repository"),
    ]),
    ui.el("div", { class: "board-head" }, [heading, statusText]),
    notes,
    ui.panel("Repository", [facts]),
    ui.panel("Settings", [
      ui.el("div", { class: "form" }, [
        ...EDITABLE.map((spec) =>
          field(
            spec.label,
            inputs[spec.name],
            `${spec.hint} Emptying this field clears it back to per-run detection.`,
          ),
        ),
        ui.el("div", { class: "row" }, [save]),
      ]),
    ]),
    ui.panel("GitHub access", [authHost]),
    ui.panel("Runtime", [
      ui.el("p", { class: "field-hint" }, [
        "An update re-reads the intended runtime -- container image, environment, network " +
          "and lifecycle -- and converges the live one onto it. It is what to press after " +
          "a new container image is pushed.",
      ]),
      ui.el("div", { class: "row" }, [runtime]),
    ]),
    ui.panel("Deregister", [
      ui.el("p", { class: "field-hint" }, [
        "Two clicks, and the second one only counts after a moment: this deletes AWS " +
          "resources that take minutes to rebuild.",
      ]),
      ui.el("div", { class: "row" }, [deregister]),
      dangerHost,
    ]),
  );

  // Drawn once before the first answer so the page has its shape immediately; the poll's
  // `immediate` read is the load, so there is no second request for the same item.
  seed({ force: true });
  render();
  renderAuth();
  // Read once on mount and then only when something could have changed it -- a click on
  // Authorise, or this tab regaining focus after the operator came back from GitHub. It is
  // deliberately **not** on the six-second poll: the check makes three calls to GitHub, so
  // polling it would be a write probe against someone's repository every six seconds.
  void refreshAuth();
  document.addEventListener("visibilitychange", onVisible);
  configs.load().then(render).catch(() => {
    // The settings page works without the cache -- it reads its own configuration
    // directly, and only the pre-first-answer name comes from the list.
  });

  function onVisible() {
    // The return leg from the authorisation tab. The operator approves on GitHub, closes
    // that tab and this one becomes visible again -- which is the moment the vault's answer
    // has changed, and the only signal this page gets that it has.
    if (document.visibilityState === "visible") void refreshAuth();
  }

  return {
    destroy() {
      poll.stop();
      document.removeEventListener("visibilitychange", onVisible);
      ui.empty(root);
    },
  };
}
