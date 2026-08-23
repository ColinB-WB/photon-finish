// DOM wiring + fetch + the 1 s polling loop. All rendering of server values goes
// through textContent / attributes — never innerHTML (spec §5). The state logic
// itself lives in state.js.

import {
  ALL, initialState, isBusy, isDegraded, reduce, resultRevision, showResults, stopAvailable,
} from "./state.js";

const $ = (id) => document.getElementById(id);
let state = initialState;
let pollTimer = null;
let pollInFlight = false;
let renderedResultRevision = null;
let renderedResultsVisible = false;

// Static shell copy (characterized): the degraded state is detected CLIENT-side
// (the server is unreachable), so its copy must live here, not in views.py.
const DEGRADED_MESSAGE = "Photon Finish can't reach its processing engine — the app may have quit. Your photos and any completed images are safe.";

// NO DEADLINE for the folder chooser. Browse has exactly two outcomes -- the user
// cancels (keep the current value) or picks a folder (replace it) -- and a client
// deadline invents a third that silently keeps the old value while the dialog
// returns the right answer to nobody. That was the 2026-08-22 defect: a blanket 10s
// abort against a dialog waiting on a HUMAN. A bigger number is not the fix; the
// deadline is the fix. Window mode's native dialog has no timeout at all, and
// browser mode's subprocess picker bounds itself (pickers.py), so the server always
// answers eventually.
const NO_DEADLINE = 0;
// A scan walks a library that may live on cloud storage with a cold cache -- a
// filesystem operation of unbounded size, not a quick RPC. It has no human in the
// loop, so a generous deadline is still meaningful here.
const SCAN_TIMEOUT_MS = 120000;

async function api(path, { method = "GET", body, timeoutMs = 10000 } = {}) {
  // timeoutMs = 0 means NO deadline: for a request whose duration is a human's
  // decision, aborting is never the right answer (see NO_DEADLINE above).
  const controller = new AbortController();
  const timeout = timeoutMs > 0 ? setTimeout(() => controller.abort(), timeoutMs) : null;
  const options = { method, headers: { "X-Photon-Finish": "1" } };
  if (timeout !== null) options.signal = controller.signal;
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    // Network-level failure (connection refused: the app quit or crashed).
    // Uniform shape: actions surface the degraded copy via their notice paths,
    // poll()/boot() count it toward the degraded state — never a silent throw.
    return { ok: false, status: 0, data: { message: DEGRADED_MESSAGE } };
  } finally {
    if (timeout !== null) clearTimeout(timeout);
  }
  let data = {};
  try { data = await response.json(); } catch { /* non-JSON (e.g. images) */ }
  return { ok: response.ok, status: response.status, data };
}

function dispatch(event) {
  state = reduce(state, event);
  render();
  ensurePolling();
}

// ---- rendering --------------------------------------------------------------

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const child of children) node.appendChild(child);
  return node;
}

function setInput(input, value) {
  if (document.activeElement !== input && input.value !== value) input.value = value;
}

function render() {
  const busy = isBusy(state);
  const show = showResults(state);

  setInput($("library"), state.selection.library);
  setInput($("output"), state.selection.output);
  if ($("profile").value !== state.selection.profile) $("profile").value = state.selection.profile;
  renderWhich();
  renderScan();

  for (const id of ["library", "library-browse", "scan", "which", "profile", "output", "output-browse", "render", "open-recipe", "rerender", "quit"]) {
    $(id).disabled = busy;
  }
  $("render").textContent = busy ? "Creating…" : "Create finished photo";
  $("quit-row").hidden = state.mode !== "browser";
  $("fine-tune").hidden = !state.fineTune || busy;
  $("stop").hidden = !stopAvailable(state);

  const status = $("status");
  status.classList.toggle("degraded", isDegraded(state));
  if (isDegraded(state)) status.textContent = DEGRADED_MESSAGE;
  else if (state.notice) status.textContent = state.notice;
  else if (busy) status.textContent = state.job.status_text || "";
  else if (show && state.job.summary) status.textContent = state.job.summary;
  else status.textContent = "";

  const revision = show ? resultRevision(state) : "";
  if (show !== renderedResultsVisible || revision !== renderedResultRevision) {
    renderResults(show);
    renderedResultsVisible = show;
    renderedResultRevision = revision;
  }
}

function renderWhich() {
  const select = $("which");
  select.replaceChildren();
  const choices = state.scan ? state.scan.choices : [];
  select.appendChild(el("option", { value: "", text: "" }));
  for (const c of choices) select.appendChild(el("option", { value: c.value, text: c.label }));
  if (state.selection.which && !choices.some((c) => c.value === state.selection.which)) {
    // reattach case: the selection came from the last job, before any scan
    select.appendChild(el("option", { value: state.selection.which, text: state.selection.which }));
  }
  select.value = state.selection.which;
}

