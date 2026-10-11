# Troubleshooting

## Start with the dependency check

When running from source, open a terminal or PowerShell in the project
environment and run:

```console
seestar doctor
```

On Windows, `py -m seestar.cli doctor` also works. The report lists the resolved
path for every required helper application.

## The app cannot find a helper tool

Install the missing application and restart the processor. For StarNet2, confirm
that its command-line program is present:

```console
starnet2 --help
```

Tool paths resolve in this order: environment variable, `config.yaml`, system
`PATH`, then known platform installation locations. Advanced source users can
set `tools.<name>` in `config.yaml` or a `SEESTAR_<NAME>` environment variable.
Run `seestar doctor` again after changing a path.

## No photos were found

Check that:

- you selected the copied Seestar target or library folder;
- the folder contains a `.fit` or `.fits` stacked color master;
- the file is from a Seestar S30, S30 Pro, or S50; and
- the source is a stack, not an individual raw subframe.

Filenames do not need to start with `Stacked_`. If FITS files were skipped, the
interface reports an example reason under technical details.

## A whole-library batch has some failures

Processing a whole Seestar library does not stop at the first photo that fails.
Each target is attempted independently: if one photo's processing fails, later
targets in the same batch are still attempted, and every output already produced
for an earlier, successful target is kept untouched.

On the command line, `seestar process` names each failed target with its error
and states that the successful outputs were kept, then prints a summary line
with how many targets succeeded and how many failed. Because a partial failure
still means part of the batch did not produce a finished photo, a command-line
batch with any failed target exits with a non-zero status, so scripts and
automation can detect a partial failure even though some photos rendered
successfully. The GUI's existing behavior — it already continues past one bad
photo in a whole-library batch and reports each result — is unchanged.

## The app window did not open, or you get an "already open" message

Photon Finish opens in its own native app window, not a browser tab. If the window
does not appear, check whether it is already running — the app only allows one
instance at a time, and a second launch shows a message that it is already open;
switch to the existing window (or quit it) instead of launching a second copy.

If you started the interface from source with `--browser` (or set
`SEESTAR_GUI_PORT`), it serves a normal browser tab instead of a window; the
terminal prints the exact local address to open, on an ephemeral port unless
`SEESTAR_GUI_PORT` picked a fixed one. The interface is local either way — a
browser page or app window does not mean the photographs are being uploaded.

## Rendering appears stuck

Watch the elapsed time and latest stage. GraXpert and StarNet2 may legitimately
take several minutes and can be slower on the first run (GraXpert downloads and
caches its AI models the first time it runs). Photon Finish keeps the elapsed
time moving throughout, including during a long helper-tool step, so a
live-updating clock does not mean anything is wrong.

Each helper tool also has a hard time limit. If a tool genuinely hangs, Photon
Finish stops that photo on its own rather than waiting forever, and shows a
named, actionable message — which tool timed out, roughly how long it waited,
and what to try next (for GraXpert specifically: open it once by hand to
finish first-run setup, then try again). No source photo is ever changed by a
timeout. This is not a user-tunable setting; if a specific tool consistently
needs longer than its limit, report it (see "Getting more help" below) rather
than editing internal configuration files.

For repeated failures, run one target rather than a whole-library batch and
copy the technical error shown in the result area.

**New behavior:** while a photo is being created, Photon Finish refuses to quit or
be stopped — there is no cancellation, and pretending to cancel a render that is
still mutating files would be worse than refusing (closing the window, quitting
from the menu, and Ctrl-C in `--browser`/source mode are all refused the same
way). Wait for the current photo to finish, or for a multi-photo batch use "Finish
this photo, then stop" to stop at the next photo boundary. If a render is
genuinely and permanently stuck (not just slow — see above), the only way out is
an OS-level kill of the process; a source checkout's Ctrl-C no longer stops the
render the way it did in v1.0.

## The save folder is rejected

Choose a writable folder that is separate from the copied Seestar library.
Confirm that the drive has free space. The app intentionally refuses to treat
the source library as a processing workspace.

## The result is capture-limited

Read the reason in the result check. Fine-tuning cannot restore signal that is
below the noise floor or correct severe focus, tracking, clipping, cloud, or dew
problems. A longer or cleaner capture is often the effective fix.

## The stars or color are not to my taste

Try the Natural style for a quieter presentation. For extended subjects, compare
the Full star field and Reduced stars versions. If the overall style is right
but one detail bothers you, use the
[optional adjustment file](fine-tuning.md).

## A recipe edit causes an error

JSON is strict about commas, braces, and quoted text. Undo the last edit or set
the changed `your_value` back to `null`. The normal **Create finished photo**
action ignores the saved recipe and produces a clean automatic render.

## Windows does not recognize `py`

Use `python` instead:

```powershell
python -m pip install -e ".[gui]"
python -m seestar.gui
```

If neither command exists, reinstall Python from python.org and enable **Add
Python to PATH**.

## Getting more help

See `SUPPORT.md` at the repository root for the information to include in a report.
Never attach private image files unless you deliberately choose to share them.
