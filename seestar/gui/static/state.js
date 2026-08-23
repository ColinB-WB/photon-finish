// PURE UI-state reducer — no DOM, no fetch, no timers (spec §3). app.js owns all
// side effects; tests/frontend/state.test.mjs exercises every transition.

export const ALL = "(all targets)"; // must equal seestar.gui.views.ALL_TARGETS (pinned by test_gui_static.py)

export const initialState = {
  mode: "browser",                       // "window" | "browser" (from /api/state)
  selection: { library: "", which: "", profile: "showcase", output: "" },
  scan: null,                            // /api/scan view model, or null
  fineTune: false,                       // /api/fine-tune availability
  job: null,                             // latest job snapshot, or null
  pollFailures: 0,
  notice: "",                            // transient server message (409 busy, quit, picker…)
};

export function isBusy(state) {
  return !!state.job && state.job.state === "running";
}

export function isDegraded(state) {
  return state.pollFailures >= 3;
}

export function stopAvailable(state) {
  return isBusy(state) && !!state.job.stop && state.job.stop.available;
}

// A cheap key for the expensive gallery/QA DOM. Progress text may change every
// second without destroying and recreating image elements.
export function resultRevision(state) {
  const job = state.job;
  if (!job) return "";
  return `${job.id}:${job.state}:${(job.results || []).length}`;
}

function adoptRunningSelection(selection, job) {
  if (!job || job.state !== "running" || !job.request) return selection;
  return {
    library: job.request.library_dir,
    which: job.request.which,
    profile: job.request.profile,
    output: job.request.output_root,
  };
}

// Stale-result rule (spec §4): a job's results render only while the normalized
// selection matches that job's request. use_recipe is an action, not a selection.
export function showResults(state) {
  const job = state.job;
  if (!job || !job.request) return false;
  const sel = state.selection;
  return job.request.library_dir === sel.library.trim()
    && job.request.which === sel.which
    && job.request.profile === sel.profile
    && job.request.output_root === sel.output.trim();
}

export function reduce(state, event) {
  switch (event.type) {
    case "init": {
      const prefs = event.prefs || {};
      let selection = {
        library: prefs.library || "",
        which: "",
        profile: prefs.profile || "showcase",
        output: prefs.output || "",
      };
      const job = event.job || null;
      // An active job is authoritative. This makes a second/reloaded page show
      // what the engine is actually doing even if best-effort prefs lagged.
      selection = adoptRunningSelection(selection, job);
      // `which` is not persisted; adopt it from the last job when every persisted
      // field matches, so a finished-while-unwatched render appears (spec §4) —
      // but never under a mismatched library/style/save-folder.
      if (job && job.request
          && job.request.library_dir === selection.library.trim()
          && job.request.output_root === selection.output.trim()
          && job.request.profile === selection.profile) {
        selection.which = job.request.which;
      }
      return { ...initialState, mode: event.mode || "browser", selection, job };
    }
    case "scanned":
      return { ...state, scan: event.view, notice: "",
        selection: { ...state.selection, which: event.view.value || "" } };
    case "field-changed": {
      const selection = { ...state.selection, [event.field]: event.value };
      if (event.field === "library") {
        // A changed source invalidates its scan and (via showResults) any visible
        // result — parity with gui.py:807-812.
        selection.which = "";
        return { ...state, selection, scan: null, notice: "" };
      }
      return { ...state, selection, notice: "" };
    }
    case "render-started":
      return { ...state, job: event.job,
        selection: adoptRunningSelection(state.selection, event.job),
        pollFailures: 0, notice: "" };
    case "poll-ok":
      if (state.job && event.job && state.job.id === event.job.id
          && Number(event.job.seq || 0) < Number(state.job.seq || 0)) {
        return { ...state, pollFailures: 0 };
      }
      return { ...state, job: event.job, pollFailures: 0 };
    case "poll-failed":
      return { ...state, pollFailures: state.pollFailures + 1 };
    case "job-replaced":
      // /api/jobs/{id} 404'd but the server answered /api/state: the job was
      // superseded by a newer one. Reachable server -> failures reset.
      return { ...state, job: event.job || null,
        selection: adoptRunningSelection(state.selection, event.job), pollFailures: 0 };
    case "fine-tune":
      return { ...state, fineTune: !!event.available };
    case "notice":
      return { ...state, notice: event.message || "" };
    default:
      return state;
  }
}