function renderScan() {
  const box = $("targets");
  box.replaceChildren();
  const message = $("scan-message");
  const detail = $("scan-detail");
  if (!state.scan) {
    box.appendChild(el("div", { class: "empty-table", text: "No photos found yet." }));
    message.textContent = "";
    detail.hidden = true;
    return;
  }
  message.textContent = state.scan.message || "";
  detail.hidden = !state.scan.detail;
  $("scan-detail-text").textContent = state.scan.detail || "";
  if (!state.scan.rows.length) {
    box.appendChild(el("div", { class: "empty-table", text: "No photos found yet." }));
    return;
  }
  const head = el("tr", {}, ["Photo", "Filter", "Sessions", "Latest capture"].map((h) => el("th", { scope: "col", text: h })));
  const body = state.scan.rows.map((row) => el("tr", {}, row.map((cell) => el("td", { text: String(cell) }))));
  box.appendChild(el("table", { class: "target-table" }, [el("thead", {}, [head]), el("tbody", {}, body)]));
}

function renderResults(show) {
  const gallery = $("gallery");
  const qa = $("qa");
  gallery.replaceChildren();
  qa.replaceChildren();
  if (!show) return;
  const job = state.job;
  for (const result of job.results) {
    for (const image of result.images || []) {
      const url = `/results/${job.id}/${image.k}`;
      const img = el("img", { src: url, alt: image.label, loading: "lazy" });
      img.addEventListener("click", () => openLightbox(url, image.label));
      gallery.appendChild(el("figure", {}, [
        img,
        el("figcaption", {}, [
          el("span", { text: `${result.target} — ${image.label}` }),
          el("a", { href: url, download: image.filename, text: "Download" }),
        ]),
      ]));
    }
  }
  let first = true;
  for (const result of job.results) {
    if (!first) qa.appendChild(el("hr"));
    first = false;
    if (result.error) {
      qa.appendChild(el("h4", { text: `${result.target} — Processing problem` }));
      qa.appendChild(el("p", { text: result.error.friendly }));
      qa.appendChild(el("details", {}, [
        el("summary", { text: "Technical details" }),
        el("pre", { text: result.error.technical || "" }),
      ]));
      continue;
    }
    qa.appendChild(el("h4", { text: result.target }));
    if (result.replay) qa.appendChild(replayBlock(result.replay));
    for (const w of result.warnings || []) qa.appendChild(el("p", { class: "warn", text: `⚠️ ${w}` }));
    if (result.qa) qa.appendChild(qaBlock(result.qa));
  }
  if (job.state === "failed") {
    if (!first) qa.appendChild(el("hr"));
    qa.appendChild(el("h4", { text: "The render job stopped unexpectedly" }));
    qa.appendChild(el("p", { text: job.summary || "Some completed images may still be available above." }));
    qa.appendChild(el("details", {}, [
      el("summary", { text: "Technical details" }),
      el("pre", { text: job.technical || "" }),
    ]));
  }
}

function qaBlock(view) {
  const wrap = el("div");
  wrap.appendChild(el("h4", { text: view.heading }));
  wrap.appendChild(el("p", { text: view.explanation }));
  for (const w of view.warnings || []) wrap.appendChild(el("p", { class: "warn", text: `⚠️ ${w}` }));
  const items = (view.details || []).map((d) => el("li", { text: d }));
  if (view.technical) items.push(el("li", { text: view.technical }));
  if (items.length) {
    wrap.appendChild(el("details", {}, [el("summary", { text: "Technical details" }), el("ul", {}, items)]));
  }
  return wrap;
}

function replayBlock(replay) {
  const rows = replay.changes.map((c) => el("tr", {}, [
    el("td", { text: c.adjustment }), el("td", { text: String(c.prior) }),
    el("td", { text: String(c.now) }), el("td", { text: c.source }),
    el("td", { text: c.changed ? "yes" : "no" }),
  ]));
  const head = el("tr", {}, ["adjustment", "prior", "now", "source", "changed"].map((h) => el("th", { text: h })));
  const children = [
    el("summary", { text: "Adjustment details" }),
    el("p", { text: "What changed in this re-creation" }),
    el("table", {}, [el("thead", {}, [head]), el("tbody", {}, rows)]),
  ];
  for (const w of replay.warnings || []) children.push(el("p", { class: "warn", text: `⚠️ ${w}` }));
  return el("details", {}, children);
}

function openLightbox(url, alt) {
  const box = $("lightbox");
  const img = box.querySelector("img");
  img.src = url;
  img.alt = alt;
  box.hidden = false;
}

// ---- polling ----------------------------------------------------------------

