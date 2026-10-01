/**
 * The Cognito hosted UI, authorization-code flow with PKCE, and the only place a token
 * is read or written.
 *
 * The two `fetch` calls below go to the Cognito token endpoint, not to the control
 * plane -- which is why the "only `api.js` may `fetch`" rule exempts this file and
 * nothing else: a credential exchange must not travel through the API's own
 * bearer-header plumbing, because it is what produces the credential that plumbing
 * sends.
 *
 * PKCE with S256 and no client secret: this is a public client, the code verifier never
 * leaves the browser, and an intercepted authorization code is useless without it.
 *
 * `claims()` decodes the ID token payload and never checks its signature. That is not
 * an oversight -- the HTTP API's Cognito JWT authorizer is the verifier, and a
 * browser-side check would be a second, weaker one that an attacker who controls the
 * browser has already lost. The decode exists so the sidebar can print an email.
 */

// Session-scoped web storage, deliberately, and named exactly once below so the choice
// stays greppable: closing the browser signs you out, and that is the intended trade
// (PLAN Tier 1b). The origin-persistent alternative survives a closed laptop in a
// shared office, and is used nowhere in this console.
const tokenStore = sessionStorage;

const KEY_ID = "rca.idToken";
const KEY_ACCESS = "rca.accessToken";
const KEY_REFRESH = "rca.refreshToken";
const KEY_EXPIRES = "rca.expiresAt";
const KEY_VERIFIER = "rca.pkceVerifier";
const KEY_STATE = "rca.oauthState";

// `github_app.MANIFEST_RETURN_PARAM` / `MANIFEST_RETURN_VALUE`, which the API puts on the
// GitHub App manifest's `redirect_url`. Held together by
// `tests/test_web_console.py::test_the_manifest_return_marker_is_the_handlers_spelling` --
// a mismatch is silent, because sign-in keeps working and only the app conversion stops.
export const FOREIGN_FLOW_PARAM = "rca";
export const FOREIGN_FLOW_VALUE = "github-app";

// Refresh two minutes before expiry rather than on the 401: a board holds several
// requests in flight at once, and letting them all fail first turns one silent refresh
// into five visible errors.
export const REFRESH_LEAD_MS = 120000;

// One in-flight refresh, reused by every concurrent caller. A Cognito refresh token is
// reusable, but the *response* is not idempotent in effect: two exchanges racing would
// have the loser overwrite the winner's newer ID token.
let inFlight = null;

function settings() {
  return window.RCA_CONFIG || {};
}

function domain() {
  return String(settings().cognitoDomain || "").replace(/\/+$/, "");
}

function redirectUri() {
  // The hosted UI matches this against the app client's callback list exactly, so it is
  // the origin with a trailing slash and never `location.href` (which carries the hash
  // route and would never match).
  return `${window.location.origin}/`;
}

function base64url(bytes) {
  let ascii = "";
  for (const byte of bytes) ascii += String.fromCharCode(byte);
  return btoa(ascii).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function randomString(bytes) {
  return base64url(crypto.getRandomValues(new Uint8Array(bytes)));
}

async function challenge(verifier) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return base64url(new Uint8Array(digest));
}

function store(payload) {
  if (payload.id_token) tokenStore.setItem(KEY_ID, payload.id_token);
  if (payload.access_token) tokenStore.setItem(KEY_ACCESS, payload.access_token);
  // A refresh grant's response carries no `refresh_token`: keep the one we have rather
  // than clearing it, or the second refresh signs the operator out mid-run.
  if (payload.refresh_token) tokenStore.setItem(KEY_REFRESH, payload.refresh_token);
  const seconds = Number(payload.expires_in || 0);
  tokenStore.setItem(KEY_EXPIRES, String(Date.now() + seconds * 1000));
}

function clear() {
  for (const key of [KEY_ID, KEY_ACCESS, KEY_REFRESH, KEY_EXPIRES, KEY_VERIFIER, KEY_STATE]) {
    tokenStore.removeItem(key);
  }
}

