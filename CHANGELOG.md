# Changelog

Notable user-visible changes are recorded here. This project follows
[Semantic Versioning](https://semver.org/) after v1.0.

## [Unreleased]

## [1.1.1] - 2026-10-09

### Changed

- The ZWO Seestar S50 is now listed as supported. Its stacked color FITS files
  were already processed correctly; the documentation now says so.
- The documentation explains each processing step in plain language: what
  changes in your photo, which application does it, what Photon Finish decides
  for you, and what to do if you don't like the result.
- The app window is dark again, always. v1.1.0 followed the desktop's light/dark
  setting, so a light desktop got a light app — the Gradio GUI it replaced had
  forced dark for every user. Dark is now fixed: Photon Finish is used at night,
  and a bright panel next to the photo biases the eye judging its background and
  stars. Native controls and scrollbars follow the dark palette now too, which
  they never did.

### Fixed

- The Browse buttons in the Windows app window did nothing at all — no dialog, no
  error, no message. Both buttons, every time. pywebview builds its Windows folder
  dialog on whichever thread calls it, and the app serves that request from a
  worker thread, where the dialog hangs forever and blocks the second button too.
  Windows now opens the folder chooser the same way the browser mode always has,
  in a separate process, which is safe from any thread. macOS is unaffected and
  keeps its native dialog. Typing a path by hand was the only workaround.

## [1.1.0] - 2026-08-21

### Changed

- The GUI is now a native app window (pywebview) over a local job server (FastAPI),
  replacing Gradio. A render now survives page reloads and browser closes; a second
  page reattaches to the live render. The app refuses to quit while a photo is being
  created (no cancellation exists — honesty over pretense), and multi-photo batches
  offer "Finish this photo, then stop."
- The Windows build script now uses the replacement GUI stack. A packaged Windows
  application is still not released: it builds, but has not been verified hands-on.
- One app instance at a time, enforced by an OS-held lock instead of a fixed port;
  the server now uses an ephemeral localhost port with token+cookie auth
  (SEESTAR_GUI_PORT still honored).
- When the native app window cannot open, Photon Finish now reports the reason and
  stops instead of quietly switching to a browser tab. On Windows the cause is
  almost always a missing Microsoft Edge WebView2 Runtime, and the message says so.
  A silent downgrade left the user with an unexplained browser tab — the packaged
  build has no console, so the old explanation was never visible — and it committed
  the project to two different Windows execution paths. `--browser` still works for
  anyone who deliberately wants a browser tab.

### Removed

- Gradio dependency subtree (gradio, gradio-client, pandas, orjson, …) from the
  packaged app; the preview-cache copy layer (results now serve directly, indexed
  and authenticated).
- The automatic native-window → browser fallback, and the launch-token re-minting
  that existed only to serve it.

### Fixed
- **Choosing a folder now works however long you take.** The Browse dialog waits for
  you, but the app gave up listening after ten seconds — so any pick that took longer
  (every real navigation into a nested cloud-storage folder) silently kept the old
  folder, looking as though the app had ignored the choice. Scanning had the same
  ten-second limit and would have hit it on a large or cloud-hosted library.
- A folder chooser that fails to open now says so, and points to the box you can type
  or paste a path into, instead of leaving the previous folder on screen with no
  explanation.
- Reloading the browser page during a render no longer silently resets the
  folder and style choices to a previous session's values — the fields now
  restore the current saved choices on every page load.
- Clicking "Create finished photo" while a render is already running (for
  example after a page reload hid its progress) is now refused with a clear
  "still working in the background" message instead of starting a second,
  conflicting render in the same save folder.
- The macOS app now writes a log file (`~/Library/Logs/Photon Finish/app.log`)
  instead of silently discarding errors when launched by double-click.
- A render that fails part-way now deletes its own working files instead of leaving
  a `_work_…` folder of full-size FITS behind. Retrying after a failure used to cost
  hundreds of megabytes per attempt. `--keep-scratch` and debug mode still keep them,
  on the failure path as on the success path.
- Finished photos are now named from the bundled 219-object catalog, matched by where
  the telescope was pointed. Previously only six objects had a common name — the ones
  this project happened to be built against — so M8 exported as "M8 — Lagoon Nebula"
  while M42 exported as just "M42". Titles and keywords are affected; nothing about how
  the photo is processed. Three names change: M17 is "Checkmark Nebula" (its catalog
  name; Omega and Swan are listed aliases), M13 is "Hercules Globular Cluster", and M5
  no longer repeats itself as "Messier 5".

### Changed
- **Publication honesty:** a `processing_failed` render now publishes `<T>_uncertified.*`
  (never `_final`); prior certified outputs — rasters, sidecars, the QA starless, and the
  recipe — are never touched by failed reruns; `seestar qa` and `seestar validate` report
  uncertified state explicitly (nonzero exit; hard fail instead of silent skip). Known
  follow-ups recorded in the spec: GUI copy for uncertified outcomes lands after the GUI
  replacement merges (the current GUI's Fine-tune panel keys on `<T>_recipe.json`, which a
  failed rerun no longer refreshes).

## [1.0.0] - 2026-08-02

First public release of **Photon Finish** — finished, share-worthy photos from
ZWO Seestar S30 and S30 Pro stacked FITS files.

### Supported input and platforms

- Input: stacked color FITS masters written by a ZWO Seestar S30 or S30 Pro.
  Files are recognized by their content, not their filename.
- macOS: a packaged, self-contained application (signed, notarized DMG) — no
  Python installation required.
- Windows 11: run from source for now; a packaged application is coming soon.
- Both platforms use four separately installed helper applications: GraXpert,
  Siril, StarNet2, and ExifTool (see the installation guide).

### Privacy and local behavior

- All photo processing happens on your computer; photos are never uploaded.
  The only network use is an optional online object lookup (SIMBAD) when the
  bundled offline catalog has no match, and the helper applications' own
  one-time model downloads.

### Capabilities

- A local interface for finding Seestar stacks, choosing an output style,
  creating finished photos, viewing variants, and reading quality results.
- Measured gradient removal, denoise, color, stretch, content treatment,
  star treatment, satellite-trail handling, exports, metadata, and quality
  checks — decisions driven by what is measured in each photo, never by the
  target's name.
- Showcase, Natural, Reference, and Print output styles.
- Optional per-image adjustment recipes and deterministic replay.
- A bundled, deduplicated Messier and Caldwell target catalog derived from
  pinned OpenNGC data, used offline.
- Live per-stage progress that reflects what the app is actually doing, with
  generous safety time limits on the helper applications. A photo whose
  quality cannot be verified is reported as a failure rather than passing
  silently, and batch runs isolate per-photo failures while keeping earlier
  successes.
- The v1 user manual documents capabilities and limits; contributor
  documentation covers the architecture.

### Known notes

- The public project name is **Photon Finish**: the Python distribution is
  `photon-finish` and the macOS application is `Photon Finish.app`
  (bundle id `com.workingbackwards.photon-finish`). For compatibility, the
  Python import package and the command-line interface remain `seestar`.
- If you installed an earlier beta build (`Seestar S30.app`), it is not
  replaced automatically because the application identity changed — delete it
  after installing Photon Finish.
