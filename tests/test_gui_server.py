"""Route + auth-matrix tests with FastAPI TestClient (spec §11). Needs fastapi+httpx
(dev extra); skipped, never silently green, when they are absent."""

import os
import sys

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from seestar.gui import views  # noqa: E402
from seestar.gui.jobs import EngineHooks, JobRegistry  # noqa: E402
from seestar.gui.server import CSRF_HEADER, AuthState, create_app  # noqa: E402

HOST = "127.0.0.1:7777"
TARGETS = {
    "targets": [{"target": "M8", "folder": "/lib/M8", "filter": "LP", "sessions": 1, "datetime": "20260615-221349"}],
    "skipped": [],
}


@pytest.fixture()
def app_env(tmp_path):
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>Photon Finish</title>", encoding="utf-8")
    (static / "app.js").write_text("// js", encoding="utf-8")
    state = {}
    calls = {"quit": 0, "picked": [], "opened": []}

    def process_target(target, request, cb):
        cb("stretch", "Stretching", "Siril", 5, 9)
        return {
            "target": target["target"],
            "ok": True,
            "images": [],
            "qa": None,
            "replay": None,
            "warnings": [],
            "error": None,
        }

    registry = JobRegistry(
        EngineHooks(discover=lambda d: TARGETS, validate=lambda r: [], process_target=process_target)
    )
    auth = AuthState()
    app = create_app(
        registry=registry,
        auth=auth,
        expected_hosts={HOST, "localhost:7777"},
        static_dir=str(static),
        mode="browser",
        load_state=lambda: dict(state),
        save_state=lambda **kw: state.update({k: v for k, v in kw.items() if v is not None}),
        discover=lambda d: TARGETS,
        pick_folder=lambda current: calls["picked"].append(current) or "/picked",
        request_quit=lambda: calls.__setitem__("quit", calls["quit"] + 1),
        open_recipe=lambda p: calls["opened"].append(p) or f"Opened {os.path.basename(p)}",
    )
    client = TestClient(app, base_url=f"http://{HOST}")
    return client, auth, registry, state, calls


def _signin(client, auth):
    r = client.get(f"/?launch={auth.launch_token}", follow_redirects=False)
    assert r.status_code == 303 and "pf_session" in r.headers.get("set-cookie", "")
    return {CSRF_HEADER: "1"}


# ---- auth matrix (spec §11) --------------------------------------------------


def test_launch_token_exchanges_once_and_redirects(app_env):
    client, auth, *_ = app_env
    h = _signin(client, auth)
    # replayed token on the COOKIED client: serves the app (the cookie wins),
    # but mints no new session cookie
    r2 = client.get(f"/?launch={auth.launch_token}", follow_redirects=False)
    assert r2.status_code == 200 and "set-cookie" not in r2.headers
    # with the cookie, plain / serves the app page
    r3 = client.get("/")
    assert r3.status_code == 200 and "Photon Finish" in r3.text
    assert client.get("/api/state", headers=h).status_code == 200


def test_consumed_token_without_cookie_gets_relaunch_page(app_env):
    """The real §8 single-use case: a SECOND, cookieless browser presenting the
    already-consumed token must land on the friendly relaunch page, cookieless."""
    client, auth, *_ = app_env
    _signin(client, auth)  # consumes the token
    fresh = TestClient(client.app, base_url=f"http://{HOST}")  # no cookies
    r = fresh.get(f"/?launch={auth.launch_token}", follow_redirects=False)
    assert r.status_code == 200 and "set-cookie" not in r.headers
    assert "link is no longer valid" in r.text


def test_uncookied_api_is_401_and_results_too(app_env):
    client, *_ = app_env
    assert client.get("/api/state").status_code == 401
    assert client.get("/results/abc/0").status_code == 401


def test_post_without_custom_header_is_403(app_env):
    client, auth, *_ = app_env
    _signin(client, auth)
    r = client.post("/api/scan", json={"library_dir": "/lib"})
    assert r.status_code == 403


