# Privacy and network use

## Image processing is local

Your FITS, TIFF, PNG, and JPG image data is processed on your computer. The app
does not provide accounts, cloud storage, telemetry, or remote rendering.

The app window (or, in `--browser` mode, a browser tab) connects to a small job
server on your own computer, at `127.0.0.1` on an ephemeral local port — a fresh
port each launch, not a fixed one. That server only accepts requests carrying a
one-time launch token exchanged for a session cookie at startup; every request
after that must present the cookie, and only your own copy of the app ever holds
it. Seeing the interface in a browser tab does not mean it is a public website —
nothing outside your computer can reach this server.

## Optional catalog lookup

For a plate-solved frame, the app can send a small sky-coordinate query—right
ascension, declination, and search radius—to the SIMBAD astronomy catalog. This
can help distinguish an uncertain galaxy from a dense star cluster.

It does **not** send:

- the FITS or any rendered image;
- pixels, thumbnails, or image metadata files;
- your source or output folder names; or
- an account identifier.

The network lookup can be disabled from the CLI with `--no-prior`. The bundled
Messier and Caldwell catalog remains available offline and is not disabled by
that flag.

## Local files the app creates

The processor writes generated images, notes, recipes, and quality records to
the output folder you choose. The GUI also stores its last-selected folder and
style in:

```text
~/.seestar_gui_state.json
```

That small local settings file contains paths and a style choice, not image
data. Delete it to forget the GUI's remembered choices.

The app also holds a small single-instance lock file at `~/.seestar_gui.lock`
while running (just its process ID and the port it bound, so a second launch can
detect it and tell you it's already open); it is released automatically when the
app quits.

## External applications

GraXpert, Siril, StarNet2, and ExifTool run as separately installed programs.
Consult each project's own documentation for its network and privacy behavior.
This project's processing workflow invokes their local command-line programs.
