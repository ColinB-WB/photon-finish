"""Single-instance lock + socket startup — REAL subprocesses, real OS locks (spec §11).

Required CI exercises fcntl on macOS/Linux and the actual msvcrt branch on Windows;
socket behavior runs on every platform."""

import os
import socket
import subprocess
import sys
import textwrap

import pytest

from seestar.gui.instance import AlreadyRunning, SingleInstanceLock, bind_server_socket

HOLDER = textwrap.dedent("""
    import sys, time
    from seestar.gui.instance import SingleInstanceLock
    lock = SingleInstanceLock(sys.argv[1])
    lock.acquire()
    lock.write_metadata(12345, 54321)
    print("HELD", flush=True)
    time.sleep(120)
""")


def _spawn_holder(lock_path):
    child = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock_path)],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    assert child.stdout.readline().strip() == "HELD"
    return child


def test_second_acquisition_refused_while_holder_alive(tmp_path):
    lock_path = tmp_path / "gui.lock"
    child = _spawn_holder(lock_path)
    try:
        lock = SingleInstanceLock(str(lock_path))
        with pytest.raises(AlreadyRunning) as e:
            lock.acquire()
        assert "already open" in str(e.value)
    finally:
        child.kill()
        child.wait()


def test_lock_released_on_hard_death(tmp_path):
    lock_path = tmp_path / "gui.lock"
    child = _spawn_holder(lock_path)
    child.kill()
    child.wait()
    lock = SingleInstanceLock(str(lock_path))
    lock.acquire()  # must NOT raise: the OS released the dead holder's lock
    lock.release()


def test_lock_released_on_normal_release(tmp_path):
    lock_path = tmp_path / "gui.lock"
    first = SingleInstanceLock(str(lock_path))
    first.acquire()
    first.write_metadata(os.getpid(), 4242)
    with open(lock_path) as f:
        assert '"port": 4242' in f.read()  # diagnostics only, but must be written
    second = SingleInstanceLock(str(lock_path))
    with pytest.raises(AlreadyRunning):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def test_ephemeral_socket_binds_loopback_and_publishes_port():
    sock = bind_server_socket(None)
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1" and port > 0
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass  # it is really listening
    finally:
        sock.close()


def test_fixed_port_honored_and_busy_fixed_port_raises():
    first = bind_server_socket(None)
    busy_port = first.getsockname()[1]
    try:
        with pytest.raises(OSError):
            bind_server_socket(busy_port)
    finally:
        first.close()
    # after release (+SO_REUSEADDR) the same fixed port binds again -- the
    # quit-then-immediately-relaunch case the old TIME_WAIT probe got wrong
    again = bind_server_socket(busy_port)
    try:
        assert again.getsockname()[1] == busy_port
    finally:
        again.close()