def test_bad_host_is_400_even_with_cookie(app_env):
    client, auth, *_ = app_env
    h = _signin(client, auth)
    r = client.get("/api/state", headers={**h, "host": "evil.example:7777"})
    assert r.status_code == 400


def test_bad_launch_token_gets_friendly_page_not_cookie(app_env):
    client, *_ = app_env
    r = client.get("/?launch=WRONG", follow_redirects=False)
    assert r.status_code == 200 and "set-cookie" not in r.headers


def test_security_headers_and_no_store(app_env):
    client, auth, *_ = app_env
    h = _signin(client, auth)
    r = client.get("/api/state", headers=h)
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    page = client.get("/")
    assert page.headers["cache-control"] == "no-store"
    asset = client.get("/static/app.js")
    assert asset.status_code == 200 and asset.headers["cache-control"] == "no-store"


# ---- routes -------------------------------------------------------------------


def test_state_prefs_roundtrip(app_env):
    client, auth, _reg, state, _calls = app_env
    h = _signin(client, auth)
    r = client.put("/api/prefs", json={"library": "/lib", "profile": "natural"}, headers=h)
    assert r.status_code == 200
    got = client.get("/api/state", headers=h).json()
    assert got["prefs"] == {"library": "/lib", "profile": "natural"}
    assert got["mode"] == "browser" and got["job"] is None


def test_accepted_render_persists_the_canonical_request(app_env):
    client, auth, _reg, state, _calls = app_env
    h = _signin(client, auth)
    r = client.post(
        "/api/render",
        headers=h,
        json={
            "library_dir": "/lib/../lib",
            "which": " M8 ",
            "profile": "showcase",
            "output_root": "/out/../out",
            "use_recipe": False,
        },
    )
    assert r.status_code == 200
    assert state == {"library": os.path.realpath("/lib"), "output": os.path.realpath("/out"), "profile": "showcase"}
    assert r.json()["job"]["request"]["which"] == "M8"


def test_prefs_rejects_unknown_fields_and_bad_profile(app_env):
    client, auth, *_ = app_env
    h = _signin(client, auth)
    assert client.put("/api/prefs", json={"evil": "x"}, headers=h).status_code == 422
    assert client.put("/api/prefs", json={"profile": "neon"}, headers=h).status_code == 422


def test_scan_returns_view_model(app_env):
    client, auth, *_ = app_env
    h = _signin(client, auth)
    r = client.post("/api/scan", json={"library_dir": "/definitely/missing"}, headers=h)
    assert r.json()["message"] == views.SCAN_CHOOSE_FOLDER


def test_render_roundtrip_and_named_job_fetch(app_env, tmp_path):
    """Start -> named snapshot fetch -> unknown id 404. (The 409 busy shape is a
    jobs-layer behavior pinned in test_gui_jobs; the HTTP mapping is pinned by
    test_render_validation_maps_to_400 below on the same code path.)"""
    client, auth, reg, *_ = app_env
    h = _signin(client, auth)
    body = {
        "library_dir": "/lib",
        "which": "M8",
        "profile": "showcase",
        "output_root": str(tmp_path),
        "use_recipe": False,
    }
    r = client.post("/api/render", json=body, headers=h)
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    reg.join(timeout=10)
    got = client.get(f"/api/jobs/{job_id}", headers=h).json()
    assert got["state"] == "done" and got["summary"].startswith("✅ Finished: M8")
    assert client.get("/api/jobs/nope", headers=h).status_code == 404


def test_render_validation_maps_to_400(app_env):
    client, auth, reg, *_ = app_env
    h = _signin(client, auth)
    reg._hooks = reg._hooks.__class__(  # same registry, refusing validate
        discover=reg._hooks.discover,
        validate=lambda r: [views.MSG_CHOOSE_OUTPUT],
        process_target=reg._hooks.process_target,
    )
    r = client.post(
        "/api/render",
        json={"library_dir": "/lib", "which": "M8", "profile": "showcase", "output_root": "", "use_recipe": False},
        headers=h,
    )
    assert r.status_code == 400 and "saved" in r.json()["message"]


