// Front-end UI-state reducer tests — node:test, zero dependencies (spec §11).
// Run: node --test 'tests/frontend/**/*.test.mjs'
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ALL, initialState, isBusy, isDegraded, reduce, resultRevision, showResults, stopAvailable,
} from "../../seestar/gui/static/state.js";

const REQUEST = { library_dir: "/lib", which: "M8", profile: "showcase", output_root: "/out", use_recipe: false };
const RUNNING = { id: "j1", seq: 1, state: "running", request: REQUEST, status_text: "⏳ M8 …",
  results: [], summary: null, stop: { requested: false, available: false }, skipped: [] };
const DONE = { ...RUNNING, state: "done", status_text: null, summary: "✅ Finished: M8 · Showcase style. Saved in /out" };

function init(job, prefs = { library: "/lib", output: "/out", profile: "showcase" }) {
  return reduce(initialState, { type: "init", mode: "browser", prefs, job });
}

test("initial attach with no job", () => {
  const s = init(null);
  assert.equal(s.selection.library, "/lib");
  assert.equal(isBusy(s), false);
  assert.equal(showResults(s), false);
});

test("a running job is authoritative over lagging saved preferences", () => {
  const s = init(RUNNING, { library: "/stale", output: "/old", profile: "natural" });
  assert.deepEqual(s.selection, {
    library: "/lib", which: "M8", profile: "showcase", output: "/out",
  });
  assert.equal(showResults(s), true);
});

test("reload during a run reattaches: busy, selection adopted from the job", () => {
  const s = init(RUNNING);
  assert.equal(isBusy(s), true);
  assert.equal(s.selection.which, "M8"); // adopted so the match rule can hold
  assert.equal(showResults(s), true);
});

test("finished-while-unwatched render appears on reload when selection matches", () => {
  const s = init(DONE);
  assert.equal(isBusy(s), false);
  assert.equal(showResults(s), true);
});

test("job under a DIFFERENT saved selection is not adopted and not shown", () => {
  const s = init(DONE, { library: "/other", output: "/out", profile: "showcase" });
  assert.equal(s.selection.which, "");
  assert.equal(showResults(s), false);
});

test("changing any selection field hides the results view (stale-result rule)", () => {
  for (const [field, value] of [["library", "/elsewhere"], ["which", "M17"], ["profile", "natural"], ["output", "/new"]]) {
    const s = reduce(init(DONE), { type: "field-changed", field, value });
    assert.equal(showResults(s), false, field);
  }
});

test("changing the library also clears the scan", () => {
  let s = init(null);
  s = reduce(s, { type: "scanned", view: { rows: [["M8"]], choices: [{ label: "M8", value: "M8" }], value: "M8", message: "Found 1 photo. Choose one below.", detail: null } });
  assert.equal(s.selection.which, "M8"); // single target auto-selects
  s = reduce(s, { type: "field-changed", field: "library", value: "/other" });
  assert.equal(s.scan, null);
  assert.equal(s.selection.which, "");
});

test("second page during a run is busy without having started anything", () => {
  const s = init(RUNNING);
  assert.equal(isBusy(s), true);
  assert.equal(stopAvailable(s), false); // single-target job: no boundary
});

test("stop is offered exactly when the job says so", () => {
  const batch = { ...RUNNING, stop: { requested: false, available: true } };
  assert.equal(stopAvailable(init(batch)), true);
});

test("three failed polls -> degraded; a successful poll recovers", () => {
  let s = init(RUNNING);
  s = reduce(s, { type: "poll-failed" });
  s = reduce(s, { type: "poll-failed" });
  assert.equal(isDegraded(s), false);
  s = reduce(s, { type: "poll-failed" });
  assert.equal(isDegraded(s), true);
  s = reduce(s, { type: "poll-ok", job: RUNNING });
  assert.equal(isDegraded(s), false);
});

test("terminal poll keeps the results visible with the summary", () => {
  let s = init(RUNNING);
  s = reduce(s, { type: "poll-ok", job: DONE });
  assert.equal(isBusy(s), false);
  assert.equal(showResults(s), true);
  assert.equal(s.job.summary.startsWith("✅"), true);
});

