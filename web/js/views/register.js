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
 * 1. **The API is the only validator.** There is no URL grammar and no token shape check
 *    here, deliberately: `configurations._repository_from_url` accepts two forms and
 *    `_reject_classic_pat` refuses a whole family of tokens, and both answer with a
 *    sentence written for the operator. A second copy of either regex in this file would
 *    drift from the server's, and the drift would show up as a console refusing a URL the
 *    API would have taken. The `InvalidRepositoryUrl` / `ClassicTokenRejected` message is
 *    shown verbatim.
 * 2. **An emptied setting is `null`, not `""`.** `configurations.patch` REMOVEs on `null`,
 *    which puts `verifyCommand`, `setupCommand` or `defaultBranch` back to per-run
 *    detection, while a stored empty string is a *declared* "run nothing". Clearing a
 *    field in this form therefore sends `null` and the hint under it says so.
 * 3. **The token leaves this page immediately.** It is the one value in the system that
 *    cannot be read back, so the field is emptied as soon as the request is made and it is
 *    never held in a variable that outlives the call.
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

// Where a fine-grained token is made. The page is linked rather than described because the
// path through Settings -> Developer settings -> Personal access tokens -> Fine-grained is
// four levels deep and is where the wrong token type gets chosen.
//
// Only `description` is passed, and it is **best-effort**: GitHub documents no query
// parameters for this page -- the long-standing `?scopes=` on the *classic* page is
// unofficial and there is no fine-grained equivalent -- so the repository and the two
// permissions cannot be pre-selected from a URL. They are spelled out beside the link
// instead. If GitHub ignores `description`, nothing breaks; the link still lands on the
// right page, which is most of what was confusing.
const TOKEN_PAGE = "https://github.com/settings/personal-access-tokens/new";

