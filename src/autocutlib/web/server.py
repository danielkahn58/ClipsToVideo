"""Local web UI for autocut: autocut-web, then open http://127.0.0.1:8765

Media is referenced where it already lives on disk (picked with the macOS file dialog or
the built-in browser), so multi-gigabyte takes are never copied. Uploading is offered too,
for files that aren't on this machine yet; those are stored in the workspace's uploads/
folder, which is then their permanent location as far as the FCPXML is concerned.
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from flask import Flask, Response, abort, jsonify, redirect, request, send_file, send_from_directory

from ..cloud import get_store
from ..media import FFMPEG_INSTALL, converted_path, probe, resolve_problems
from ..pipeline import Options
from ..report import AutocutError
from ..projects import Conflict, Project, create_project, list_projects
from ..screenplay import characters, dump_text, load_dialogue
from ..settings import Settings
from ..transcribe import cache_path
from ..voice import PRICE_PER_MIN, VOICES
from ..voice import cache_dir as voice_cache_dir
from ..voice import preview as voice_preview
from .worker import EVENT_MARK

STATIC = Path(__file__).parent / "static"
SRC_ROOT = Path(__file__).resolve().parents[2]       # .../src, for the worker's PYTHONPATH
IS_MAC = sys.platform == "darwin"
VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".mxf", ".mpeg", ".mpg", ".m2v", ".vob", ".avi", ".mkv",
              ".mts", ".m2ts", ".ts", ".wmv", ".webm", ".dv", ".3gp"}
MODELS = ["large-v3", "large-v3-turbo", "large-v2", "medium", "medium.en", "small", "small.en",
          "base", "base.en", "tiny", "tiny.en"]


def create_app(home, port=8765):
    home = Path(home).expanduser()
    jobs_dir, uploads_dir, converted_dir = home / "jobs", home / "uploads", home / "converted"
    for d in (jobs_dir, uploads_dir, converted_dir):
        d.mkdir(parents=True, exist_ok=True)

    app = Flask(__name__, static_folder=None)
    settings = Settings(home / "settings.json")
    jobs = JobStore(jobs_dir, settings)
    pending_sign_ins = {}          # OAuth state -> flow (holds the PKCE verifier)

    # ------------------------------------------------------------------ guard
    # The server can read and list local files, so only answer this machine's own pages:
    # reject other Host names (DNS rebinding) and require a custom header on API calls,
    # which a cross-site form or fetch can't send without a CORS preflight we never grant.
    @app.before_request
    def guard():
        m = re.match(r"^(\[[^\]]*\]|[^:]*)(:\d+)?$", request.host)
        host = m.group(1) if m else ""
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            abort(403)
        open_paths = request.method == "GET" and (
            ("/api/jobs/" in request.path and (request.path.endswith("/events") or "/file/" in request.path))
            or request.path.startswith("/api/voice/audio/")
            or (request.path.startswith("/api/projects/") and request.path.endswith("/preview")))
        if request.path.startswith("/api/") and not open_paths and request.headers.get("X-Autocut") != "1":
            abort(403)

    @app.errorhandler(AutocutError)
    def autocut_error(e):
        if isinstance(e, Conflict):
            return jsonify(error=str(e), conflict=True, modified=e.modified), 409
        return jsonify(error=str(e)), 400

    def store():
        st = get_store(settings)
        if st is None:
            raise AutocutError("Sign in with Google first.")
        return st

    def project(pid):
        return Project.open(store(), pid, home)

    def body():
        return request.get_json(silent=True) or {}

    def existing(path, what="File"):
        if not path:
            raise AutocutError(f"No {what.lower()} given.")
        p = Path(path).expanduser()
        if not p.exists():
            raise AutocutError(f"{what} not found: {p}")
        return p

    # ------------------------------------------------------------------ pages
    @app.get("/")
    def index():
        return send_from_directory(STATIC, "index.html")

    @app.get("/static/<path:name>")
    def static_file(name):
        return send_from_directory(STATIC, name)

    @app.get("/api/config")
    def config():
        return jsonify(
            home=str(home), uploads=str(uploads_dir), converted=str(converted_dir),
            mac=IS_MAC, picker=IS_MAC and shutil.which("osascript") is not None,
            ffmpeg=shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None,
            ffmpeg_install=FFMPEG_INSTALL,
            whisperx=importlib.util.find_spec("whisperx") is not None,
            models=MODELS, defaults=Options().__dict__, user_home=str(Path.home()),
            voices=VOICES, voice_price=PRICE_PER_MIN, fal_key=settings.fal_key() is not None,
            fal_key_from_env="FAL_KEY" in os.environ, google=google_status_dict(),
        )

    # ------------------------------------------------------------------ Google sign-in
    def google_status_dict():
        local = os.environ.get("AUTOCUT_STORE", "").startswith("local:")
        signed_in = local or bool(settings.get("google_token"))
        return {"client": local or bool(settings.get("google_client")), "signed_in": signed_in,
                "account": "local test folder" if local else settings.get("google_account"),
                "local_store": local}

    @app.get("/api/google")
    def google_status():
        return jsonify(google_status_dict())

    @app.post("/api/google/client")
    def google_client():
        from ..cloud.gdrive import check_client_config
        settings.set("google_client", check_client_config(body().get("json") or ""))
        return jsonify(google_status_dict())

    @app.post("/api/google/connect")
    def google_connect():
        from ..cloud.gdrive import find_client_file, start_sign_in
        client = settings.get("google_client")
        if not client:
            # One-time per computer: pick up the client file from where it was downloaded.
            path, client = find_client_file([Path.home() / d for d in ("Downloads", "Desktop", "Documents")]
                                            + [home, SRC_ROOT.parent])
            if not client:
                return jsonify(need_client=True)
            settings.set("google_client", client)
        flow, url, state = start_sign_in(client, f"http://127.0.0.1:{port}/oauth/callback")
        pending_sign_ins[state] = flow
        return jsonify(url=url)

    @app.get("/oauth/callback")
    def oauth_callback():
        from ..cloud.gdrive import GoogleDriveStore, finish_sign_in
        flow = pending_sign_ins.pop(request.args.get("state", ""), None)
        if request.args.get("error") or not flow or not request.args.get("code"):
            msg = request.args.get("error") or "This sign-in link expired. Start again from the app."
            return (f"<p>Google sign-in didn't complete: {msg}</p><p><a href='/'>Back to autocut</a></p>", 400)
        try:
            token = finish_sign_in(flow, request.args["code"])
            settings.set("google_token", token)
            settings.set("google_account", GoogleDriveStore(token).account())
        except Exception as e:  # noqa: BLE001 - show whatever Google said
            return (f"<p>Google sign-in failed: {e}</p><p><a href='/'>Back to autocut</a></p>", 400)
        return redirect("/?signed_in=1")

    @app.post("/api/google/disconnect")
    def google_disconnect():
        settings.set("google_token", None)
        settings.set("google_account", None)
        return jsonify(google_status_dict())

    # ------------------------------------------------------------------ projects
    @app.get("/api/projects")
    def projects_list():
        return jsonify(projects=list_projects(store()))

    @app.post("/api/projects")
    def projects_create():
        name = (body().get("name") or "").strip()
        if not name:
            raise AutocutError("Give the project a name.")
        proj = create_project(store(), name, home)
        return jsonify(id=proj.id, name=proj.name)

    def local_status(proj, state):
        refs = ([state["script"]] if state.get("script") else []) + \
            [r for rs in state.get("takes", {}).values() for r in rs]
        return {r["id"]: str(proj.local_path(r) or "") for r in refs}

    @app.get("/api/projects/<pid>")
    def projects_get(pid):
        proj = project(pid)
        state, modified = proj.load_state()
        return jsonify(id=proj.id, name=proj.name, state=state, modified=modified,
                       local_dir=str(proj.local), local=local_status(proj, state))

    @app.put("/api/projects/<pid>/state")
    def projects_save(pid):
        b = body()
        proj = project(pid)
        modified = proj.save_state(b.get("state") or {}, b.get("base"))
        return jsonify(modified=modified, local=local_status(proj, b.get("state") or {}))

    @app.post("/api/projects/<pid>/upload")
    def projects_upload(pid):
        proj = project(pid)
        files = [{"path": str(existing(f.get("path"))), "for": f.get("for")} for f in body().get("files", [])]
        if not files:
            raise AutocutError("No files to upload.")
        job = jobs.create("upload", {"kind": "upload", "home": str(home), "name": "upload",
                                     "project": {"id": proj.id, "name": proj.name}, "files": files},
                          preview=False)
        return jsonify(job.summary())

    @app.post("/api/projects/<pid>/parse")
    def projects_parse(pid):
        proj = project(pid)
        state, _ = proj.load_state()
        if not state.get("script"):
            raise AutocutError("Add the screenplay PDF first.")
        path = proj.ensure_local(state["script"])        # PDFs are small: fetch right away
        lines = load_dialogue(path, body().get("pages") or None)
        return jsonify(path=str(path), lines=[{"n": i, "page": ln.page, "character": ln.character,
                                               "text": ln.text} for i, ln in enumerate(lines, 1)],
                       characters=[{"name": c, "lines": n} for c, n in characters(lines).items()],
                       dump=dump_text(lines))

    @app.post("/api/projects/<pid>/probe")
    def projects_probe(pid):
        proj = project(pid)
        ref = body().get("ref") or {}
        p = proj.local_path(ref)
        in_drive = bool(proj.store.find(proj.sub("transcripts"), ref.get("name", "") + ".words.json"))
        if not p:
            return jsonify(local=False, name=ref.get("name"), size=ref.get("size") or 0, cached=in_drive)
        info = probe(p)
        return jsonify(local=True, path=str(p), name=ref.get("name"), size=p.stat().st_size,
                       width=info["width"], height=info["height"], fps=float(info["rate"]),
                       duration=info["duration"], vcodec=info["vcodec"], acodec=info["acodec"],
                       problems=resolve_problems(info), converted=None, converted_exists=False,
                       cached=in_drive or cache_path(p).exists())

    @app.get("/api/projects/<pid>/runs")
    def projects_runs(pid):
        return jsonify(runs=project(pid).list_runs())

    @app.get("/api/projects/<pid>/runs/<run_id>")
    def projects_run(pid, run_id):
        proj = project(pid)
        folder = proj.run_folder(run_id)
        log = proj.store.find(folder, "log.txt")
        local = jobs.get(run_id)
        return jsonify(result=proj.run_json(run_id, "result.json"),
                       summary=proj.run_json(run_id, "summary.json"),
                       log=proj.store.read_bytes(log["id"]).decode("utf-8", "replace") if log else "",
                       local_job=run_id if local and local.out.exists() else None)

    @app.get("/api/projects/<pid>/runs/<run_id>/preview")
    def projects_run_preview(pid, run_id):
        local = jobs.get(run_id)
        path = local.preview if local and local.preview and local.preview.exists() else \
            project(pid).run_file(run_id, "preview.mp4")
        if not path:
            abort(404)
        return send_file(path, mimetype="video/mp4", as_attachment=request.args.get("download") == "1",
                         download_name=f"{run_id}_preview.mp4", conditional=True, max_age=0)

    @app.post("/api/projects/<pid>/runs/<run_id>/export")
    def projects_export(pid, run_id):
        proj = project(pid)
        if jobs.running():
            raise AutocutError("A job is already running. Wait for it, or cancel it first.")
        job = jobs.create(f"export-{run_id}", {
            "kind": "export", "home": str(home), "name": run_id, "run_id": run_id,
            "project": {"id": proj.id, "name": proj.name}, "converted_dir": str(converted_dir)},
            preview=False)
        return jsonify(job.summary())

    @app.post("/api/settings")
    def save_settings():
        b = body()
        if "fal_key" in b:
            settings.set("fal_key", (b["fal_key"] or "").strip() or None)
        return jsonify(fal_key=settings.fal_key() is not None)

    # ------------------------------------------------------------------ voice
    @app.post("/api/voice/preview")
    def preview_voice():
        b = body()
        if b.get("project") and b.get("ref"):
            p = project(b["project"]).local_path(b["ref"])
            if not p:
                raise AutocutError("That take isn't on this computer yet. It's downloaded when you "
                                   "build the cut (or upload it from here).")
        else:
            p = existing(b.get("path"), "Video")
        voice = (b.get("voice") or "").strip()
        if not voice:
            raise AutocutError("Pick a voice first.")
        key = settings.fal_key()
        if not key:
            raise AutocutError("Set your fal.ai API key first (Options > Voice).")
        os.environ.setdefault("FAL_KEY", key)
        # Start just before the first word heard in the take, if it's been transcribed.
        start = 0.0
        for c in (cache_path(p), cache_path(converted_path(p))):
            if c.exists():
                try:
                    words = json.loads(c.read_text())
                    start = max(0.0, float(words[0]["start"]) - 0.3) if words else 0.0
                except (ValueError, KeyError, IndexError, TypeError):
                    pass
                break
        seed = int(b["seed"]) if str(b.get("seed", "")).strip().lstrip("-").isdigit() else 42
        stab = b.get("stability")
        stab = min(max(float(stab), 0.0), 1.0) if isinstance(stab, (int, float)) else None
        wav = voice_preview(p, voice, start, 8.0, bool(b.get("denoise")), seed, stability=stab)
        return jsonify(url=f"/api/voice/audio/{wav.name}", start=start)

    @app.get("/api/voice/audio/<name>")
    def voice_audio(name):
        if not re.fullmatch(r"[0-9a-f]{20}-\d+\.wav", name):
            abort(404)
        return send_from_directory(voice_cache_dir(), name, mimetype="audio/wav")

    # ------------------------------------------------------------------ files
    @app.post("/api/pick")
    def pick():
        if not IS_MAC:
            raise AutocutError("The native file dialog is only available on macOS; use Browse.")
        b = body()
        return jsonify(paths=native_pick(b.get("kind", "video"), bool(b.get("multiple")), b.get("start")))

    @app.get("/api/fs")
    def browse():
        kind = request.args.get("kind", "video")
        p = Path(request.args.get("path") or Path.home()).expanduser()
        if p.is_file():
            p = p.parent
        if not p.is_dir():
            raise AutocutError(f"Not a folder: {p}")
        exts = {".pdf"} if kind == "pdf" else VIDEO_EXTS
        dirs, files = [], []
        try:
            entries = sorted(p.iterdir(), key=lambda e: e.name.lower())
        except PermissionError:
            raise AutocutError(f"Permission denied: {p} (grant your terminal Files & Folders "
                               "access in System Settings > Privacy & Security)") from None
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir():
                    dirs.append(e.name)
                elif e.suffix.lower() in exts and not e.name.endswith(".partial.mov"):
                    files.append({"name": e.name, "size": e.stat().st_size})
            except OSError:
                continue
        return jsonify(path=str(p), parent=str(p.parent) if p.parent != p else None,
                       dirs=dirs, files=files)

    @app.put("/api/upload")
    def upload():
        name = re.sub(r"[^\w.\- ]+", "_", Path(request.args.get("name", "upload")).name).strip() or "upload"
        dest = uploads_dir / name
        n = 1
        while dest.exists():
            dest = uploads_dir / f"{Path(name).stem}-{n}{Path(name).suffix}"
            n += 1
        part = dest.with_name(dest.name + ".part")
        with open(part, "wb") as f:
            while chunk := request.stream.read(1 << 20):
                f.write(chunk)
        part.replace(dest)
        return jsonify(path=str(dest))

    @app.post("/api/reveal")
    def reveal():
        p = existing(body().get("path"))
        if IS_MAC:
            subprocess.run(["open", "-R", str(p)], check=False)
        return jsonify(ok=IS_MAC)

    # ------------------------------------------------------------------ inputs
    @app.post("/api/script/parse")
    def parse_script():
        b = body()
        path = existing(b.get("path"), "Screenplay")
        lines = load_dialogue(path, b.get("pages") or None)
        return jsonify(
            path=str(path),
            lines=[{"n": i, "page": ln.page, "character": ln.character, "text": ln.text}
                   for i, ln in enumerate(lines, 1)],
            characters=[{"name": c, "lines": n} for c, n in characters(lines).items()],
            dump=dump_text(lines),
        )

    @app.post("/api/probe")
    def probe_take():
        b = body()
        p = existing(b.get("path"), "Video")
        info = probe(p)
        problems = resolve_problems(info)
        conv = converted_path(p, None if b.get("convert_next_to_original", True) else converted_dir)
        return jsonify(
            path=str(p), name=p.name, size=p.stat().st_size,
            width=info["width"], height=info["height"], fps=float(info["rate"]),
            duration=info["duration"], timecode=info["timecode"],
            vcodec=info["vcodec"], acodec=info["acodec"], channels=info["channels"],
            problems=problems, converted=str(conv) if problems else None,
            converted_exists=bool(problems) and conv.exists(),
            cached=cache_path(p).exists() or (bool(problems) and cache_path(conv).exists()),
        )

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    def list_jobs():
        return jsonify(jobs=[j.summary() for j in jobs.all()[:40]])

    @app.post("/api/jobs")
    def create_job():
        b = body()
        if jobs.running():
            raise AutocutError("A job is already running. Wait for it, or cancel it first.")
        if b.get("project"):
            return create_project_job(b)
        script = existing(b.get("script"), "Screenplay")
        videos = []
        for char, paths in (b.get("takes") or {}).items():
            for p in paths:
                videos.append([char.strip().upper(), str(existing(p, "Video"))])
        if not videos:
            raise AutocutError("Add at least one take.")
        o = dict(b.get("options") or {})
        o["convert_dir"] = None if o.pop("convert_next_to_original", True) else str(converted_dir)
        o["fallback_dir"] = str(converted_dir)
        opts = Options.from_dict(o).__dict__
        name = re.sub(r"[^\w.\- ]+", "_", (b.get("name") or script.stem)).strip() or "cut"
        job = jobs.create(name, {
            "script": str(script), "pages": b.get("pages") or None, "videos": videos,
            "options": opts, "name": name,
        }, preview=bool(b.get("preview", True)))
        return jsonify(job.summary())

    def job_options(b):
        o = dict(b.get("options") or {})
        o["convert_dir"] = None if o.pop("convert_next_to_original", True) else str(converted_dir)
        o["fallback_dir"] = str(converted_dir)
        return Options.from_dict(o).__dict__

    def create_project_job(b):
        proj = project(b["project"])
        state, _ = proj.load_state()
        if not state.get("script"):
            raise AutocutError("Add the screenplay PDF first.")
        take_refs = [[char.strip().upper(), ref] for char, refs in (state.get("takes") or {}).items()
                     for ref in refs]
        if not take_refs:
            raise AutocutError("Add at least one take.")
        name = re.sub(r"[^\w.\- ]+", "_", (b.get("name") or state.get("name") or proj.name)).strip() or "cut"
        job = jobs.create(name, {
            "kind": "cut", "home": str(home), "project": {"id": proj.id, "name": proj.name},
            "script_ref": state["script"], "take_refs": take_refs, "pages": state.get("pages") or None,
            "options": job_options(b), "name": name,
        }, preview=bool(b.get("preview", True)))
        return jsonify(job.summary())

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id):
        job = jobs.get(job_id) or abort(404)
        return jsonify(job.detail())

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id):
        job = jobs.get(job_id) or abort(404)
        job.cancel()
        return jsonify(job.summary())

    @app.get("/api/jobs/<job_id>/events")
    def job_events(job_id):
        job = jobs.get(job_id) or abort(404)
        after = int(request.headers.get("Last-Event-ID") or request.args.get("after") or 0)

        def stream():
            i = after
            while True:
                batch, finished = job.wait(i, timeout=15)
                for ev in batch:
                    i += 1
                    yield f"id: {i}\ndata: {json.dumps(ev)}\n\n"
                if not batch:
                    if finished:
                        yield f"event: end\ndata: {json.dumps(job.summary())}\n\n"
                        return
                    yield ": keepalive\n\n"

        return Response(stream(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/jobs/<job_id>/file/<kind>")
    def job_file(job_id, kind):
        job = jobs.get(job_id) or abort(404)
        path = {"fcpxml": job.out, "preview": job.preview, "log": job.dir / "log.txt"}.get(kind)
        if not path or not Path(path).exists():
            abort(404)
        mime = {"fcpxml": "application/xml", "preview": "video/mp4", "log": "text/plain"}[kind]
        return send_file(path, mimetype=mime, as_attachment=request.args.get("download") == "1",
                         download_name=Path(path).name, conditional=True, max_age=0)

    return app


# ---------------------------------------------------------------------- native picker

def native_pick(kind, multiple, start=None):
    """Show the macOS open dialog (via AppleScript) and return the chosen POSIX paths."""
    def q(s):
        return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'
    types = '{"com.adobe.pdf"}' if kind == "pdf" else '{"public.movie", "public.mpeg", "public.audiovisual-content"}'
    prompt = "Choose the screenplay PDF" if kind == "pdf" else "Choose video take(s)"
    opts = f"with prompt {q(prompt)} of type {types}"
    if multiple:
        opts += " with multiple selections allowed"
    if start and Path(start).expanduser().is_dir():
        opts += f" default location (POSIX file {q(Path(start).expanduser())})"
    script = f"""activate
set chosen to choose file {opts}
set out to ""
repeat with f in (chosen as list)
    set out to out & POSIX path of f & linefeed
end repeat
return out"""
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        if "-128" in r.stderr:          # user cancelled
            return []
        raise AutocutError(f"File dialog failed: {r.stderr.strip()}")
    return [p for p in r.stdout.splitlines() if p.strip()]


# ---------------------------------------------------------------------- jobs

class Job:
    def __init__(self, job_dir):
        self.dir = Path(job_dir)
        self.id = self.dir.name
        self.cfg = json.loads((self.dir / "job.json").read_text())
        state_file = self.dir / "state.json"
        self.state = json.loads(state_file.read_text()) if state_file.exists() else {}
        self.events = []
        ev_file = self.dir / "events.jsonl"
        if ev_file.exists():
            for line in ev_file.read_text().splitlines():
                try:
                    self.events.append(json.loads(line))
                except ValueError:
                    pass
        self.proc = None
        self.cond = threading.Condition()
        self._cancelled = False
        self.settings = None
        if self.state.get("status") in ("running", "starting"):      # server restarted mid-job
            self.state["status"] = "interrupted"
            self._save_state()

    @property
    def out(self):
        return Path(self.cfg["out"])

    @property
    def preview(self):
        return Path(self.cfg["preview"]) if self.cfg.get("preview") else None

    @property
    def status(self):
        return self.state.get("status", "unknown")

    @property
    def finished(self):
        return self.status not in ("running", "starting")

    def _save_state(self):
        (self.dir / "state.json").write_text(json.dumps(self.state))

    def summary(self):
        return {"id": self.id, "name": self.cfg.get("name"), "status": self.status,
                "created": self.state.get("created"), "ended": self.state.get("ended"),
                "error": self.state.get("error"), "script": self.cfg.get("script"),
                "kind": self.kind, "project": (self.cfg.get("project") or {}).get("id")}

    @property
    def kind(self):
        return self.cfg.get("kind", "cut")

    def detail(self):
        result_file = self.dir / "result.json"
        return {**self.summary(), "config": self.cfg,
                "result": json.loads(result_file.read_text()) if result_file.exists() else None}

    def start(self):
        self.state.update(status="running", created=self.state.get("created") or time.time())
        self._save_state()
        key = self.settings.fal_key() if self.settings else None
        env = {**({"FAL_KEY": key} if key else {}), **os.environ, "PYTHONUNBUFFERED": "1",
               "PYTHONPATH": os.pathsep.join(filter(None, [str(SRC_ROOT), os.environ.get("PYTHONPATH")]))}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "autocutlib.web.worker", str(self.dir)],
            cwd=self.dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)
        threading.Thread(target=self._pump, daemon=True).start()

    def _add(self, ev):
        with self.cond:
            self.events.append(ev)
            with open(self.dir / "events.jsonl", "a") as f:
                f.write(json.dumps(ev) + "\n")
            if ev.get("type") == "log":
                with open(self.dir / "log.txt", "a") as f:
                    if not ev.get("replace"):
                        f.write(ev["text"] + "\n")
            if ev.get("type") == "error":
                self.state["error"] = ev["message"]
            self.cond.notify_all()

    def _pump(self):
        """Read the worker's merged stdout/stderr; split on \\n and \\r (progress bars)."""
        pending = b""
        stream = self.proc.stdout
        while chunk := stream.read1(65536):
            pending += chunk
            while (m := re.search(rb"\r\n|\r|\n", pending)) is not None:
                raw, sep, pending = pending[:m.start()], m.group(), pending[m.end():]
                self._line(raw.decode("utf-8", "replace"), replace=sep == b"\r")
        if pending:
            self._line(pending.decode("utf-8", "replace"), replace=False)
        code = self.proc.wait()
        with self.cond:
            if self._cancelled:
                status = "cancelled"
            elif code == 0:
                status = "done"
            else:
                status = "failed"
                self.state.setdefault("error", f"Worker exited with code {code}")
            self.state.update(status=status, ended=time.time())
            self._save_state()
        self._add({"type": "status", "status": status})

    def _line(self, text, replace):
        if text.startswith(EVENT_MARK):
            try:
                self._add(json.loads(text[1:]))
                return
            except ValueError:
                pass
        if text.strip():
            self._add({"type": "log", "text": text.rstrip(), "raw": True, "replace": replace})

    def wait(self, i, timeout):
        with self.cond:
            self.cond.wait_for(lambda: len(self.events) > i or self.finished, timeout=timeout)
            return self.events[i:], self.finished

    def cancel(self):
        if self.proc is None or self.proc.poll() is not None:
            return
        self._cancelled = True
        self._add({"type": "log", "text": "Cancelling..."})
        if os.name == "nt":
            # No process groups on Windows: kill the worker and its ffmpeg children as a tree.
            subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                           capture_output=True, check=False)
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)       # the worker and its ffmpeg children
        except ProcessLookupError:
            return

        def hard_kill():
            time.sleep(5)
            if self.proc.poll() is None:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        threading.Thread(target=hard_kill, daemon=True).start()


class JobStore:
    def __init__(self, root, settings=None):
        self.root = Path(root)
        self.settings = settings
        self.jobs = {}
        self.lock = threading.Lock()
        for d in self.root.iterdir():
            if (d / "job.json").exists():
                try:
                    self.jobs[d.name] = Job(d)
                except (OSError, ValueError):
                    pass

    def all(self):
        return sorted(self.jobs.values(), key=lambda j: j.state.get("created") or 0, reverse=True)

    def get(self, job_id):
        return self.jobs.get(job_id)

    def running(self):
        """A cut or export in progress (uploads can run alongside)."""
        return any(not j.finished and j.kind != "upload" for j in self.jobs.values())

    def create(self, name, cfg, preview):
        with self.lock:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            job_dir = self.root / f"{stamp}-{name}"
            n = 1
            while job_dir.exists():
                job_dir = self.root / f"{stamp}-{name}-{n}"
                n += 1
            job_dir.mkdir(parents=True)
            cfg = {**cfg, "out": str(job_dir / f"{name}.fcpxml"),
                   "preview": str(job_dir / f"{name}_preview.mp4") if preview else None}
            (job_dir / "job.json").write_text(json.dumps(cfg, indent=1))
            job = Job(job_dir)
            job.settings = self.settings
            job.state = {"status": "starting", "created": time.time()}
            self.jobs[job.id] = job
        job.start()
        return job


# ---------------------------------------------------------------------- entry point

def main(argv=None):
    p = argparse.ArgumentParser(prog="autocut-web", description="Local web UI for autocut.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--home", default=os.environ.get("AUTOCUT_HOME", "~/Movies/Autocut"),
                   help="workspace for jobs, uploads and conversions (default ~/Movies/Autocut)")
    p.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = p.parse_args(argv)

    app = create_app(args.home, args.port)
    url = f"http://127.0.0.1:{args.port}"
    print(f"autocut web UI: {url}   (workspace: {Path(args.home).expanduser()})\nCtrl-C to stop.")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    app.run(host="127.0.0.1", port=args.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