def test_render_body_is_strict(app_env):
    client, auth, *_ = app_env
    h = _signin(client, auth)
    r = client.post(
        "/api/render", json={"library_dir": "/lib", "which": "M8", "profile": "vivid", "output_root": "/out"}, headers=h
    )
    assert r.status_code == 422  # profile enum
    r = client.post(
        "/api/render",
        json={
            "library_dir": "/lib",
            "which": "M8",
            "profile": "showcase",
            "output_root": "/out",
            "recipe_path": "/evil",
        },
        headers=h,
    )
    assert r.status_code == 422  # extra=forbid: the GUI can never supply a recipe path


def test_stop_route_translates_registry_answer(app_env, tmp_path):
    client, auth, reg, *_ = app_env
    h = _signin(client, auth)
    r = client.post(
        "/api/render",
        json={
            "library_dir": "/lib",
            "which": "M8",
            "profile": "showcase",
            "output_root": str(tmp_path),
            "use_recipe": False,
        },
        headers=h,
    )
    job_id = r.json()["job_id"]
    reg.join(timeout=10)
    assert client.post(f"/api/jobs/{job_id}/stop", headers=h).status_code == 409  # idle/single: no boundary


def test_fine_tune_and_recipe_open(app_env, tmp_path):
    client, auth, _reg, _state, calls = app_env
    h = _signin(client, auth)
    root = tmp_path
    (root / "M8").mkdir()
    r = client.post("/api/fine-tune", json={"which": "M8", "output_root": str(root)}, headers=h)
    assert r.json() == {"available": False}
    rp = root / "M8" / "M8_recipe.json"
    rp.write_text("{}")
    r = client.post("/api/fine-tune", json={"which": "M8", "output_root": str(root)}, headers=h)
    assert r.json() == {"available": True}
    r = client.post("/api/recipe/open", json={"which": "M8", "output_root": str(root)}, headers=h)
    assert r.status_code == 200 and calls["opened"] == [str(rp)]
    r = client.post("/api/recipe/open", json={"which": "M17", "output_root": str(root)}, headers=h)
    assert r.status_code == 404 and "M17" in r.json()["message"]
    r = client.post("/api/recipe/open", json={"which": "../evil", "output_root": str(root)}, headers=h)
    assert r.status_code == 400  # unsafe label is rejected, not resolved


def test_pick_folder_returns_path_and_serializes(app_env):
    client, auth, _reg, _state, calls = app_env
    h = _signin(client, auth)
    r = client.post("/api/pick-folder", json={"current": "/somewhere"}, headers=h)
    assert r.json() == {"path": "/picked"} and calls["picked"] == ["/somewhere"]


def test_quit_idle_triggers_shutdown_callback(app_env):
    client, auth, _reg, _state, calls = app_env
    h = _signin(client, auth)
    assert client.post("/api/quit", headers=h).status_code == 200
    assert calls["quit"] == 1
    refused = client.post(
        "/api/render",
        json={"library_dir": "/lib", "which": "M8", "profile": "showcase", "output_root": "/out"},
        headers=h,
    )
    assert refused.status_code == 409 and "shutting down" in refused.json()["message"].lower()


