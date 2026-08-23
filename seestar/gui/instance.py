"""Process-lifetime single-instance lock + bound-socket startup (spec §8).

The OS advisory lock IS the protocol: held for the process lifetime, released
by the OS on ANY process death — so there is no stale-lock heuristic, no PID
probing, and no check-delete-create race. The pid/port written into the file
are diagnostic metadata only, never read back for liveness decisions. This
replaces the fixed port's ACCIDENTAL instance guard (gui.py B-07) with an
explicit one that also closes the cross-instance two-renders race.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time

from . import views

LOCK_PATH = os.path.expanduser("~/.seestar_gui.lock")

# Byte offset of the Windows lock sentinel. msvcrt locks are MANDATORY (unlike
# POSIX advisory flock), so whichever byte we lock becomes unreadable to everyone
# else -- locking byte 0 made the diagnostic metadata unreadable exactly while the
# app was running, which is when you would want to read it. Lock a sentinel far
# past the JSON instead: Win32 LockFile explicitly permits locking a region beyond
# end-of-file, the metadata stays readable, and exclusion is unaffected (measured:
# a second acquirer is still refused).
_WINDOWS_LOCK_SENTINEL = 1 << 30

# TerminateProcess returns -- and the process handle becomes signalled, so wait()
# returns -- BEFORE Windows finishes tearing down the dead process's open handles
# and releasing its file locks. Measured lag on Windows 11: ~100 ms. POSIX has no
# equivalent window (flock is released as part of reaping), so this budget is
# Windows-only. Without it, relaunching straight after a crash is refused with
# "already open" and the user has no way to understand why.
_WINDOWS_LOCK_RELEASE_GRACE_S = 1.0
_WINDOWS_LOCK_POLL_S = 0.05


class AlreadyRunning(RuntimeError):
    """Another live Photon Finish process holds the lock."""


class SingleInstanceLock:
    def __init__(self, path: str = LOCK_PATH):
        self.path = path
        self._fh = None

    def acquire(self) -> None:
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            if sys.platform.startswith("win"):
                self._acquire_windows(fh)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise AlreadyRunning(views.MSG_ALREADY_RUNNING) from None
        self._fh = fh

    @staticmethod
    def _acquire_windows(fh) -> None:
        """Take the sentinel-byte lock, retrying briefly so a dead holder's
        not-yet-released lock is not mistaken for a live instance. Raises OSError
        if it stays held; the caller turns that into AlreadyRunning."""
        import msvcrt

        # msvcrt.locking locks a range starting at the CURRENT position.
        deadline = time.monotonic() + _WINDOWS_LOCK_RELEASE_GRACE_S
        while True:
            try:
                fh.seek(_WINDOWS_LOCK_SENTINEL)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_WINDOWS_LOCK_POLL_S)

    def write_metadata(self, pid: int, port: int) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(0)
            self._fh.truncate()
            self._fh.write(json.dumps({"pid": pid, "port": port}))
            self._fh.flush()
        except OSError:
            pass  # incidental: the held lock, not the metadata, is the protocol

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()  # closing the fd releases both lock flavors
            except OSError:
                pass
            self._fh = None


def bind_server_socket(port: int | None) -> socket.socket:
    """Pre-bind the loopback socket BEFORE uvicorn starts (skeleton-proven order):
    the launch URL and lock metadata always describe a socket that already
    exists. `None` -> ephemeral (port 0). A LIVE listener must fail the bind
    loudly, while a fixed SEESTAR_GUI_PORT relaunch must succeed straight after
    the previous instance quits.

    SO_REUSEADDR delivers exactly that on POSIX and the OPPOSITE on Windows,
    where it lets a second socket bind a port another socket is actively
    listening on -- silently stealing it. Measured on Windows 11: with the
    option, a second bind to a live listener's port succeeded; without it, the
    bind was refused (WSAEADDRINUSE) and rebinding after close still worked. So
    Windows needs no option at all -- its default already is the contract -- and
    setting it would defeat the fixed-port instance guard."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if not sys.platform.startswith("win"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port or 0))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock
