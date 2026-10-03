"""Projects kept in a Store (Google Drive), usable from any computer.

In the store:
    Autocut/<project>/project.json      setup: script, takes, voices, options (no paths)
                      files/            screenplay PDF and video takes, as uploaded
                      transcripts/      <file>.words.json, so no computer transcribes twice
                      voice/            converted voice audio, so nothing is paid for twice
                      runs/<run id>/    result.json, summary.json, log.txt, preview, fcpxml

On each computer, files are downloaded on demand into a stable local folder
(<workspace>/projects/<project>/), which Resolve can keep pointing at. Files uploaded from
this computer are used where they already are, not copied. A local index remembers which
local file is which Drive file, so nothing is downloaded twice.

Files are referred to by "refs": {"id", "name", "size", "md5"}.
"""

import json
import re
import socket
import time
from pathlib import Path

from .cloud.store import md5_file
from .report import AutocutError, Reporter
from .transcribe import cache_path

SUBFOLDERS = ("files", "transcripts", "voice", "runs")


class Conflict(AutocutError):
    def __init__(self, modified):
        super().__init__("This project was changed on another computer since you opened it.")
        self.modified = modified


def default_state():
    return {"version": 1, "script": None, "pages": "", "takes": {}, "voices": {},
            "options": {}, "preview": True, "name": ""}


def _safe(name):
    return re.sub(r"[^\w.\- ]+", "_", name).strip() or "project"


def list_projects(store):
    root = store.app_root()
    return [{"id": i["id"], "name": i["name"], "modified": i["modified"]}
            for i in store.list(root, folders_only=True)]


def create_project(store, name, home):
    name = _safe(name)
    root = store.app_root()
    if store.find(root, name):
        raise AutocutError(f"A project called {name!r} already exists.")
    folder = store.ensure_folder(name, root)
    for sub in SUBFOLDERS:
        store.ensure_folder(sub, folder)
    store.write_json(folder, "project.json", default_state())
    return Project(store, folder, name, home)