function ensurePolling() {
  const shouldPoll = isBusy(state);
  if (shouldPoll && pollTimer === null && !pollInFlight) {
    pollTimer = setTimeout(() => {
      pollTimer = null;
      void poll();
    }, 1000);
  }
  if (!shouldPoll && pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

async function poll() {
  if (!state.job || pollInFlight) return;
  pollInFlight = true;
  const wasRunning = isBusy(state);
  try {
    const r = await api(`/api/jobs/${state.job.id}`);
    if (r.ok) {
      dispatch({ type: "poll-ok", job: r.data });
    } else if (r.status === 404) {
      // The server is ALIVE but this job was replaced (a second page started a
      // newer one). Adopt the server's current job — never show the degraded
      // "can't reach" message when the engine is reachable.
      const s = await api("/api/state");
      if (s.ok) dispatch({ type: "job-replaced", job: s.data.job });
      else dispatch({ type: "poll-failed" });
    } else {
      dispatch({ type: "poll-failed" });
    }
  } catch {
    dispatch({ type: "poll-failed" });
  } finally {
    pollInFlight = false;
    ensurePolling();
  }
  if (wasRunning && !isBusy(state)) refreshFineTune(); // just finished
}

// ---- actions ----------------------------------------------------------------

async function refreshFineTune() {
  const { which, output } = state.selection;
  if (!which || which === ALL || !output.trim()) {
    dispatch({ type: "fine-tune", available: false });
    return;
  }
  const r = await api("/api/fine-tune", { method: "POST", body: { which, output_root: output } });
  dispatch({ type: "fine-tune", available: r.ok && r.data.available });
}

function fieldChanged(field, value) {
  dispatch({ type: "field-changed", field, value });
  if (field !== "which") {
    void api("/api/prefs", { method: "PUT", body: { [field]: value } }).then((r) => {
      if (!r.ok) dispatch({ type: "notice", message: r.data.message || DEGRADED_MESSAGE });
    });
  }
  refreshFineTune();
}

async function scan() {
  const r = await api("/api/scan", {
    method: "POST", body: { library_dir: $("library").value }, timeoutMs: SCAN_TIMEOUT_MS,
  });
  if (r.ok) dispatch({ type: "scanned", view: r.data });
  else dispatch({ type: "notice", message: r.data.message || DEGRADED_MESSAGE });
  refreshFineTune();
}

async function startRender(useRecipe) {
  const body = {
    library_dir: $("library").value,
    which: state.selection.which,
    profile: state.selection.profile,
    output_root: $("output").value,
    use_recipe: useRecipe,
  };
  const r = await api("/api/render", { method: "POST", body });
  if (r.ok) dispatch({ type: "render-started", job: r.data.job });
  else {
    if (r.status === 409 && r.data.job) dispatch({ type: "render-started", job: r.data.job });
    dispatch({ type: "notice", message: r.data.message || "Something went wrong." });
  }
}

async function pickFolder(inputId) {
  const r = await api("/api/pick-folder", {
    method: "POST", body: { current: $(inputId).value }, timeoutMs: NO_DEADLINE,
  });
  if (r.ok && r.data.path) {
    $(inputId).value = r.data.path;
    fieldChanged(inputId, r.data.path);
  } else if (!r.ok) {
    dispatch({ type: "notice", message: r.data.message || "" });
  }
}

async function openRecipe() {
  const r = await api("/api/recipe/open", { method: "POST", body: { which: state.selection.which, output_root: $("output").value } });
  dispatch({ type: "notice", message: r.data.message || "" });
}

async function requestStop() {
  const r = await api(`/api/jobs/${state.job.id}/stop`, { method: "POST" });
  dispatch({ type: "notice", message: r.data.message || "" });
}

async function quit() {
  const r = await api("/api/quit", { method: "POST" });
  if (r.status === 409 && r.data.job) dispatch({ type: "render-started", job: r.data.job });
  dispatch({ type: "notice", message: r.data.message || "" });
}

// ---- boot -------------------------------------------------------------------

async function boot() {
  $("library").addEventListener("change", (e) => fieldChanged("library", e.target.value));
  $("output").addEventListener("change", (e) => fieldChanged("output", e.target.value));
  $("profile").addEventListener("change", (e) => fieldChanged("profile", e.target.value));
  $("which").addEventListener("change", (e) => fieldChanged("which", e.target.value));
  $("scan").addEventListener("click", scan);
  $("library-browse").addEventListener("click", () => pickFolder("library"));
  $("output-browse").addEventListener("click", () => pickFolder("output"));
  $("render").addEventListener("click", () => startRender(false));
  $("rerender").addEventListener("click", () => startRender(true));
  $("open-recipe").addEventListener("click", openRecipe);
  $("stop").addEventListener("click", requestStop);
  $("quit").addEventListener("click", quit);
  $("lightbox").addEventListener("click", () => { $("lightbox").hidden = true; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("lightbox").hidden = true; });

  const r = await api("/api/state");
  if (r.ok) {
    dispatch({ type: "init", mode: r.data.mode, prefs: r.data.prefs, job: r.data.job });
    refreshFineTune();
  } else {
    dispatch({ type: "poll-failed" });
    dispatch({ type: "poll-failed" });
    dispatch({ type: "poll-failed" });  // straight to the degraded message
  }
}

boot();
