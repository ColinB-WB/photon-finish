"""H1 foundation (Task 3): validated tool-timeout configuration + a bounded
external-process runner (`.superpowers/sdd/task-3-brief.md`).

Step 1 tests cover `ToolTimeouts`/`ToolPaths` defaults and `resolve_tools`/
`doctor`'s config parsing. Step 2 tests spawn REAL throwaway Python processes
(never a mocked `subprocess.run`) to exercise `run_external`'s normal-path
behavior and its timeout/process-tree-cleanup contract. Windows tree-walk
logic is unit-tested against a fake `Popen` (cannot be exercised on this
Mac); the POSIX path gets a real boundary test since that's the current
platform (brief: "the current platform must also have one real boundary
test").
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from seestar import tools as T
from seestar.adapters import _external_process as EP
from seestar.adapters import base as B
from seestar.adapters._external_process import ExternalToolTimeout, run_external

_TOOLS = ("siril", "graxpert", "starnet2", "exiftool")

# The real-process kill tests below exercise POSIX-specific semantics (new
# session + process-group signalling, SIG_IGN on SIGTERM), and their liveness
# probe (`os.kill(pid, 0)`) is a POSIX idiom — on Windows `os.kill` is
# implemented via TerminateProcess and raises WinError 87 for a dead pid.
# The Windows tree-kill path (bounded `taskkill /T /F` + direct-child
# fallback) is covered by the fake-Popen unit tests (test_windows_tree_kill_*),
# per the plan's platform split. This is an explicit platform-capability skip,
# not a silent one: the POSIX path runs for real on macOS CI + the Linux canary.
_posix_kill_semantics = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process-group kill semantics; Windows taskkill path is unit-tested via fake Popen",
)


# ---------------------------------------------------------------------------
# Step 1: configuration
# ---------------------------------------------------------------------------


def _stub_resolve_one(monkeypatch):
    """Mock ONLY tool path discovery -- resolver tests must not require
    installed astronomy tools."""

    def fake(tool: str, cfg: dict):
        return (f"/fake/bin/{tool}", "test-stub", [])

    monkeypatch.setattr(T, "_resolve_one", fake)


def test_toolpaths_constructed_with_only_four_paths_gets_settled_defaults():
    paths = B.ToolPaths(siril="s", graxpert="g", starnet2="n", exiftool="e")
    assert paths.timeouts.siril == 1800.0
    assert paths.timeouts.graxpert == 3600.0
    assert paths.timeouts.starnet2 == 3600.0
    assert paths.timeouts.exiftool == 120.0


def test_resolve_tools_carries_valid_timeout_overrides_into_toolpaths(monkeypatch):
    _stub_resolve_one(monkeypatch)
    cfg = {"tools": {"timeout_s": {"siril": 900, "graxpert": 1200, "starnet2": 1500, "exiftool": 30}}}
    paths = T.resolve_tools(cfg)
    assert paths.timeouts.siril == 900
    assert paths.timeouts.graxpert == 1200
    assert paths.timeouts.starnet2 == 1500
    assert paths.timeouts.exiftool == 30


def test_resolve_tools_missing_timeout_key_uses_its_default(monkeypatch):
    _stub_resolve_one(monkeypatch)
    cfg = {"tools": {"timeout_s": {"siril": 900}}}  # graxpert/starnet2/exiftool absent
    paths = T.resolve_tools(cfg)
    assert paths.timeouts.siril == 900
    assert paths.timeouts.graxpert == 3600.0
    assert paths.timeouts.starnet2 == 3600.0
    assert paths.timeouts.exiftool == 120.0


def test_resolve_tools_with_no_timeout_block_uses_all_defaults(monkeypatch):
    _stub_resolve_one(monkeypatch)
    paths = T.resolve_tools({"tools": {"siril": "", "graxpert": "", "starnet2": "", "exiftool": ""}})
    assert paths.timeouts.siril == 1800.0
    assert paths.timeouts.graxpert == 3600.0
    assert paths.timeouts.starnet2 == 3600.0
    assert paths.timeouts.exiftool == 120.0


@pytest.mark.parametrize(
    "bad_value",
    [0, 0.0, -1, -0.5, True, False, None, float("nan"), float("inf"), float("-inf"), "900", [900], {}],
    ids=[
        "zero-int",
        "zero-float",
        "neg-int",
        "neg-float",
        "true",
        "false",
        "none",
        "nan",
        "inf",
        "neg-inf",
        "str",
        "list",
        "dict",
    ],
)
def test_resolve_tools_rejects_every_invalid_timeout_value(monkeypatch, bad_value):
    _stub_resolve_one(monkeypatch)
    cfg = {"tools": {"timeout_s": {"siril": bad_value}}}
    with pytest.raises(ValueError, match=r"tools\.timeout_s\.siril"):
        T.resolve_tools(cfg)


@pytest.mark.parametrize("tool", _TOOLS)
def test_resolve_tools_invalid_value_names_the_specific_tool_key(monkeypatch, tool):
    _stub_resolve_one(monkeypatch)
    cfg = {"tools": {"timeout_s": {tool: -5}}}
    with pytest.raises(ValueError, match=rf"tools\.timeout_s\.{tool}\b"):
        T.resolve_tools(cfg)


def test_tooltimeouts_direct_construction_also_validates():
    with pytest.raises(ValueError, match=r"tools\.timeout_s\.siril"):
        B.ToolTimeouts(siril=0)
    with pytest.raises(ValueError, match=r"tools\.timeout_s\.graxpert"):
        B.ToolTimeouts(graxpert=True)
    with pytest.raises(ValueError, match=r"tools\.timeout_s\.exiftool"):
        B.ToolTimeouts(exiftool=float("nan"))


def test_tooltimeouts_for_tool_is_exhaustive_and_unknown_tool_raises():
    t = B.ToolTimeouts()
    assert t.for_tool("siril") == 1800.0
    assert t.for_tool("graxpert") == 3600.0
    assert t.for_tool("starnet2") == 3600.0
    assert t.for_tool("exiftool") == 120.0
    with pytest.raises(ValueError, match="unknown tool"):
        t.for_tool("gimp")


def test_doctor_reports_the_four_effective_timeout_values(monkeypatch):
    _stub_resolve_one(monkeypatch)
    report = T.doctor({"tools": {"timeout_s": {"siril": 42}}})
    assert "42" in report
    assert "3600" in report  # graxpert + starnet2 defaults
    assert "120" in report  # exiftool default


# ---------------------------------------------------------------------------
# Step 2: the bounded runner -- real throwaway processes only
# ---------------------------------------------------------------------------


def _tools_with_timeout(tool: str, seconds: float) -> B.ToolPaths:
    """A ToolPaths whose *only* deliberately-shortened timeout is `tool`'s --
    the other three keep their settled defaults (also positive/finite, so
    construction stays valid)."""
    field_values = {"siril": 1800.0, "graxpert": 3600.0, "starnet2": 3600.0, "exiftool": 120.0}
    field_values[tool] = seconds
    return B.ToolPaths(siril="s", graxpert="g", starnet2="n", exiftool="e", timeouts=B.ToolTimeouts(**field_values))


def test_run_external_normal_completion_preserves_stdout_stderr_and_returncode():
    tools = _tools_with_timeout("siril", 10.0)
    result = run_external(
        [sys.executable, "-c", "import sys; print('out-line'); print('err-line', file=sys.stderr); sys.exit(3)"],
        tool="siril",
        tools=tools,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 3
    assert result.stdout.strip() == "out-line"
    assert result.stderr.strip() == "err-line"
    assert isinstance(result.stdout, str)


def test_run_external_binary_mode_returns_bytes_when_text_not_requested():
    tools = _tools_with_timeout("siril", 10.0)
    result = run_external([sys.executable, "-c", "print('hello')"], tool="siril", tools=tools, capture_output=True)
    assert isinstance(result.stdout, bytes)
    assert b"hello" in result.stdout


def test_run_external_encoding_without_text_flag_still_decodes():
    # Mirrors siril.py's real call: encoding="utf-8" is passed without text=True,
    # and subprocess.Popen still decodes to str -- must be preserved verbatim.
    tools = _tools_with_timeout("siril", 10.0)
    result = run_external(
        # Emit UTF-8 bytes directly: `print` would encode via the child's console
        # code page on Windows (cp1252 -> mojibake); the runner's decoding contract
        # is what's under test, so the child's bytes must be deterministic.
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write('caf\\u00e9'.encode('utf-8'))"],
        tool="siril",
        tools=tools,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert isinstance(result.stdout, str)
    assert "café" in result.stdout


def test_run_external_check_true_raises_calledprocesserror_with_returncode():
    tools = _tools_with_timeout("siril", 10.0)
    with pytest.raises(subprocess.CalledProcessError) as exc_info:
        run_external(
            [sys.executable, "-c", "import sys; sys.exit(7)"],
            tool="siril",
            tools=tools,
            capture_output=True,
            text=True,
            check=True,
        )
    assert exc_info.value.returncode == 7


def test_run_external_check_false_does_not_raise_on_nonzero_exit():
    tools = _tools_with_timeout("siril", 10.0)
    result = run_external(
        [sys.executable, "-c", "import sys; sys.exit(1)"], tool="siril", tools=tools, capture_output=True
    )
    assert result.returncode == 1


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_until_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


def test_run_external_timeout_raises_promptly():
    tools = _tools_with_timeout("siril", 0.15)
    start = time.monotonic()
    with pytest.raises(ExternalToolTimeout):
        run_external(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            tool="siril",
            tools=tools,
            capture_output=True,
            text=True,
        )
    elapsed = time.monotonic() - start
    # 0.15s deadline + up to 5s grace (default handler makes this child die on
    # the first TERM) + generous scheduling slack -- must not hang for 30s.
    assert elapsed < 8.0


def test_run_external_timeout_message_is_safe_and_informative():
    tools = _tools_with_timeout("graxpert", 0.15)
    secret_path = os.path.join(os.sep, "Users", "definitely-not-a-real-user", "secret-target-name")
    with pytest.raises(ExternalToolTimeout) as exc_info:
        run_external(
            [sys.executable, "-c", "import time; time.sleep(30)", secret_path],
            tool="graxpert",
            tools=tools,
        )
    message = str(exc_info.value)
    assert "GraXpert" in message
    assert "0.15" in message
    assert "timeout" in message.lower()
    assert secret_path not in message
    assert "time.sleep(30)" not in message
    assert sys.executable not in message


@_posix_kill_semantics
def test_run_external_kills_process_that_ignores_sigterm_after_bounded_grace():
    """Exercises the real POSIX new-session/process-group TERM -> bounded grace
    -> KILL path (the current platform's required real boundary test): a child
    that ignores SIGTERM can only die from the follow-up SIGKILL, so the
    runner must wait through the grace period and then force-kill it."""
    tools = _tools_with_timeout("siril", 0.2)
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    start = time.monotonic()
    with pytest.raises(ExternalToolTimeout):
        run_external([sys.executable, "-c", script], tool="siril", tools=tools)
    elapsed = time.monotonic() - start
    # Must have waited most of the ~5s grace period before the SIGKILL landed --
    # too-fast completion would mean the grace/KILL path never actually ran.
    assert elapsed >= 3.0
    assert elapsed < 9.0


@_posix_kill_semantics
def test_run_external_cleans_up_child_and_grandchild_as_a_tree(tmp_path):
    """The motivating defect: a plain `subprocess.run(timeout=...)` kill only
    reaches the direct child, orphaning any grandchild it spawned. Confirm the
    recorded child/grandchild PIDs are both gone after the runner returns."""
    pid_file = tmp_path / "pids.txt"
    script = textwrap.dedent(f"""
        import os, subprocess, sys, time
        grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        with open({str(pid_file)!r}, "w") as f:
            f.write(f"{{os.getpid()}} {{grandchild.pid}}")
        time.sleep(30)
        """)
    tools = _tools_with_timeout("siril", 1.0)
    with pytest.raises(ExternalToolTimeout):
        run_external([sys.executable, "-c", script], tool="siril", tools=tools)

    assert pid_file.exists(), "child never reached the point of recording PIDs"
    child_pid_s, grandchild_pid_s = pid_file.read_text().split()
    child_pid, grandchild_pid = int(child_pid_s), int(grandchild_pid_s)

    assert _wait_until_gone(child_pid), "direct child still running after runner returned"
    assert _wait_until_gone(grandchild_pid), "grandchild still running after runner returned (orphaned)"


@_posix_kill_semantics
def test_run_external_kills_surviving_grandchild_when_direct_child_exits_promptly(tmp_path):
    """Regression: the SIGKILL escalation must be gated on whether the whole
    PROCESS GROUP is still alive, not merely on whether the direct child is
    still alive. A direct child with the default SIGTERM handler dies almost
    immediately when the group is TERM'd, but a grandchild that installs
    SIG_IGN for SIGTERM survives the TERM and can only be reaped by the
    follow-up SIGKILL. If escalation is gated on the direct child's `poll()`
    (the pre-fix behavior), the loop exits as soon as the child dies and the
    SIGKILL is skipped entirely -- the grandchild leaks."""
    pid_file = tmp_path / "pids.txt"
    script = textwrap.dedent(f"""
        import os, signal, subprocess, sys, time
        grandchild = subprocess.Popen([
            sys.executable, "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
        ])
        with open({str(pid_file)!r}, "w") as f:
            f.write(f"{{os.getpid()}} {{grandchild.pid}}")
        # Default SIGTERM handling: this process (the direct child) dies almost
        # immediately once the group is TERM'd, well before the grandchild does.
        time.sleep(30)
        """)
    # 1.0s inner timeout to match the sibling tree-cleanup test's CI-load margin
    # (test_run_external_cleans_up_child_and_grandchild_as_a_tree) -- the prior 0.5s
    # was tighter than its sibling for no functional reason.
    tools = _tools_with_timeout("siril", 1.0)
    with pytest.raises(ExternalToolTimeout):
        run_external([sys.executable, "-c", script], tool="siril", tools=tools)

    assert pid_file.exists(), "child never reached the point of recording PIDs"
    child_pid_s, grandchild_pid_s = pid_file.read_text().split()
    child_pid, grandchild_pid = int(child_pid_s), int(grandchild_pid_s)

    assert _wait_until_gone(child_pid, timeout=8.0), "direct child still running after runner returned"
    assert _wait_until_gone(grandchild_pid, timeout=8.0), (
        "grandchild (SIGTERM-ignoring) still running after runner returned -- "
        "SIGKILL escalation must be gated on process-GROUP liveness, not the direct child's poll()"
    )


def test_killpg_after_natural_exit_raises_processlookuperror_is_caught(tmp_path):
    """Confirms the documented darwin race directly: killpg on an already-exited
    group raises ProcessLookupError, and `_terminate_process_tree_posix` must
    swallow it rather than propagate. Runs a process that finishes almost
    immediately, then calls the cleanup helper on the already-reaped handle --
    it must return without raising."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
    proc.wait(timeout=5.0)
    assert proc.poll() is not None  # already exited and reaped
    # Must not raise even though the process group is long gone.
    note = EP._terminate_process_tree_posix(proc)
    assert note is None


def test_killpg_liveness_check_raising_permissionerror_is_treated_as_group_gone(monkeypatch):
    """Real macOS CI failure (GitHub Actions run 30768964744, 2026-08-02):
    `os.killpg(pgid, 0)` -- the liveness probe used after the grace period to
    decide whether to escalate to SIGKILL -- raised `PermissionError` instead
    of `ProcessLookupError` when the process group had fully exited but the
    kernel hadn't finished reclaiming the pgid. None of the four tools this
    runner launches (Siril/GraXpert/StarNet2/exiftool) ever drop privileges,
    so an EPERM at THIS specific check means the group is gone, the same as
    ESRCH -- not that a live, unreachable process exists. Uses a fake Popen
    plus monkeypatched `os.getpgid`/`os.killpg` (not a real process) so this
    reproduces deterministically on every platform, including this one,
    rather than depending on a rare real kernel race.
    Before the fix: PermissionError propagates uncaught out of
    `_terminate_process_tree_posix`, crashing cleanup and masking whatever
    original exception `run_external` was already unwinding for."""
    monkeypatch.setattr(EP, "_GRACE_PERIOD_S", 0.05)

    class _FakeProc:
        pid = 4242

        def __init__(self):
            self._exited = False

        def poll(self):
            return 0 if self._exited else None

        def wait(self, timeout=None):
            self._exited = True
            return 0

    proc = _FakeProc()
    killpg_signals: list[int] = []

    monkeypatch.setattr(os, "getpgid", lambda pid: 4242, raising=False)

    def fake_killpg(pgid, sig):
        killpg_signals.append(sig)
        if sig == 0:
            raise PermissionError("Operation not permitted")

    monkeypatch.setattr(os, "killpg", fake_killpg, raising=False)

    note = EP._terminate_process_tree_posix(proc)

    assert 0 in killpg_signals, "expected the liveness-check killpg(pgid, 0) call to have happened"
    assert note is None, "PermissionError on the liveness check must not surface as a cleanup-status note"


def test_windows_tree_kill_uses_taskkill_then_falls_back_to_direct_kill(monkeypatch):
    """Windows tree-walk logic, unit-tested against a fake Popen (cannot be
    exercised on this Mac): bounded `taskkill /PID <pid> /T /F`, and if the
    tree survives that, a direct-child `Popen.kill()` fallback."""
    monkeypatch.setattr(EP, "_GRACE_PERIOD_S", 0.05)
    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(EP.subprocess, "run", fake_run)

    class _FakeProc:
        pid = 4242

        def __init__(self):
            self.killed = False
            self._exited = False

        def poll(self):
            return 0 if self._exited else None

        def kill(self):
            self.killed = True
            self._exited = True

        def wait(self, timeout=None):
            return 0

    proc = _FakeProc()
    note = EP._terminate_process_tree_windows(proc)

    assert calls, "expected a taskkill invocation"
    assert calls[0][0] == "taskkill"
    assert "/PID" in calls[0] and str(proc.pid) in calls[0]
    assert "/T" in calls[0]
    assert "/F" in calls[0]
    assert proc.killed, "expected the direct-child kill fallback since the fake tree never exits on its own"
    assert note is None


def test_windows_tree_kill_succeeds_without_fallback_when_taskkill_reaps_tree(monkeypatch):
    monkeypatch.setattr(EP, "_GRACE_PERIOD_S", 0.05)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(EP.subprocess, "run", fake_run)

    class _FakeProc:
        pid = 99

        def __init__(self):
            self.killed = False

        def poll(self):
            return 0  # already gone by the time we check -- taskkill did its job

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return 0

    proc = _FakeProc()
    note = EP._terminate_process_tree_windows(proc)
    assert not proc.killed, "fallback kill should not fire when the tree already exited"
    assert note is None


# ---------------------------------------------------------------------------
# C1 (final review): run_external must tree-kill on ANY exception during
# communicate(), not only subprocess.TimeoutExpired -- a Ctrl-C (KeyboardInterrupt)
# during a long external-tool call must not leak the detached child/grandchild the
# runner launched with start_new_session=True (which no longer receives the
# terminal's SIGINT).
# ---------------------------------------------------------------------------


@_posix_kill_semantics
def test_run_external_terminates_process_tree_on_keyboardinterrupt_during_communicate(tmp_path, monkeypatch):
    """Real spawned child + grandchild (the same tree-cleanup fixture as the
    timeout tests above). `Popen.communicate` is wrapped so that, once the real
    process is launched, it raises KeyboardInterrupt instead of returning --
    simulating a Ctrl-C landing while `run_external` is blocked waiting on the
    external tool. The runner must still terminate the whole process tree it
    launched (reusing the same tree-terminate helper the timeout path uses),
    close the pipes, and re-raise the original KeyboardInterrupt -- never leave
    the child/grandchild running."""
    pid_file = tmp_path / "pids.txt"
    script = textwrap.dedent(f"""
        import os, subprocess, sys, time
        grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        with open({str(pid_file)!r}, "w") as f:
            f.write(f"{{os.getpid()}} {{grandchild.pid}}")
        time.sleep(30)
        """)
    tools = _tools_with_timeout("siril", 10.0)  # deliberately long -- interrupt fires first, not the deadline

    real_popen = EP.subprocess.Popen

    def fake_popen(args, **kwargs):
        proc = real_popen(args, **kwargs)

        def raising_communicate(*a, **kw):
            # Give the child a moment to actually spawn its grandchild and record
            # both PIDs before we simulate the interrupt landing.
            time.sleep(0.5)
            raise KeyboardInterrupt()

        proc.communicate = raising_communicate
        return proc

    monkeypatch.setattr(EP.subprocess, "Popen", fake_popen)

    with pytest.raises(KeyboardInterrupt):
        run_external([sys.executable, "-c", script], tool="siril", tools=tools)

    assert pid_file.exists(), "child never reached the point of recording PIDs"
    child_pid_s, grandchild_pid_s = pid_file.read_text().split()
    child_pid, grandchild_pid = int(child_pid_s), int(grandchild_pid_s)

    assert _wait_until_gone(child_pid), "direct child still running after KeyboardInterrupt propagated"
    assert _wait_until_gone(grandchild_pid), (
        "grandchild still running after KeyboardInterrupt propagated -- run_external leaked the tree "
        "on a non-timeout exception"
    )


# ---------------------------------------------------------------------------
# Diagnosis 7 (Task 6): actionable external-tool failures.
#
# The failure surface used to be a first-200-character truncation of whatever
# the tool printed -- which on a chatty tool is a banner and a version string,
# with the one line that says what went wrong cut off the end. These tests pin
# the replacement: a bounded summary that keeps the ACTIONABLE tail, a typed
# failure carrying tool/stage/class/status, a bounded diagnostic log holding the
# full output, and the timeout wording the GUI recognizes left untouched.
# ---------------------------------------------------------------------------


def test_summary_keeps_the_actionable_last_line_not_the_first_200_chars():
    noise = "\n".join(f"log line {i} of routine progress chatter" for i in range(200))
    text = f"{noise}\nError: cannot open file 'M_linear.fit': No such file or directory\n"
    summary = EP.summarize_tool_output(text)

    assert "cannot open file" in summary, "the actionable line must survive"
    assert "log line 0 " not in summary, "leading chatter must not crowd it out"
    assert len(summary) <= EP.SUMMARY_LIMIT


def test_summary_falls_back_to_the_tail_when_no_line_looks_like_an_error():
    text = "\n".join(f"step {i} complete" for i in range(500))
    summary = EP.summarize_tool_output(text)

    assert "step 499 complete" in summary  # the tail is where a failure lands
    assert len(summary) <= EP.SUMMARY_LIMIT


def test_summary_of_empty_output_is_honest_rather_than_blank():
    assert "no output" in EP.summarize_tool_output("").lower()
    assert "no output" in EP.summarize_tool_output("   \n \n").lower()


def test_failure_message_names_tool_and_stage_and_stays_bounded():
    failure = EP.ExternalToolFailure(
        tool="siril",
        failure_class="script_failed",
        summary="Error: cannot open file 'M_linear.fit'",
        stage="removing the light-pollution gradient",
        exit_status=1,
        log_path="/out/M_diagnostics.log",
    )
    msg = str(failure)

    assert "Siril" in msg and "removing the light-pollution gradient" in msg
    assert "cannot open file" in msg
    assert "/out/M_diagnostics.log" in msg, "the bounded full log must be discoverable"
    assert "No source photo was changed." in msg
    assert len(msg) <= EP.MESSAGE_LIMIT


def test_failure_identifies_tool_and_stage_even_with_no_stage_known_yet():
    # a failure raised before any op/manifest context exists still names the tool
    msg = str(EP.ExternalToolFailure(tool="graxpert", failure_class="no_output", summary="model download failed"))
    assert "GraXpert" in msg and "model download failed" in msg


def test_with_stage_fills_in_the_stage_without_losing_the_other_fields():
    bare = EP.ExternalToolFailure(tool="siril", failure_class="script_failed", summary="boom", exit_status=3)
    staged = bare.with_stage("balancing colour")

    assert staged.stage == "balancing colour" and bare.stage is None  # a NEW failure, not a mutation
    assert (staged.tool, staged.failure_class, staged.summary, staged.exit_status) == (
        "siril",
        "script_failed",
        "boom",
        3,
    )
    assert "balancing colour" in str(staged)


def test_diagnostic_log_holds_the_full_output_and_is_capped(tmp_path):
    huge = "x" * (EP.LOG_CAP_BYTES * 3)
    path, warning = EP.write_diagnostic_log(str(tmp_path), tool="siril", stage="stretch", text=huge)

    assert warning is None
    assert path and os.path.exists(path)
    size = os.path.getsize(path)
    assert size <= EP.LOG_CAP_BYTES, f"log must be capped, got {size}"
    body = open(path, encoding="utf-8").read()
    assert "siril" in body and "stretch" in body
    assert "truncated" in body.lower(), "a capped log must SAY it was capped"


def test_diagnostic_log_appends_successive_failures_within_the_cap(tmp_path):
    first, _ = EP.write_diagnostic_log(str(tmp_path), tool="siril", stage="a", text="first failure detail")
    second, _ = EP.write_diagnostic_log(str(tmp_path), tool="graxpert", stage="b", text="second failure detail")

    assert first == second, "one diagnostic log per run directory"
    body = open(first, encoding="utf-8").read()
    assert "first failure detail" in body and "second failure detail" in body


def test_diagnostic_log_write_failure_is_reported_and_never_hides_the_tool_error(tmp_path):
    # an unwritable log directory must degrade to (None, warning) -- the caller
    # still raises the ORIGINAL tool failure (CHANGE_SAFETY rule 5: a secondary
    # failure is reported, never allowed to mask the primary one)
    blocked = tmp_path / "file-not-a-dir"
    blocked.write_text("")
    path, warning = EP.write_diagnostic_log(str(blocked), tool="siril", stage="stretch", text="detail")

    assert path is None
    assert warning and "diagnostic log" in warning.lower()


def test_no_diagnostics_dir_degrades_to_no_log_without_a_warning(tmp_path):
    # not every call site has a run directory (a bare `seestar inspect`); that is
    # not an error, so it must not manufacture a warning
    path, warning = EP.write_diagnostic_log(None, tool="siril", stage=None, text="detail")
    assert path is None and warning is None


def test_timeout_message_wording_is_unchanged_by_the_new_failure_type():
    # the GUI recognizes a timeout ONLY by these two markers; Task 6 must not
    # disturb them (seestar/gui.py::_friendly_error)
    msg = EP._timeout_message("graxpert", 3600.0)
    assert "did not finish within" in msg.lower()
    assert "no source photo was changed" in msg.lower()
    assert "open GraXpert once to finish its model setup" in msg


# ---------------------------------------------------------------------------
# The tool wrappers' own failure path: bounded message + full log on disk.
# ---------------------------------------------------------------------------


def _tools_with_diagnostics(tmp_path):
    return B.ToolPaths(
        siril="siril-cli",
        graxpert="GraXpert",
        starnet2="starnet2",
        exiftool="exiftool",
        diagnostics_dir=str(tmp_path),
    )


def test_failed_siril_script_summarizes_and_logs_the_full_output(tmp_path, monkeypatch):
    """Red before Task 6: the whole output became the message and was then cut to
    its first 200 characters -- Siril's banner. Now the message keeps the
    actionable tail and the FULL text lands in the bounded log."""
    from seestar.adapters import siril as siril_module

    banner = "\n".join(f"Siril startup line {i}" for i in range(80))
    output = f"{banner}\nError: cannot open file 'M_linear.fit'\nScript execution failed\n"

    def _fake_run_external(*a, **kw):
        return subprocess.CompletedProcess(args=["siril-cli"], returncode=1, stdout=output, stderr="")

    monkeypatch.setattr(siril_module, "run_external", _fake_run_external)

    with pytest.raises(EP.ExternalToolFailure) as exc_info:
        siril_module.run_siril(["load x"], _tools_with_diagnostics(tmp_path))

    failure = exc_info.value
    assert failure.tool == "siril" and failure.failure_class == "script_failed"
    assert failure.exit_status == 1
    msg = str(failure)
    assert "cannot open file" in msg, "the actionable line must reach the user"
    assert "Siril startup line 0" not in msg, "the banner must not crowd the message"
    assert len(msg) <= EP.MESSAGE_LIMIT

    log = tmp_path / EP.DIAGNOSTIC_LOG_NAME
    assert log.exists() and str(log) in msg, "the message must say where the full output is"
    body = log.read_text()
    assert "Siril startup line 0" in body and "cannot open file" in body, "the LOG keeps everything"
    # the full text stays available for local classification, never in the message
    assert failure.full_output == output


def test_failed_siril_script_still_raises_when_the_log_cannot_be_written(tmp_path, monkeypatch):
    """CHANGE_SAFETY rule 5: a secondary failure (no log) is REPORTED beside the
    original error, never allowed to replace or hide it."""
    from seestar.adapters import siril as siril_module

    def _fake_run_external(*a, **kw):
        return subprocess.CompletedProcess(
            args=["siril-cli"], returncode=1, stdout="Error: disk went away\nScript execution failed\n", stderr=""
        )

    monkeypatch.setattr(siril_module, "run_external", _fake_run_external)
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("")
    tools = B.ToolPaths(
        siril="siril-cli",
        graxpert="GraXpert",
        starnet2="starnet2",
        exiftool="exiftool",
        diagnostics_dir=str(blocked),
    )

    with pytest.raises(EP.ExternalToolFailure) as exc_info:
        siril_module.run_siril(["load x"], tools)

    msg = str(exc_info.value)
    assert "disk went away" in msg, "the ORIGINAL tool error survives"
    assert "diagnostic log" in msg.lower(), "...and the secondary failure is reported too"
    assert exc_info.value.log_path is None


def test_tool_wrapper_without_a_diagnostics_dir_reports_the_summary_alone(tmp_path, monkeypatch):
    """A call site with no run directory (the duck-typed fakes, `seestar inspect`)
    must still fail with an actionable message -- just without a log path."""
    from seestar.adapters import siril as siril_module

    def _fake_run_external(*a, **kw):
        return subprocess.CompletedProcess(
            args=["siril-cli"], returncode=1, stdout="Error: no such file\nScript execution failed\n", stderr=""
        )

    monkeypatch.setattr(siril_module, "run_external", _fake_run_external)
    bare = B.ToolPaths(siril="siril-cli", graxpert="GraXpert", starnet2="starnet2", exiftool="exiftool")

    with pytest.raises(EP.ExternalToolFailure) as exc_info:
        siril_module.run_siril(["load x"], bare)

    assert "no such file" in str(exc_info.value)
    assert exc_info.value.log_path is None
    assert "Full output:" not in str(exc_info.value)