class Project:
    def __init__(self, store, folder_id, name, home):
        self.store, self.id, self.name = store, folder_id, name
        tag = re.sub(r"[^\w]", "", folder_id)[-8:]
        self.local = Path(home).expanduser() / "projects" / f"{_safe(name)}-{tag}"
        (self.local / "files").mkdir(parents=True, exist_ok=True)
        (self.local / "voice").mkdir(exist_ok=True)
        self._subs = {}
        self._index_file = self.local / "index.json"

    @classmethod
    def open(cls, store, folder_id, home):
        return cls(store, folder_id, store.meta(folder_id)["name"], home)

    def sub(self, name):
        if name not in self._subs:
            self._subs[name] = self.store.ensure_folder(name, self.id)
        return self._subs[name]

    # ------------------------------------------------------------------ state
    def load_state(self):
        data, item = self.store.read_json(self.id, "project.json")
        return {**default_state(), **(data or {})}, (item or {}).get("modified")

    def save_state(self, state, base_modified=None):
        """Write project.json; refuse if someone else saved since `base_modified`."""
        item = self.store.find(self.id, "project.json")
        if item and base_modified and item["modified"] != base_modified:
            raise Conflict(item["modified"])
        new = self.store.write_json(self.id, "project.json", state, file_id=item["id"] if item else None)
        return new["modified"]

    # ------------------------------------------------------------------ local copies
    def _index(self):
        try:
            return json.loads(self._index_file.read_text())
        except (OSError, ValueError):
            return {}

    def _remember(self, ref, path):
        idx = self._index()
        st = Path(path).stat()
        idx[ref["id"]] = {"path": str(path), "size": st.st_size, "mtime": st.st_mtime, "md5": ref.get("md5")}
        self._index_file.write_text(json.dumps(idx, indent=1))

    def local_path(self, ref):
        """This computer's copy of a project file, if it has an up-to-date one."""
        e = self._index().get(ref["id"])
        if not e:
            return None
        p = Path(e["path"])
        try:
            st = p.stat()
        except OSError:
            return None
        if st.st_size != e["size"] or (ref.get("size") and st.st_size != ref["size"]):
            return None
        if ref.get("md5") and e.get("md5") and ref["md5"] != e["md5"]:
            return None
        return p

    def ensure_local(self, ref, rep=None):
        rep = rep or Reporter()
        p = self.local_path(ref)
        if p:
            return p
        dest = self.local / "files" / ref["name"]
        if dest.exists() and ref.get("md5") and dest.stat().st_size == ref.get("size") \
                and md5_file(dest) == ref["md5"]:
            self._remember(ref, dest)
            return dest
        rep.log(f"Downloading {ref['name']} ({_mb(ref.get('size'))}) from Drive...")
        self.store.download(ref["id"], dest, _progress(rep, ref["name"]))
        self._remember(ref, dest)
        return dest

    def upload_file(self, local, rep=None):
        """Upload a file from this computer into files/; returns its ref."""
        rep = rep or Reporter()
        local = Path(local)
        if not local.is_file():
            raise AutocutError(f"File not found: {local}")
        existing = {i["name"] for i in self.store.list(self.sub("files"))}
        name, n = local.name, 2
        while name in existing:
            name = f"{local.stem}-{n}{local.suffix}"
            n += 1
        rep.log(f"Uploading {local.name} ({_mb(local.stat().st_size)}) to Drive...")
        item = self.store.upload(local, self.sub("files"), name, _progress(rep, local.name))
        ref = {"id": item["id"], "name": item["name"], "size": item["size"], "md5": item["md5"]}
        self._remember(ref, local)
        # A transcript made for this file earlier can come along.
        self.push_transcript(ref, local)
        return ref

    # ------------------------------------------------------------------ transcripts
    def pull_transcript(self, ref, local):
        cache = cache_path(local)
        if cache.exists():
            return
        item = self.store.find(self.sub("transcripts"), ref["name"] + ".words.json")
        if item:
            self.store.download(item["id"], cache)

    def push_transcript(self, ref, local):
        cache = cache_path(local)
        if not cache.exists():
            return
        name = ref["name"] + ".words.json"
        item = self.store.find(self.sub("transcripts"), name)
        if item and item.get("md5") == md5_file(cache):
            return
        self.store.upload(cache, self.sub("transcripts"), name, file_id=item["id"] if item else None)

    # ------------------------------------------------------------------ voice audio
    @property
    def voice_dir(self):
        return self.local / "voice"

    def pull_voice(self):
        have = {p.name for p in self.voice_dir.iterdir()}
        for item in self.store.list(self.sub("voice")):
            if item["name"] not in have:
                self.store.download(item["id"], self.voice_dir / item["name"])

    def push_voice(self):
        remote = {i["name"] for i in self.store.list(self.sub("voice"))}
        for f in sorted(self.voice_dir.glob("*.wav")):
            if f.name not in remote:
                self.store.upload(f, self.sub("voice"), f.name)

    # ------------------------------------------------------------------ runs
    def push_run(self, run_id, files, summary):
        folder = self.store.ensure_folder(run_id, self.sub("runs"))
        for name, path in files.items():
            if path and Path(path).exists():
                self.store.upload(path, folder, name)
        self.store.write_json(folder, "summary.json", summary)
        return folder

    def list_runs(self):
        runs = []
        for f in self.store.list(self.sub("runs"), folders_only=True):
            summary, _ = self.store.read_json(f["id"], "summary.json")
            if summary:
                runs.append({**summary, "id": f["name"], "folder": f["id"]})
        return sorted(runs, key=lambda r: r.get("created") or 0, reverse=True)

    def run_folder(self, run_id):
        f = self.store.find(self.sub("runs"), run_id)
        if not f:
            raise AutocutError(f"No run {run_id!r} in this project.")
        return f["id"]

    def run_json(self, run_id, name):
        data, _ = self.store.read_json(self.run_folder(run_id), name)
        return data

    def run_file(self, run_id, name):
        """Download one of a run's files (e.g. the preview) into the local project folder."""
        folder = self.run_folder(run_id)
        item = self.store.find(folder, name)
        if not item:
            return None
        dest = self.local / "runs" / run_id / name
        if not (dest.exists() and dest.stat().st_size == item["size"]):
            self.store.download(item["id"], dest)
        return dest


def run_summary(result, name, job_id):
    return {"name": name, "created": time.time(), "host": socket.gethostname(),
            "clips": len(result["segments"]), "duration": result["duration"],
            "flagged": sum(1 for s in result["segments"] if s["flag"]), "job": job_id}


def _mb(n):
    return f"{(n or 0) / 1e6:,.0f} MB" if (n or 0) >= 1e6 else f"{(n or 0) / 1e3:,.0f} KB"


def _progress(rep, name):
    def cb(done, total):
        rep.progress(done / total if total else 1.0, f"{name}: {_mb(done)} / {_mb(total)}")
    return cb
