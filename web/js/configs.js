/**
 * The registered repositories, cached once for the whole session.
 *
 * Every view needs them for something -- the sidebar draws them, the board header names
 * one, `transitions.moveRefusal` needs a `status` to say why a run cannot start -- and
 * `GET /configurations` is a DynamoDB `scan`, so fetching it per view would be the one
 * expensive read on the busiest path. It is cached, and every write that could change
 * it calls `invalidate()`: registration, a patch, a rotate, a runtime update, a
 * deregistration.
 *
 * `load()` is also single-flight, because the sidebar and the first routed view mount in
 * the same turn and would otherwise each start the same scan.
 */

import * as api from "./api.js";

// The route's own maximum page. There are as many configurations as there are
// registered repositories, so one page is the realistic whole list; `nextCursor` coming
// back non-null is reported by the sidebar rather than silently dropped.
const PAGE = 100;

let cache = null;
let truncated = false;
let loading = null;

/** Drop the cache. Called after every write, not on a timer: a stale sidebar is only
 * ever caused by a write, and a `CREATING` repository is watched by the poll. */
export function invalidate() {
  cache = null;
  truncated = false;
  loading = null;
}

/** Fetch the list if it is not cached, or unconditionally with `{force: true}`. */
export function load({ force = false } = {}) {
  if (force) invalidate();
  if (cache) return Promise.resolve(cache);
  if (!loading) {
    loading = api
      .listConfigurations({ limit: PAGE })
      .then((page) => {
        cache = page?.items || [];
        truncated = Boolean(page?.nextCursor);
        return cache;
      })
      .finally(() => {
        loading = null;
      });
  }
  return loading;
}

/** What is cached right now, without fetching. `[]` before the first `load()`. */
export function list() {
  return cache || [];
}

/** Whether the list on screen is the whole list. A silent cap reads as "that is all of
 * them", so the sidebar says so when it is not. */
export function isTruncated() {
  return truncated;
}

export function get(configId) {
  return list().find((config) => config.configId === configId) || null;
}

/** `READY` and nothing else. `UPDATING` is a live runtime being replaced and a run
 * dispatched against it would race the replacement, which is the server's rule too. */
export function isReady(configId) {
  return get(configId)?.status === "READY";
}

/** `owner/repo`, falling back to the id so a half-loaded cache still names something
 * an operator can match to what they clicked. */
export function repository(configId) {
  return get(configId)?.repository || configId;
}

/**
 * Where `#/` goes: the first `READY` repository, or the first of any status, or `null`
 * when nothing is registered (which routes to `#/register` instead).
 *
 * A `CREATING` repository is offered rather than skipped when it is all there is --
 * its board is empty but its provisioning status is the thing the operator wants to
 * watch, and a "nothing here" panel would hide it.
 */
export function defaultConfigId() {
  const ready = list().find((config) => config.status === "READY");
  return (ready || list()[0])?.configId || null;
}
