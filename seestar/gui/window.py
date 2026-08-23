"""pywebview desktop shell (spec §3): the native window on the MAIN thread, the
closing-veto quit protocol (spec §4), and the macOS Edit menu clipboard fix
(gate finding 2026-08-12). Import webview lazily so browser-only installs never
touch it."""

from __future__ import annotations

import sys
import traceback

from . import views


def run_window(url: str, *, registry, window_ref: dict) -> None:
    """Blocks in webview.start() until the window closes. Must run on the main
    thread (Cocoa). Window-X and Cmd-Q BOTH route through `closing` (gate fact);
    a running job vetoes the close — no cancellation interface exists, so the
    app never pretends it can cancel (spec §4, R7)."""
    import webview

    window = webview.create_window(
        "Photon Finish",
        url,
        width=1280,
        height=880,
        # Gate finding: text_select defaults False, leaving QA text and picked
        # paths uncopyable. The Edit menu below supplies the Cmd-C/V/X/A routes.
        text_select=True,
    )
    window_ref["window"] = window

    def on_closing():
        accepted, _active = registry.begin_shutdown()
        if not accepted:
            try:
                window.create_confirmation_dialog("Photon Finish is still working", views.MSG_QUIT_BUSY)
            except Exception:
                print("quit-veto dialog failed:", traceback.format_exc())
            return False  # the veto IS the protocol; the dialog is a courtesy
        return True

    window.events.closing += on_closing
    webview.start(private_mode=True, func=_install_macos_edit_menu)


def _install_macos_edit_menu() -> None:
    """Cmd-C/V/X/A need an Edit menu with the standard Cocoa selectors; pywebview
    does not create one (gate finding). Runs as webview.start(func=...) on a
    worker thread, so the AppKit mutation is dispatched to the main queue.
    Windows (WebView2) has native clipboard shortcuts — nothing to do."""
    if sys.platform != "darwin":
        return
    try:
        import AppKit

        def install():
            app = AppKit.NSApplication.sharedApplication()
            main_menu = app.mainMenu()
            if main_menu is None:
                return
            edit_menu = AppKit.NSMenu.alloc().initWithTitle_("Edit")
            for title, selector, key in (
                ("Undo", "undo:", "z"),
                ("Redo", "redo:", "Z"),
                ("Cut", "cut:", "x"),
                ("Copy", "copy:", "c"),
                ("Paste", "paste:", "v"),
                ("Select All", "selectAll:", "a"),
            ):
                edit_menu.addItem_(AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, selector, key))
            holder = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
            holder.setSubmenu_(edit_menu)
            main_menu.addItem_(holder)

        AppKit.NSOperationQueue.mainQueue().addOperationWithBlock_(install)
    except Exception:
        # The Task 9 battery verifies clipboard; a failed install must not kill the app.
        print("edit menu install failed:", traceback.format_exc())