test("an older response for the same job cannot overwrite newer state", () => {
  let s = init({ ...RUNNING, seq: 5 });
  s = reduce(s, { type: "poll-ok", job: { ...DONE, seq: 7 } });
  s = reduce(s, { type: "poll-ok", job: { ...RUNNING, seq: 6 } });
  assert.equal(s.job.state, "done");
  assert.equal(s.job.seq, 7);
});

test("result revision changes only when result-bearing state changes", () => {
  let s = init({ ...RUNNING, seq: 1, status_text: "first" });
  const first = resultRevision(s);
  s = reduce(s, { type: "poll-ok", job: { ...RUNNING, seq: 2, status_text: "second" } });
  assert.equal(resultRevision(s), first);
  s = reduce(s, { type: "poll-ok", job: {
    ...RUNNING, seq: 3, results: [{ target: "M8", images: [] }],
  } });
  assert.notEqual(resultRevision(s), first);
});

test("render-started resets failures and shows the new job", () => {
  let s = init(null);
  s = reduce(s, { type: "field-changed", field: "which", value: "M8" });
  s = reduce(s, { type: "poll-failed" });
  s = reduce(s, { type: "render-started", job: RUNNING });
  assert.equal(isBusy(s), true);
  assert.equal(s.pollFailures, 0);
});

test("use_recipe is excluded from the selection match (spec §4)", () => {
  const replayJob = { ...DONE, request: { ...REQUEST, use_recipe: true } };
  assert.equal(showResults(init(replayJob)), true);
});

test("a replaced job id adopts the server's current job instead of degrading", () => {
  let s = init(RUNNING);
  s = reduce(s, { type: "job-replaced", job: { ...RUNNING, id: "j2" } });
  assert.equal(s.job.id, "j2");
  assert.equal(isDegraded(s), false);
  s = reduce(s, { type: "job-replaced", job: null });
  assert.equal(s.job, null);
});

test("ALL matches the server's batch sentinel", () => {
  assert.equal(ALL, "(all targets)");
});

// ---------------------------------------------------------------------------
// Choosing a DIFFERENT folder must stick (owner report, 2026-08-21: picking a
// new library appeared to "revert" to the previously saved one). The reducer
// half of that promise is pinned here; the pickFolder half lives in app.js.
// ---------------------------------------------------------------------------

test("a newly chosen library replaces the saved one and SURVIVES later polls", () => {
  let s = init(DONE, { library: "/old/NGC7000", output: "/out", profile: "showcase" });
  assert.equal(s.selection.library, "/old/NGC7000");

  s = reduce(s, { type: "field-changed", field: "library", value: "/new/M8" });
  assert.equal(s.selection.library, "/new/M8");

  // Polling continues while the user reads the screen. A poll must never put the
  // old folder back -- that is exactly what "it reverts" would look like.
  s = reduce(s, { type: "poll-ok", job: DONE });
  assert.equal(s.selection.library, "/new/M8", "a poll reverted the user's chosen folder");

  s = reduce(s, { type: "job-replaced", job: DONE });
  assert.equal(s.selection.library, "/new/M8", "a replaced job reverted the user's chosen folder");
});

test("a FINISHED job's request never overwrites a newly chosen folder", () => {
  // adoptRunningSelection is authoritative only while a job is RUNNING; a
  // done/failed job's request must not drag the fields back to what it used.
  let s = init(DONE);
  s = reduce(s, { type: "field-changed", field: "output", value: "/new/out" });
  s = reduce(s, { type: "poll-ok", job: DONE });
  assert.equal(s.selection.output, "/new/out");
});

test("switching library twice keeps the LAST choice", () => {
  let s = init(null);
  s = reduce(s, { type: "field-changed", field: "library", value: "/first" });
  s = reduce(s, { type: "field-changed", field: "library", value: "/second" });
  assert.equal(s.selection.library, "/second");
});
