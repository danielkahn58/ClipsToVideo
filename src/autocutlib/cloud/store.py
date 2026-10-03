"""Storage interface for projects, and LocalStore: a plain folder that behaves like Drive.

Items are dicts: {"id", "name", "folder": bool, "size", "md5", "modified"}. `modified` is an
opaque string that changes whenever the file changes (used to detect another computer's save).
"""

import base64
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from ..report import AutocutError

APP_FOLDER = "Autocut"


class StoreError(AutocutError):
    pass


class Store:
    """What projects need from Drive. Progress callbacks get (bytes_done, bytes_total)."""

    def app_root(self):                                     # id of the "Autocut" folder
        raise NotImplementedError

    def list(self, parent, folders_only=False):
        raise NotImplementedError

    def find(self, parent, name):
        return next((i for i in self.list(parent) if i["name"] == name), None)

    def ensure_folder(self, name, parent):
        raise NotImplementedError

    def upload(self, local, parent, name, progress=None, file_id=None):
        raise NotImplementedError

    def download(self, file_id, local, progress=None):
        raise NotImplementedError

    def read_bytes(self, file_id):
        raise NotImplementedError

    def write_bytes(self, parent, name, data, file_id=None):
        raise NotImplementedError

    def meta(self, file_id):
        raise NotImplementedError

    def account(self):
        return None

    # conveniences
    def read_json(self, parent, name):
        item = self.find(parent, name)
        return (json.loads(self.read_bytes(item["id"])), item) if item else (None, None)

    def write_json(self, parent, name, data, file_id=None):
        return self.write_bytes(parent, name, json.dumps(data, indent=1).encode(), file_id)


def md5_file(path, chunk=1 << 22):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


class LocalStore(Store):
    """A folder standing in for Drive. Ids are opaque (URL-safe encoded relative paths)."""

    def __init__(self, root):
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def _p(self, file_id):
        rel = base64.urlsafe_b64decode(file_id + "=" * (-len(file_id) % 4)).decode() if file_id else ""
        p = (self.root / rel).resolve()
        if self.root.resolve() not in (p, *p.parents):
            raise StoreError(f"Bad id {file_id!r}")
        return p

    def _id(self, p):
        rel = p.relative_to(self.root.resolve()).as_posix()
        return base64.urlsafe_b64encode(rel.encode()).decode().rstrip("=")

    def _item(self, p):
        st = p.stat()
        return {"id": self._id(p.resolve()), "name": p.name, "folder": p.is_dir(),
                "size": 0 if p.is_dir() else st.st_size,
                "md5": None if p.is_dir() else md5_file(p),
                "modified": datetime.fromtimestamp(st.st_mtime_ns / 1e9, timezone.utc).isoformat()}

    def app_root(self):
        return self.ensure_folder(APP_FOLDER, "")

    def list(self, parent, folders_only=False):
        d = self._p(parent)
        return [self._item(p) for p in sorted(d.iterdir())
                if not p.name.endswith(".part") and (p.is_dir() or not folders_only)]

    def ensure_folder(self, name, parent):
        p = self._p(parent) / name
        p.mkdir(parents=True, exist_ok=True)
        return self._id(p.resolve())

    def upload(self, local, parent, name, progress=None, file_id=None):
        dst = self._p(file_id) if file_id else self._p(parent) / name
        total = Path(local).stat().st_size
        tmp = dst.with_name(dst.name + ".part")
        with open(local, "rb") as src, open(tmp, "wb") as out:
            done = 0
            while b := src.read(1 << 22):
                out.write(b)
                done += len(b)
                if progress:
                    progress(done, total)
        tmp.replace(dst)
        return self._item(dst)

    def download(self, file_id, local, progress=None):
        src = self._p(file_id)
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_name(local.name + ".part")
        shutil.copyfile(src, tmp)
        if progress:
            progress(src.stat().st_size, src.stat().st_size)
        tmp.replace(local)

    def read_bytes(self, file_id):
        return self._p(file_id).read_bytes()

    def write_bytes(self, parent, name, data, file_id=None):
        dst = self._p(file_id) if file_id else self._p(parent) / name
        dst.write_bytes(data)
        return self._item(dst)

    def meta(self, file_id):
        return self._item(self._p(file_id))

    def account(self):
        return f"local folder {self.root}"
