"""Static shell: copy characterization (spec R5), CSP discipline, and the node
reducer-suite wrapper. The wrapper SKIPS loudly without node; Task 6 is only done
once it has run green on the dev Mac (a skipped gate is not a gate)."""

import os
import re
import shutil
import subprocess
from importlib import resources

import pytest

from seestar.gui import views


def _static(name: str) -> str:
    return resources.files("seestar.gui").joinpath("static", name).read_text(encoding="utf-8")


def test_shell_copy_is_pinned():
    html = _static("index.html")
    for copy_str in (
        "Photon Finish",
        "Get a finished photo without editing it yourself. Your original captures are never changed.",
        "Select your copied Seestar folder, then find the photos inside it.",
        "Find photos",
        "Create finished photo",
        "Choose one photo. The all-photos option is a longer batch.",
        "A style controls the overall look. One style may create several useful image versions.",
        "The first photo may take extra time while processing components are prepared.",
        "Advanced: manually adjust this image",
        "Re-create using saved adjustments",
        "Finish this photo, then stop",
        "The Recommended image is the main result.",
        "Quit Photon Finish",
    ):
        assert copy_str in html, copy_str


def test_degraded_copy_and_busy_label_live_in_app_js():
    js = _static("app.js")
    assert "can't reach its processing engine" in js
    assert "Your photos and any completed images are safe." in js
    assert '"Creating…"' in js


def test_polling_is_serial_timed_and_result_dom_is_revision_guarded():
    js = _static("app.js")
    assert "setInterval" not in js
    assert "pollInFlight" in js
    assert "AbortController" in js
    assert "renderedResultRevision" in js


def test_frontend_is_explicitly_an_es_module():
    assert '"type": "module"' in _static("package.json")


def test_all_sentinel_matches_views():
    assert f'"{views.ALL_TARGETS}"' in _static("state.js")


def test_csp_discipline_no_inline_script_or_style():
    html = _static("index.html")
    assert "<script>" not in html  # only src= module scripts
    assert "style=" not in html
    assert "onclick=" not in html and "onload=" not in html
    assert 'src="/static/app.js"' in html and 'href="/static/app.css"' in html


def test_favicon_asset_exists():
    assert "<svg" in _static("favicon.svg")


def test_relaunch_page_copy():
    from seestar.gui.server import RELAUNCH_PAGE

    assert "link is no longer valid" in RELAUNCH_PAGE
    assert "Return to the Photon Finish window or terminal" in RELAUNCH_PAGE


def test_node_reducer_suite_passes():
    node = shutil.which("node")
    if node is None:
        pytest.skip(
            "node not installed — install a supported Node LTS; the front-end reducer suite is a REQUIRED local and CI gate"
        )
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # NOTE: passing a bare directory to `node --test` does NOT recurse into it on
    # real Node (verified against both a Current and an LTS build: it tries to
    # `require()` the directory path itself and fails with MODULE_NOT_FOUND) —
    # only explicit files or glob patterns work. A glob keeps this future-proof
    # as more *.test.mjs files are added under tests/frontend/.
    r = subprocess.run([node, "--test", "tests/frontend/**/*.test.mjs"], capture_output=True, text=True, cwd=repo_root)
    assert r.returncode == 0, f"node --test failed:\n{r.stdout}\n{r.stderr}"


def test_folder_pick_has_no_client_deadline_at_all():
    """Browse has exactly two outcomes: the user cancels (keep the current value) or
    picks a folder (replace it). A client deadline invents a third — "we stopped
    listening" — which silently keeps the old value while the dialog delivers the
    right answer to nobody.

    That was the 2026-08-22 defect: a blanket 10s abort against a dialog waiting on
    a human. The owner's point, and the reason this test asserts NO deadline rather
    than a bigger one: window mode's native dialog has no timeout, so any finite
    number reproduces the bug for a user who takes longer than it."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    js = open(os.path.join(root, "seestar", "gui", "static", "app.js"), encoding="utf-8").read()

    assert "timeoutMs: NO_DEADLINE" in js, "the folder pick must opt out of the deadline"
    assert re.search(r"const NO_DEADLINE = 0\b", js), "NO_DEADLINE must mean 'no abort timer'"
    # ...and the helper must actually honor 0 rather than treating it as "abort now".
    assert re.search(r"timeoutMs > 0 \? setTimeout", js), "api() must skip the timer when timeoutMs is 0"