def test_quit_and_render_are_409_while_a_job_runs(app_env, tmp_path):
    """Spec §4/§11: busy quit is REFUSED (and never calls the shutdown callback);
    a second render is refused with the busy copy. Held open by a real worker."""
    import threading

    client, auth, reg, _state, calls = app_env
    h = _signin(client, auth)
    release = threading.Event()

    def slow(target, request, cb):
        release.wait(timeout=30)
        return {
            "target": target["target"],
            "ok": True,
            "images": [],
            "qa": None,
            "replay": None,
            "warnings": [],
            "error": None,
        }

    reg._hooks = reg._hooks.__class__(discover=reg._hooks.discover, validate=lambda r: [], process_target=slow)
    body = {
        "library_dir": "/lib",
        "which": "M8",
        "profile": "showcase",
        "output_root": str(tmp_path),
        "use_recipe": False,
    }
    assert client.post("/api/render", json=body, headers=h).status_code == 200
    try:
        q = client.post("/api/quit", headers=h)
        assert q.status_code == 409 and "stay open" in q.json()["message"]
        assert calls["quit"] == 0, "a refused quit must not trigger shutdown"
        second = client.post("/api/render", json=body, headers=h)
        assert second.status_code == 409 and "background" in second.json()["message"].lower()
        assert second.json()["job"]["id"] == reg.snapshot()["id"]
        assert q.json()["job"]["id"] == reg.snapshot()["id"]
    finally:
        release.set()
        reg.join(timeout=10)


def _render_one_recorded_image(client, h, reg, tmp_path, real):
    """Drive one render whose result records exactly `real`; return its job id."""

    def with_image(target, request, cb):
        return {
            "target": "M8",
            "ok": True,
            "images": [{"path": str(real), "label": "Recommended", "filename": "M8_final.jpg"}],
            "qa": None,
            "replay": None,
            "warnings": [],
            "error": None,
        }

    reg._hooks = reg._hooks.__class__(discover=reg._hooks.discover, validate=lambda r: [], process_target=with_image)
    r = client.post(
        "/api/render",
        json={
            "library_dir": "/lib",
            "which": "M8",
            "profile": "showcase",
            "output_root": str(tmp_path),
            "use_recipe": False,
        },
        headers=h,
    )
    job_id = r.json()["job_id"]
    reg.join(timeout=10)
    return job_id


def test_results_serves_only_recorded_realpaths(app_env, tmp_path):
    client, auth, reg, *_ = app_env
    h = _signin(client, auth)
    real = tmp_path / "M8_final.jpg"
    real.write_bytes(b"\xff\xd8jpegish")
    job_id = _render_one_recorded_image(client, h, reg, tmp_path, real)
    ok = client.get(f"/results/{job_id}/0", headers=h)
    assert ok.status_code == 200 and ok.content.startswith(b"\xff\xd8")
    assert client.get(f"/results/{job_id}/1", headers=h).status_code == 404  # unrecorded index
    assert client.get(f"/results/{job_id}/-1", headers=h).status_code == 404
    assert client.get("/results/WRONG/0", headers=h).status_code == 404


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="creating the symlink needs SeCreateSymbolicLinkPrivilege (admin or Developer Mode). "
    "The guard itself is platform-neutral -- os.path.realpath resolves symlinks on Windows too -- "
    "so only the attack construction is unavailable here, not the behaviour under test.",
)
def test_results_rejects_a_symlink_swapped_in_after_recording(app_env, tmp_path):
    """The recorded realpath is re-resolved per request, so replacing the recorded
    file with a symlink pointing elsewhere must 404 rather than serve the new
    target (spec section 8)."""
    client, auth, reg, *_ = app_env
    h = _signin(client, auth)
    real = tmp_path / "M8_final.jpg"
    real.write_bytes(b"\xff\xd8jpegish")
    job_id = _render_one_recorded_image(client, h, reg, tmp_path, real)
    assert client.get(f"/results/{job_id}/0", headers=h).status_code == 200

    real.unlink()
    real.symlink_to("/etc/hosts")
    assert client.get(f"/results/{job_id}/0", headers=h).status_code == 404


# ---- security-gate fixes (task 5) ----------------------------------------------


def test_non_ascii_credentials_do_not_crash_and_keep_headers(app_env):
    client, auth, *_ = app_env
    # non-ASCII launch token: must be a normal auth failure, not a 500, WITH headers
    r = client.get("/?launch=caf%C3%A9", follow_redirects=False)
    assert r.status_code == 200  # relaunch page (no valid cookie/token)
    assert r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    # non-ASCII cookie on a protected route: 401, not 500, still header-bearing.
    # httpx's str-keyed `cookies=` kwarg rejects non-ASCII outright (its own header
    # encoder raises before the request is even sent), so the raw byte>=0x80 an
    # adversary would actually put on the wire is built as a raw header tuple.
    r2 = client.get("/api/state", headers=httpx.Headers([(b"cookie", b"pf_session=caf\xe9")]))
    assert r2.status_code == 401
    assert r2.headers["x-content-type-options"] == "nosniff"