async function exchange(form) {
  const response = await fetch(`${domain()}/oauth2/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    // Form-encoded and with no secret: a browser cannot keep one, so the app client is
    // configured without it and PKCE is what takes its place.
    body: new URLSearchParams({ client_id: String(settings().clientId || ""), ...form }),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = payload?.error_description || payload?.error || `HTTP ${response.status}`;
    throw new Error(`the sign-in exchange with Cognito failed: ${detail}`);
  }
  return payload;
}

/** Whether there is a token to send. Not whether it is still valid -- the authorizer
 * decides that, and `idToken()` refreshes on the way out. */
export function isSignedIn() {
  return Boolean(tokenStore.getItem(KEY_ID));
}

/** Leave for the hosted UI. Nothing after this call runs: the page navigates. */
export async function signIn() {
  const verifier = randomString(48);
  const state = randomString(12);
  tokenStore.setItem(KEY_VERIFIER, verifier);
  tokenStore.setItem(KEY_STATE, state);

  const url = new URL(`${domain()}/oauth2/authorize`);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("client_id", String(settings().clientId || ""));
  url.searchParams.set("redirect_uri", redirectUri());
  url.searchParams.set("scope", "openid email");
  url.searchParams.set("state", state);
  url.searchParams.set("code_challenge_method", "S256");
  url.searchParams.set("code_challenge", await challenge(verifier));
  window.location.assign(url.toString());
}

/**
 * Finish a redirect back from the hosted UI, if this load is one.
 *
 * Called once by `app.js` at boot. Returns whether there is a session afterwards, so a
 * plain load with no `?code=` is one call and no special case.
 */
export async function complete() {
  const query = new URLSearchParams(window.location.search);
  // `?code=` is **not ours alone**. `views/connect.js` brings the operator back from
  // GitHub's app-manifest flow on this same origin, also with a `code`, and this function
  // runs first at boot -- so without this guard it claimed GitHub's single-use code, stripped
  // the query before the connect view could read it, and then threw "that sign-in did not
  // start in this tab" because the sign-in state was consumed by the real sign-in long ago.
  // The app was created on GitHub and the conversion could never happen.
  //
  // The marker is on the `redirect_url` this deployment hands GitHub
  // (`github_app.MANIFEST_RETURN_PARAM`/`_VALUE`), so it is a positive statement of whose
  // code this is rather than a guess from which keys the token store happens to hold.
  // Returning before the strip below is the point: the query has to survive for `connect.js`.
  if (query.get(FOREIGN_FLOW_PARAM) === FOREIGN_FLOW_VALUE) return isSignedIn();
  const code = query.get("code");
  const failure = query.get("error");
  if (!code && !failure) return isSignedIn();

  // The query string goes whatever happens: an authorization code is single-use, and
  // leaving it in the address bar means a reload tries to spend it again and fails with
  // `invalid_grant` on a session that is actually fine.
  const verifier = tokenStore.getItem(KEY_VERIFIER);
  const expected = tokenStore.getItem(KEY_STATE);
  tokenStore.removeItem(KEY_VERIFIER);
  tokenStore.removeItem(KEY_STATE);
  window.history.replaceState({}, "", `${window.location.pathname}${window.location.hash}`);

  if (failure) {
    throw new Error(`Cognito refused the sign-in: ${query.get("error_description") || failure}`);
  }
  if (!expected || query.get("state") !== expected) {
    // A mismatched `state` is either a cross-site request forgery or a stale tab. Both
    // are refused the same way, and neither spends the code.
    throw new Error("that sign-in did not start in this tab; sign in again");
  }

  store(
    await exchange({
      grant_type: "authorization_code",
      code,
      redirect_uri: redirectUri(),
      code_verifier: verifier || "",
    }),
  );
  return isSignedIn();
}

/**
 * The ID token to send, refreshed first if it is within `REFRESH_LEAD_MS` of expiry.
 *
 * The **ID** token because the API's authorizer validates it against the app client id
 * as audience. Returns `""` rather than throwing when there is nothing at all: the
 * request then 401s and `app.js` shows the sign-in panel, which is a better failure
 * than a rejected promise from a getter.
 */
export async function idToken() {
  const token = tokenStore.getItem(KEY_ID);
  const expiresAt = Number(tokenStore.getItem(KEY_EXPIRES) || 0);
  if (token && Date.now() < expiresAt - REFRESH_LEAD_MS) return token;
  if (tokenStore.getItem(KEY_REFRESH)) return refresh();
  return token || "";
}

/** Exchange the refresh token for a new ID token. Single-flight. */
export function refresh() {
  if (inFlight) return inFlight;
  const token = tokenStore.getItem(KEY_REFRESH);
  if (!token) return Promise.resolve(tokenStore.getItem(KEY_ID) || "");

  inFlight = exchange({ grant_type: "refresh_token", refresh_token: token })
    .then((payload) => {
      store(payload);
      return tokenStore.getItem(KEY_ID) || "";
    })
    .catch((error) => {
      // A refused refresh means the session is over, so drop it: the next render finds
      // no token and offers to sign in, instead of retrying a dead grant forever.
      clear();
      throw new Error(`your session has ended and could not be renewed (${error.message})`);
    })
    .finally(() => {
      inFlight = null;
    });
  return inFlight;
}

/** Forget the session here, then end it at the pool. Both halves matter: clearing only
 * this side leaves the hosted UI ready to sign the same operator straight back in. */
export function signOut() {
  clear();
  const url = new URL(`${domain()}/logout`);
  url.searchParams.set("client_id", String(settings().clientId || ""));
  url.searchParams.set("logout_uri", redirectUri());
  window.location.assign(url.toString());
}

/** The ID token's payload, decoded and not verified. `{}` when there is no session or
 * the token is not a JWT -- the sidebar prints an email, and a missing one is a missing
 * line rather than a broken console. */
export function claims() {
  const token = tokenStore.getItem(KEY_ID);
  if (!token) return {};
  try {
    const payload = token.split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(payload)) || {};
  } catch {
    return {};
  }
}
