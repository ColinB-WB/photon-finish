# Use the app

The interface has three choices and one main action.

## 1. Choose your Seestar photos

Select either:

- a single target folder that contains one or more qualifying stacked FITS
  sessions; or
- the copied Seestar library folder whose immediate subfolders contain targets.

Choose **Find photos**. The app opens candidate FITS files and checks their
headers. A filename does not have to begin with `Stacked_`.

The table shows the target name, filter, number of sessions, and latest capture.
If a target has multiple sessions, v1 uses the latest session. It does not
combine sessions.

Files that are not qualifying Seestar stacked color masters are ignored with a
reported reason. This includes raw subframes, unrelated FITS files, and the
Seestar JPG previews.

## 2. Choose what to create

Choose one target or, when offered, the entire discovered batch. Then choose an
[output style](styles-and-versions.md).

Starting with one target is useful: it confirms the helper tools, shows the
expected processing time, and enables the optional adjustment controls after
the first successful render.

## 3. Choose where to save

Choose a folder separate from the copied Seestar library. Each target receives
its own subfolder. Reusing an existing output root is fine, but current results
may replace files with the same target and style names.

## Create finished photo

Choose **Create finished photo**. Controls are temporarily disabled while the
render runs. The status line shows:

- the current target in a batch;
- the current processing step;
- the helper tool in use; and
- elapsed time.

The gallery shows only images reported by the current creation, so older style
files in the same folder are not mistaken for new results.

## Close the application

Close the app window, or choose Quit, like any other application. While a photo
is being created there is no cancellation, so quitting is refused until it
finishes — the window (or, from a terminal with `--browser`, <kbd>Control</kbd>+<kbd>C</kbd>)
stays open until the current photo completes. For a multi-photo batch, the
"Finish this photo, then stop" button offered during a render stops cleanly at
the next photo instead of waiting for the whole batch.

Only one Photon Finish can run at a time. If you try to open it again while it is
already running, it tells you so instead of starting a second, conflicting copy —
switch to the window that is already open.

## Command-line alternative

The same engine is available from the command line:

```console
seestar discover <library-folder>
seestar process <library-folder> --out <save-folder>
seestar run <target-folder> --out <target-output-folder>
```

Run `seestar --help` or a command followed by `--help` for all options. The GUI
is the recommended route for users who do not need scripting or diagnostics.