def test_unhandled_error_still_carries_security_headers(app_env):
    """discover_targets raising (e.g. an unreadable library dir, OSError) must not
    shed the CSP/no-store/nosniff policy. registry.start() calls hooks.discover(...)
    SYNCHRONOUSLY from the /api/render route (before the worker thread is spawned),
    so that -- not /api/scan, whose `discover` is a separate closure the route
    receives directly, unconnected to the registry hooks -- is the reachable path."""
    client, auth, reg, *_ = app_env
    _signin(client, auth)

    def boom(_):
        raise OSError("unreadable")

    reg._hooks = reg._hooks.__class__(discover=boom, validate=lambda r: [], process_target=reg._hooks.process_target)
    # raise_server_exceptions=False so a genuine escape of our handler would show up
    # as a real response instead of a raised exception; carries the same session
    # cookie the shared client already holds.
    local = TestClient(client.app, base_url=f"http://{HOST}", raise_server_exceptions=False, cookies=client.cookies)
    r = local.post(
        "/api/render",
        json={"library_dir": "/lib", "which": "M8", "profile": "showcase", "output_root": "/out", "use_recipe": False},
        headers={CSRF_HEADER: "1"},
    )
    assert r.status_code == 500
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"
    assert "unreadable" not in r.text  # no detail leak


def test_authstate_repr_hides_secrets():
    from seestar.gui.server import AuthState

    a = AuthState()
    text = repr(a)
    assert a.launch_token not in text and a.session_secret not in text


def _app_with_picker(tmp_path, picker):
    """The app_env fixture with ONE substitution: the folder picker."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>Photon Finish</title>", encoding="utf-8")
    (static / "app.js").write_text("// js", encoding="utf-8")
    registry = JobRegistry(
        EngineHooks(discover=lambda d: TARGETS, validate=lambda r: [], process_target=lambda *a: None)
    )
    auth = AuthState()
    app = create_app(
        registry=registry,
        auth=auth,
        expected_hosts={HOST, "localhost:7777"},
        static_dir=str(static),
        mode="window",
        load_state=dict,
        save_state=lambda **kw: None,
        discover=lambda d: TARGETS,
        pick_folder=picker,
        request_quit=lambda: None,
        open_recipe=lambda p: "",
    )
    return TestClient(app, base_url=f"http://{HOST}"), auth


def test_pick_folder_failure_reports_instead_of_returning_a_null_path(tmp_path):
    """Owner report 2026-08-21: choosing a folder appeared to "revert" to the
    previous one. Root shape: a FAILING picker and a CANCELLED picker both produced
    `{"path": null}`, and the front end does nothing with a null -- leaving the old
    path on screen with no explanation. A silent degrade is a defect (tenet 7), so a
    failure now answers with a message the UI actually shows."""
    from seestar.gui import views

    def _boom(current):
        raise RuntimeError("no window server")

    client, auth = _app_with_picker(tmp_path, _boom)
    headers = _signin(client, auth)
    r = client.post("/api/pick-folder", json={"current": ""}, headers=headers)

    assert r.status_code == 500
    assert r.json()["message"] == views.MSG_PICKER_FAILED


def test_pick_folder_cancel_stays_quiet(tmp_path):
    """The other half: a genuine cancel is not a failure. It must NOT raise an
    error banner -- the user chose to keep the folder they already had."""
    client, auth = _app_with_picker(tmp_path, lambda current: None)
    headers = _signin(client, auth)
    r = client.post("/api/pick-folder", json={"current": ""}, headers=headers)

    assert r.status_code == 200
    assert r.json()["path"] is None
