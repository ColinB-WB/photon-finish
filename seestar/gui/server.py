"""FastAPI app factory: thin translation between HTTP and JobRegistry/engine calls.

Auth model (spec §8): one-time launch token -> process-lifetime SameSite=Strict
HttpOnly session cookie; EVERY /api/* and /results/* route requires the cookie;
mutating routes additionally require the X-Photon-Finish header (CSRF
belt-and-braces); the Host header must be a loopback name (DNS-rebinding);
CSP/no-store on everything. No CORS is ever granted. All runtime copy comes
from views.py; the static shell renders server values with textContent.
"""

from __future__ import annotations

import os
import secrets
import threading
from dataclasses import dataclass, field
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import views
from .jobs import JobRegistry, JobRequest, StartRefused

COOKIE_NAME = "pf_session"
CSRF_HEADER = "x-photon-finish"

# Static shell copy (characterized in tests/test_gui_static.py): the page an
# uncookied, untokened visit sees (stale bookmark, second browser, replayed token).
RELAUNCH_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Photon Finish</title></head>
<body><h1>Photon Finish</h1>
<p>This link is no longer valid. Return to the Photon Finish window or terminal that
opened it. If that session has ended, close it fully and launch Photon Finish again.</p>
</body></html>
"""


@dataclass
class AuthState:
    """Mints the one-time launch token + process-lifetime session secret."""

    launch_token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    session_secret: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    consumed: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def try_consume(self, presented) -> bool:
        with self._lock:
            if presented is None or self.consumed:
                return False
            # str(presented).encode(...) is never itself raising; compare_digest on
            # bytes stays constant-time and never raises on non-ASCII input (an
            # adversary-controlled query param), unlike compare_digest on str.
            ok = secrets.compare_digest(str(presented).encode("utf-8", "ignore"), self.launch_token.encode())
            if ok:
                self.consumed = True
            return ok

    def cookie_ok(self, presented) -> bool:
        return presented is not None and secrets.compare_digest(
            str(presented).encode("utf-8", "ignore"), self.session_secret.encode()
        )


class ScanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    library_dir: str = Field(max_length=4096)


class PrefsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    library: str | None = Field(default=None, max_length=4096)
    output: str | None = Field(default=None, max_length=4096)
    profile: Literal["showcase", "natural", "documentation", "print"] | None = None


class RenderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    library_dir: str = Field(max_length=4096)
    which: str = Field(max_length=256)
    profile: Literal["showcase", "natural", "documentation", "print"]
    output_root: str = Field(max_length=4096)
    use_recipe: bool = False  # the SERVER derives the recipe path (v1 §12b)


class TargetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    which: str = Field(max_length=256)
    output_root: str = Field(max_length=4096)


class PickBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current: str = Field(default="", max_length=4096)


def create_app(
    *,
    registry: JobRegistry,
    auth: AuthState,
    expected_hosts: set,
    static_dir: str,
    mode: str,
    load_state,
    save_state,
    discover,
    pick_folder,
    request_quit,
    open_recipe,
) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    pick_lock = threading.Lock()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.headers.get("host", "") not in expected_hosts:
            return _finish(JSONResponse({"message": "Bad request."}, status_code=400))
        path = request.url.path
        protected = path.startswith("/api/") or path.startswith("/results/")
        if protected and not auth.cookie_ok(request.cookies.get(COOKIE_NAME)):
            return _finish(
                JSONResponse({"message": "Not signed in. Open Photon Finish again from the app."}, status_code=401)
            )
        if protected and request.method in ("POST", "PUT", "DELETE") and request.headers.get(CSRF_HEADER) != "1":
            return _finish(JSONResponse({"message": "Missing app header."}, status_code=403))
        return _finish(await call_next(request))

    def _finish(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        # A fixed-port relaunch must never reuse an older app version's assets (spec §8).
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # A crash must not shed the response policy (spec §8): return a header-bearing
        # 500 with no detail, not Starlette's bare error response.
        return _finish(JSONResponse({"message": "Something went wrong."}, status_code=500))

    @app.get("/")
    def index(request: Request):
        token = request.query_params.get("launch")
        if token is not None and auth.try_consume(token):
            resp = RedirectResponse("/", status_code=303)  # strip the token from the address bar
            resp.set_cookie(COOKIE_NAME, auth.session_secret, httponly=True, samesite="strict", path="/")
            return resp
        if not auth.cookie_ok(request.cookies.get(COOKIE_NAME)):
            return HTMLResponse(RELAUNCH_PAGE)
        return FileResponse(os.path.join(static_dir, "index.html"), media_type="text/html")

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/api/state")
    def api_state():
        # `mode` is fixed for the life of the process: the R3 window->browser
        # fallback was removed 2026-08-20, so nothing can change it after startup.
        return {"mode": mode, "prefs": load_state(), "job": registry.snapshot()}

    @app.put("/api/prefs")
    def api_prefs(body: PrefsBody):
        save_state(library=body.library, output=body.output, profile=body.profile)
        return {"ok": True}

    @app.post("/api/scan")
    def api_scan(body: ScanBody):
        library_dir = body.library_dir.strip()
        if not library_dir or not os.path.isdir(library_dir):
            return {"rows": [], "choices": [], "value": None, "message": views.SCAN_CHOOSE_FOLDER, "detail": None}
        return views.scan_view(discover(library_dir))

    @app.post("/api/render")
    def api_render(body: RenderBody):
        request_obj = JobRequest(
            library_dir=body.library_dir.strip(),
            which=body.which,
            profile=body.profile,
            output_root=body.output_root.strip(),
            use_recipe=body.use_recipe,
        )
        try:
            snap = registry.start(request_obj)
        except StartRefused as e:
            payload = {"message": e.message}
            if e.active_snapshot is not None:
                payload["job"] = e.active_snapshot
            return JSONResponse(payload, status_code=409 if e.conflict else 400)
        # Persist only after the registry has accepted and canonicalized the
        # request. This keeps reload state aligned with the authoritative job;
        # a validation/busy refusal cannot overwrite good preferences.
        accepted = snap["request"]
        save_state(library=accepted["library_dir"], output=accepted["output_root"], profile=accepted["profile"])
        return {"job_id": snap["id"], "job": snap}

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: str):
        snap = registry.snapshot(job_id)
        if snap is None:
            return JSONResponse({"message": "That creation is no longer available."}, status_code=404)
        return snap

    @app.post("/api/jobs/{job_id}/stop")
    def api_stop(job_id: str):
        ok, message = registry.request_stop(job_id)
        return JSONResponse({"message": message}, status_code=200 if ok else 409)

    @app.post("/api/fine-tune")
    def api_fine_tune(body: TargetBody):
        return {"available": views.fine_tune_available(body.which, body.output_root.strip())}

    @app.post("/api/recipe/open")
    def api_recipe_open(body: TargetBody):
        try:
            rp = views.recipe_path(body.output_root.strip(), body.which)
        except ValueError:
            return JSONResponse({"message": "Bad request."}, status_code=400)
        if not os.path.exists(rp):
            return JSONResponse({"message": views.msg_no_recipe(body.which)}, status_code=404)
        return {"message": open_recipe(rp)}

    @app.post("/api/pick-folder")
    def api_pick_folder(body: PickBody):
        if not pick_lock.acquire(blocking=False):  # no dialog stacking (spec §5)
            return JSONResponse({"message": views.MSG_PICKER_BUSY}, status_code=409)
        try:
            return {"path": pick_folder(body.current.strip())}
        except Exception:
            # The picker is an OS integration: it can fail for reasons this app does
            # not control. Report it (the front end shows the message) instead of
            # returning a null path, which is indistinguishable from a cancel and
            # leaves the user staring at their previous folder.
            return JSONResponse({"message": views.MSG_PICKER_FAILED}, status_code=500)
        finally:
            pick_lock.release()

    @app.post("/api/quit")
    def api_quit():
        accepted, active = registry.begin_shutdown()
        if not accepted:
            return JSONResponse({"message": views.MSG_QUIT_BUSY, "job": active}, status_code=409)
        request_quit()
        return {"message": "Shutting down. You can close this tab."}

    @app.get("/results/{job_id}/{k}")
    def result_image(job_id: str, k: int):
        recorded = registry.image_path(job_id, k)
        if recorded is None:
            return JSONResponse({"message": "Not found."}, status_code=404)
        real = os.path.realpath(recorded)
        # realpath was taken at RECORD time; if resolving again lands elsewhere
        # (symlink swapped underneath us) or the file vanished, refuse (spec §8).
        if real != recorded or not os.path.isfile(real):
            return JSONResponse({"message": "Not found."}, status_code=404)
        return FileResponse(real, media_type="image/jpeg")

    return app
