import os

import pytest

from seestar.inspect import sanitize_label
from seestar.paths import assert_output_disjoint_from_source, safe_output_dir


@pytest.mark.parametrize("bad", ["..", ".", "...", f"..{os.sep}.."])
def test_sanitize_label_rejects_dot_traversal(bad):
    assert sanitize_label(bad) is None


def test_sanitize_label_strips_leading_dot():
    # a hidden-file style label must not survive as a dotfile/dir
    assert sanitize_label(".secret") == "secret"


def test_sanitize_label_keeps_normal_names():
    assert sanitize_label("M 8") == "M8"
    assert sanitize_label("NGC 5139") == "NGC5139"


def test_safe_output_dir_contains_normal_label(tmp_path):
    got = safe_output_dir(str(tmp_path), "M8")
    assert got == os.path.join(os.path.realpath(str(tmp_path)), "M8")


def test_safe_output_dir_rejects_parent_escape(tmp_path):
    with pytest.raises(ValueError):
        safe_output_dir(str(tmp_path), "..")


def test_output_equal_to_source_rejected(tmp_path):
    d = str(tmp_path / "M8")
    os.makedirs(d)
    with pytest.raises(ValueError):
        assert_output_disjoint_from_source(d, d)


def test_output_nested_in_source_rejected(tmp_path):
    src = str(tmp_path / "lib")
    os.makedirs(src)
    out = os.path.join(src, "out")
    with pytest.raises(ValueError):
        assert_output_disjoint_from_source(src, out)


def test_source_nested_in_output_rejected(tmp_path):
    out = str(tmp_path / "out")
    os.makedirs(out)
    src = os.path.join(out, "lib")
    os.makedirs(src)
    with pytest.raises(ValueError):
        assert_output_disjoint_from_source(src, out)


def test_disjoint_dirs_allowed(tmp_path):
    src = str(tmp_path / "lib")
    os.makedirs(src)
    out = str(tmp_path / "out")
    os.makedirs(out)
    assert_output_disjoint_from_source(src, out)  # no raise


def test_disjoint_dirs_on_different_drives_allowed(tmp_path, monkeypatch):
    """os.path.commonpath raises ValueError (not a shared-prefix result) when the
    two paths are on different Windows drives (e.g. source on G:\\, output on C:\\)
    -- there is no common path because they can't possibly overlap, which IS
    disjoint. A real Google-Drive-mounted source with a local C:\\ output hit this
    and surfaced as an unhandled exception in the GUI (blank Gradio error toast)."""
    src = str(tmp_path / "lib")
    os.makedirs(src)
    out = str(tmp_path / "out")
    os.makedirs(out)

    def fake_commonpath(paths):
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr(os.path, "commonpath", fake_commonpath)
    assert_output_disjoint_from_source(src, out)  # no raise


def test_process_library_rejects_output_root_nested_in_library(tmp_path):
    from seestar.io import load_config
    from seestar.run import process_library

    lib = tmp_path / "lib"
    lib.mkdir()
    out_root = os.path.join(str(lib), "renders")
    with pytest.raises(ValueError):
        process_library(str(lib), out_root, load_config())


def test_process_library_accepts_disjoint_output_root(tmp_path, monkeypatch):
    import seestar.run as run_module
    from seestar.io import load_config

    lib = tmp_path / "lib"
    lib.mkdir()
    out_root = tmp_path / "out"
    out_root.mkdir()

    monkeypatch.setattr(run_module, "discover_targets", lambda d: {"targets": [], "skipped": []})
    batch = run_module.process_library(str(lib), str(out_root), load_config())
    assert batch.successes == () and batch.failures == () and batch.status_failures == ()
    assert batch.ok is True


def test_process_library_source_output_disjoint_guard_precedes_discovery(tmp_path, monkeypatch):
    """The root-level source/output-disjoint guard (tenet 2: source data is sacred)
    is not a per-target concern -- it must fire before discovery or any target is
    processed, for the whole batch, every run."""
    import seestar.run as run_module
    from seestar.io import load_config

    lib = tmp_path / "lib"
    lib.mkdir()
    out_root = os.path.join(str(lib), "out")  # nested in source -> violates disjointness

    discover_calls = []
    monkeypatch.setattr(
        run_module, "discover_targets", lambda d: discover_calls.append(d) or {"targets": [], "skipped": []}
    )
    with pytest.raises(ValueError):
        run_module.process_library(str(lib), out_root, load_config())
    assert discover_calls == []


def test_process_library_output_dir_containment_failure_aborts(tmp_path, monkeypatch):
    """A `safe_output_dir` containment failure for one discovered target (e.g. a
    target label that would escape the output root) is a path-safety violation,
    not an ordinary per-target processing failure -- it must abort the whole batch
    rather than being isolated as a TargetFailure, and run_target must never be
    reached for that target. `discover_targets` is stubbed directly so this test
    exercises `process_library`'s own per-target loop boundary rather than
    depending on `discover_targets`'s own label sanitization."""
    import seestar.run as run_module
    from seestar.io import load_config

    lib = tmp_path / "lib"
    lib.mkdir()
    out_root = tmp_path / "out"
    out_root.mkdir()

    monkeypatch.setattr(
        run_module,
        "discover_targets",
        lambda d: {"targets": [{"target": "..", "folder": str(lib / "weird")}], "skipped": []},
    )
    calls = []
    monkeypatch.setattr(run_module, "run_target", lambda *a, **kw: calls.append(a) or {"status": "pass"})

    with pytest.raises(ValueError):
        run_module.process_library(str(lib), str(out_root), load_config())
    assert calls == []
