"""Runs one job in a child process: python -m autocutlib.web.worker <job_dir>.

A separate process (rather than a thread in the server) so a job can be cancelled mid-
transcription, and so the Whisper model's memory is released when the job ends. Structured
events go to stdout prefixed with \\x1e; anything else on stdout/stderr (WhisperX's own
progress output, warnings) is passed through by the server as plain log lines.
"""

import json
import signal
import sys
import time
import traceback
from pathlib import Path

from ..cloud import get_store
from ..pipeline import Options, export_from_result, run
from ..projects import Project, run_summary
from ..report import AutocutError, Reporter
from ..settings import Settings

EVENT_MARK = "\x1e"


class EventReporter(Reporter):
    def __init__(self):
        self._last_progress = (0.0, -1.0)

    def emit(self, **event):
        sys.stdout.write(EVENT_MARK + json.dumps(event) + "\n")
        sys.stdout.flush()

    def log(self, msg=""):
        for line in str(msg).split("\n"):
            self.emit(type="log", text=line)

    def stage(self, name):
        self._last_progress = (0.0, -1.0)
        self.emit(type="stage", name=name)

    def progress(self, frac, detail=""):
        t, last = self._last_progress
        now = time.monotonic()
        if frac >= 1.0 or frac - last >= 0.01 or now - t > 1.0:
            self._last_progress = (now, frac)
            self.emit(type="progress", frac=round(frac, 4), detail=detail)


def open_project(cfg):
    if not cfg.get("project"):
        return None
    home = Path(cfg["home"])
    store = get_store(Settings(home / "settings.json"))
    if store is None:
        raise AutocutError("Not signed in to Google Drive on this computer.")
    return Project.open(store, cfg["project"]["id"], home)


def run_cut(cfg, job_dir, rep):
    proj = open_project(cfg)
    opts = Options.from_dict({**cfg["options"], "pages": cfg.get("pages")})
    script, videos, refs = cfg.get("script"), [tuple(v) for v in cfg.get("videos", [])], {}
    if proj:
        rep.stage("Getting files from Google Drive")
        script = str(proj.ensure_local(cfg["script_ref"], rep))
        videos = []
        for char, ref in cfg["take_refs"]:
            local = proj.ensure_local(ref, rep)
            proj.pull_transcript(ref, local)
            videos.append((char, str(local)))
            refs[str(local)] = ref
        proj.pull_voice()
        opts.voice_cache_dir = str(proj.voice_dir)

    result = run(script, videos, cfg["out"], cfg.get("preview"), opts, rep)

    if proj:
        for t in result["takes"]:
            t["ref"] = refs.get(t["source"])
        result["project"] = {"id": proj.id, "name": proj.name}
        (job_dir / "result.json").write_text(json.dumps(result, indent=1))
        rep.stage("Saving to Google Drive")
        for t in result["takes"]:
            if t["ref"]:
                proj.push_transcript(t["ref"], t["source"])
        proj.push_voice()
        proj.push_run(job_dir.name, {
            "result.json": job_dir / "result.json",
            "log.txt": job_dir / "log.txt",
            "preview.mp4": cfg.get("preview"),
            Path(cfg["out"]).name: cfg["out"],
        }, run_summary(result, cfg.get("name"), job_dir.name))
        rep.log("Saved this run to the project in Google Drive.")
    return result


def run_upload(cfg, rep):
    proj = open_project(cfg)
    out = []
    for n, f in enumerate(cfg["files"], 1):
        rep.stage(f"Uploading {Path(f['path']).name} ({n}/{len(cfg['files'])})")
        out.append({"for": f["for"], "ref": proj.upload_file(f["path"], rep)})
    return {"uploaded": out}


def run_export(cfg, job_dir, rep):
    proj = open_project(cfg)
    result = proj.run_json(cfg["run_id"], "result.json")
    if not result:
        raise AutocutError("That run has no result to export.")
    missing = [t["character"] for t in result["takes"] if not t.get("ref")]
    if missing:
        raise AutocutError("This run's takes aren't in the project's Drive folder, so it can only be "
                           "exported on the computer that made it.")
    rep.stage("Getting the takes from Google Drive")
    files = [proj.ensure_local(t["ref"], rep) for t in result["takes"]]
    proj.pull_voice()
    o = dict(result.get("options", {}))
    o.update(convert_dir=cfg["converted_dir"] if o.get("convert_dir") else None,
             fallback_dir=cfg["converted_dir"], voice_cache_dir=str(proj.voice_dir))
    opts = Options.from_dict(o)
    rep.stage("Writing the timeline for this computer")
    export_from_result(result, files, cfg["out"], rep, opts)
    proj.push_voice()          # in case a voice had to be converted here
    return {"fcpxml": str(Path(cfg["out"]).resolve()), "run": cfg["run_id"]}


def main():
    # Cancel arrives as SIGTERM; raise so cleanup (e.g. half-written conversions) runs.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    job_dir = Path(sys.argv[1])
    cfg = json.loads((job_dir / "job.json").read_text())
    rep = EventReporter()
    try:
        kind = cfg.get("kind", "cut")
        if kind == "upload":
            result = run_upload(cfg, rep)
        elif kind == "export":
            result = run_export(cfg, job_dir, rep)
        else:
            result = run_cut(cfg, job_dir, rep)
        (job_dir / "result.json").write_text(json.dumps(result, indent=1))
        rep.emit(type="result", result=result)
    except AutocutError as e:
        rep.emit(type="error", message=str(e))
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 - report anything, the server shows it
        rep.log(traceback.format_exc())
        rep.emit(type="error", message=f"{type(e).__name__}: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