export function tokenPageUrl(slug) {
  if (!slug) return TOKEN_PAGE;
  return `${TOKEN_PAGE}?description=${encodeURIComponent(`remote coding agent -- ${slug}`)}`;
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
  const tokenField = ui.el("input", {
    type: "password",
    autocomplete: "off",
    spellcheck: "false",
    "aria-label": "GitHub token",
    placeholder: "github_pat_...",
  });
  // The link the token hint used to describe in prose. It tracks `urlField`, so by the time
  // an operator reaches the token field the target already names their repository -- which is
  // the value they would otherwise have to carry from one tab to the other and re-select from
  // a list.
  const tokenLink = ui.el("a", {
    class: "field-hint",
    href: TOKEN_PAGE,
    target: "_blank",
    // `noopener` because this opens a page where a credential is about to be created.
    rel: "noopener noreferrer",
  });
  const tokenScope = ui.el("span", { class: "field-hint" });

  function refreshTokenLink() {
    const slug = repositorySlug(urlField.value);
    tokenLink.setAttribute("href", tokenPageUrl(slug));
    tokenLink.textContent = slug
      ? `Create a fine-grained token for ${slug} on GitHub`
      : "Create a fine-grained token on GitHub";
    ui.empty(tokenScope);
    ui.append(tokenScope, [
      "On that page: Repository access -> ",
      ui.el("strong", {}, "Only select repositories"),
      slug ? ` -> ${slug}. Then Repository permissions -> ` : " -> your repository. Then Repository permissions -> ",
      ui.el("strong", {}, "Contents: Read and write"),
      " and ",
      ui.el("strong", {}, "Pull requests: Read and write"),
      ". Nothing else. If the owner is an organisation, choose it as Resource owner -- " +
        "organisations have to enable fine-grained tokens before that appears.",
    ]);
  }
  urlField.addEventListener("input", refreshTokenLink);
  refreshTokenLink();

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
    // Verbatim. `InvalidRepositoryUrl` names both accepted forms and
    // `ClassicTokenRejected` explains why a classic PAT cannot be scoped to one
    // repository -- wording this view could only make worse.
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
      ]),
    );
  }

  async function register() {
    const repositoryUrl = urlField.value.trim();
    if (!repositoryUrl || !tokenField.value) {
      // Presence, not shape: the API refuses an absent field with `MissingField` and a
      // round trip to be told a field is empty is a request that did not need making.
      ui.toast("a repository URL and a GitHub token are both required", "warn");
      return;
    }

    const body = { repositoryUrl, githubToken: tokenField.value };
    for (const spec of EDITABLE) {
      const value = optional[spec.name].value.trim();
      // Absent rather than empty, matching `configurations.create`: an omitted field falls
      // back to per-run detection and an empty string would be a declaration.
      if (value) body[spec.name] = value;
    }

    ui.setRefusal(submit, "the registration is already in flight");
    try {
      const config = await api.createConfiguration(body);
      // Out of the page as soon as it is sent, and never held anywhere else.
      tokenField.value = "";
      configs.invalidate();
      await configs.load().catch(() => []);
      ui.toast(
        `${config.repository} is registered. Its runtime, execution role and secret are ` +
          "provisioning; that takes about ten minutes and the sidebar shows the status.",
        "good",
      );
      // Straight to the board, which is where the status is watched from. Registration
      // answers 201 with the item, so the id is known without a second read.
      window.location.hash = boardHash(config.configId);
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
        ui.el("label", { class: "field" }, [
          ui.el("span", { class: "field-label" }, "GitHub token"),
          tokenField,
          ui.el(
            "span",
            { class: "field-hint" },
            "A classic token is refused -- it cannot be scoped to one repository at all. This " +
              "one goes straight into Secrets Manager and is never readable again, not by this " +
              "console and not by the API.",
          ),
          tokenLink,
          tokenScope,
        ]),
        // The three optional fields are folded away, and that is the whole point: they were
        // presented at the same level as the two required ones, so a first-time operator read
        // five inputs to fill and hunted for values that are better left empty. `<details>` and
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
      // No timers here, so teardown is just the DOM -- and emptying it is also what takes
      // the token field out of the document.
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

  const rotateField = ui.el("input", {
    type: "password",
    autocomplete: "off",
    spellcheck: "false",
    "aria-label": "New GitHub token",
    placeholder: "github_pat_...",
  });
  const rotate = ui.action("Rotate token", { class: "btn", onClick: rotateToken });

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
      config.secretArn,
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

  async function rotateToken() {
    if (!rotateField.value) {
      ui.toast("paste the new token into the field first", "warn");
      return;
    }
    try {
      await api.rotateToken(configId, rotateField.value);
      rotateField.value = "";
      ui.toast(
        "a new version of the secret is stored, and the next run reads it. The runtime, " +
          "the execution role and the secret's ARN are unchanged, so nothing has to be " +
          "reprovisioned.",
        "good",
      );
    } catch (error) {
      // `ClassicTokenRejected` again, and `NoSecret` for a configuration whose
      // registration failed before the secret was written. Both sentences are the API's.
      ui.toast(error.message, "bad");
    }
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
      // The ARN and not the token: the secret is never readable, and its ARN is what the
      // execution role's inline policy names.
      ...kv("Secret", ui.el("span", { class: "mono truncate" }, config.secretArn || "--")),
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
            "configuration when the runtime, the role and the secret are gone."
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
    ui.panel("GitHub token", [
      ui.el("div", { class: "form" }, [
        field(
          "New token",
          rotateField,
          "rotation writes a new version of the same secret, so the ARN the execution " +
            "role names does not change. The old version is not deleted here -- do that " +
            "in GitHub by revoking the token you replaced.",
        ),
        ui.el("div", { class: "row" }, [rotate]),
      ]),
    ]),
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
  configs.load().then(render).catch(() => {
    // The settings page works without the cache -- it reads its own configuration
    // directly, and only the pre-first-answer name comes from the list.
  });

  return {
    destroy() {
      poll.stop();
      ui.empty(root);
    },
  };
}
