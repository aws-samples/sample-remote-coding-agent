/**
 * The 17 control-plane routes the console calls, and the only `fetch(` that carries an
 * `Authorization` header.
 *
 * One private `request` so the four things that must be identical on every call are
 * written once: the bearer token, the JSON content type, the error envelope, and the
 * single retry after a refresh. A view that reached for `fetch` directly would get
 * three of those right and drop the fourth, and the one it dropped would be the token
 * refresh -- which fails only after twenty minutes on an open board.
 *
 * The route table mirrors the one `tests/test_api_route_contract.py` asserts, and
 * nothing else: `POST /tasks/{taskId}/cancel` exists on the API and deliberately has no
 * wrapper here yet -- an absence declared with its reason in
 * `tests/test_client_api_contract.py::CONSOLE_NOT_CALLED`.
 *
 * Two shapes the server insists on and every caller has to honour:
 *
 * * `GET /cards` requires `configId` -- a board is one repository's cards, and without
 *   the partition key the server would be scanning every repository's;
 * * **every** `/tasks...` route requires `configId` as a query parameter, because
 *   `Tasks` is keyed `configId` + `taskId` and has no GSI. That is why `getTask` takes
 *   two arguments and why the run route in the hash is `#/tasks/<configId>/<taskId>`.
 */

import * as auth from "./auth.js";

// Same-origin: CloudFront serves this bundle and forwards `/api/*` to the HTTP API, so
// there is no CORS anywhere. The `/api/` prefix is part of the route *on the server*
// (there is no stage-level rewrite), and it is carried here by the base alone -- a path
// below is never prefixed, stripped or rewritten per call.
const DEFAULT_BASE = "/api";

/** A response the API chose, with the sentence it chose. */
export class ApiError extends Error {
  constructor(status, code, message, details = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function base() {
  return String(window.RCA_CONFIG?.apiBase || DEFAULT_BASE).replace(/\/+$/, "");
}

async function payloadOf(response) {
  // A 500 from API Gateway itself is not JSON, and neither is a 403 from CloudFront.
  // Both still have to produce a readable error rather than a parse failure.
  try {
    return await response.json();
  } catch {
    return null;
  }
}

async function request(method, path, { query, body, retried = false } = {}) {
  const url = new URL(base() + path, window.location.origin);
  for (const [key, value] of Object.entries(query || {})) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }

  // The **ID** token, not the access token: the HTTP API's Cognito JWT authorizer is
  // configured with the app client id as its audience, and only the ID token carries
  // that as `aud`. An access token here is a 401 with nothing in the log to explain it.
  const headers = { Authorization: `Bearer ${await auth.idToken()}` };
  if (body !== undefined) headers["content-type"] = "application/json";

  const response = await fetch(url, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = await payloadOf(response);
  if (response.ok) return payload;

  if (response.status === 401 && !retried) {
    // Exactly one silent retry, and only on a 401: an ID token can expire between the
    // freshness check and the request landing. A second retry would loop against a
    // session the pool has genuinely ended.
    await auth.refresh();
    return request(method, path, { query, body, retried: true });
  }

  const envelope = payload && typeof payload === "object" ? payload : {};
  const { error, message, ...details } = envelope;
  // `message` is written for a human by the handler that refused -- `RunInFlight`,
  // `CardMoved`, `ClassicTokenRejected` all arrive as sentences. Every view shows it
  // verbatim rather than inventing wording that will drift from the server's.
  throw new ApiError(
    response.status,
    error || `Http${response.status}`,
    message || `the API answered ${response.status} with no explanation`,
    details,
  );
}

// One escaper for every path parameter. A card id is generated, but a `configId`
// arrives from the hash and an operator can paste anything into it.
const seg = (...parts) => parts.map((part) => encodeURIComponent(String(part))).join("/");

// -- configurations ------------------------------------------------------------------

export const listConfigurations = (query = {}) => request("GET", "/configurations", { query });
export const getConfiguration = (configId) => request("GET", `/configurations/${seg(configId)}`);
export const createConfiguration = (body) => request("POST", "/configurations", { body });
export const patchConfiguration = (configId, body) =>
  request("PATCH", `/configurations/${seg(configId)}`, { body });
export const deleteConfiguration = (configId) =>
  request("DELETE", `/configurations/${seg(configId)}`);
export const runtimeUpdate = (configId) =>
  request("POST", `/configurations/${seg(configId)}/runtime-update`);
// A new *version* of the one secret, which is why this is a PUT and not a POST.
// -- the GitHub credential -----------------------------------------------------------
//
// Four deployment-wide calls plus two per repository. None of them carries a credential in
// either direction: `connectGitHubApp` returns a *manifest* for the browser to POST to
// GitHub, `convertGitHubApp` hands the API a single-use code, and the two per-repository
// calls deal only in an authorisation URL and a verdict. There is deliberately no
// `rotateToken` any more -- there is no stored token to rotate.

export const getGitHubApp = () => request("GET", "/github-app");
export const connectGitHubApp = (consoleOrigin) =>
  request("POST", "/github-app", { body: { consoleOrigin } });
export const convertGitHubApp = (code) =>
  request("POST", "/github-app/conversions", { body: { code } });
export const disconnectGitHubApp = () => request("DELETE", "/github-app");

export const authorizeGitHub = (configId, { force = false } = {}) =>
  request("POST", `/configurations/${seg(configId)}/github-authorize`, {
    body: { consoleOrigin: window.location.origin, force },
  });
export const completeGitHubAuthorization = (configId, sessionUri) =>
  request("POST", `/configurations/${seg(configId)}/github-authorize/complete`, {
    body: { sessionUri },
  });
export const githubStatus = (configId) =>
  request("GET", `/configurations/${seg(configId)}/github-status`);

// -- cards ---------------------------------------------------------------------------

export const listCards = (configId, query = {}) =>
  request("GET", "/cards", { query: { configId, ...query } });
export const getCard = (cardId) => request("GET", `/cards/${seg(cardId)}`);
export const createCard = (body) => request("POST", "/cards", { body });
export const patchCard = (cardId, body) => request("PATCH", `/cards/${seg(cardId)}`, { body });
export const deleteCard = (cardId) => request("DELETE", `/cards/${seg(cardId)}`);
// The only door to a run. The server decides whether the column is legal; the console
// asks, and quotes the answer.
export const moveCard = (cardId, column) =>
  request("POST", `/cards/${seg(cardId)}/move`, { body: { column } });

// -- tasks ---------------------------------------------------------------------------

export const createTask = (body) => request("POST", "/tasks", { body });
export const listTasks = (configId, query = {}) =>
  request("GET", "/tasks", { query: { configId, ...query } });
export const getTask = (configId, taskId) =>
  request("GET", `/tasks/${seg(taskId)}`, { query: { configId } });
export const getActivity = (configId, taskId, query = {}) =>
  request("GET", `/tasks/${seg(taskId)}/activity`, { query: { configId, ...query } });
