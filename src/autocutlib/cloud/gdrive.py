"""Google Drive as a project store, via the Drive API with the `drive.file` scope.

`drive.file` lets the app see only the files and folders it created itself (everything under
"Autocut/" in My Drive). It's a non-sensitive scope, so a personal OAuth client published "In
production" needs no Google review and its sign-in doesn't expire weekly.
"""

import io
import json
import mimetypes
import os
from pathlib import Path

from .store import APP_FOLDER, Store, StoreError

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
FOLDER = "application/vnd.google-apps.folder"
FIELDS = "id,name,mimeType,size,md5Checksum,modifiedTime"
CHUNK = 8 * 1024 * 1024

# Google may report the granted scopes slightly differently from what was asked; don't fail on it.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")


# ---------------------------------------------------------------------- sign-in

def check_client_config(data):
    """Validate the JSON downloaded from Google Cloud Console; returns it as a dict."""
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except ValueError:
            raise StoreError("That isn't a JSON file.") from None
    if not isinstance(data, dict) or not ({"installed", "web"} & set(data)):
        raise StoreError("This doesn't look like a Google OAuth client file. In Google Cloud Console, "
                         "create an OAuth client of type 'Desktop app' and download its JSON.")
    inner = data.get("installed") or data.get("web")
    if not inner.get("client_id") or not inner.get("client_secret"):
        raise StoreError("The client file is missing client_id or client_secret.")
    return data


# Google names it client_secret_<id>.apps.googleusercontent.com.json; browsers may add "(1)" etc.
CLIENT_FILE_PATTERNS = ("*client_secret*.json", "google_client.json")


def find_client_file(folders):
    """The newest valid Google OAuth client file in `folders` (Google's default download name is
    client_secret_<id>.apps.googleusercontent.com.json), as (path, config), or (None, None)."""
    found = []
    for folder in folders:
        folder = Path(folder).expanduser()
        if not folder.is_dir():
            continue
        for pattern in CLIENT_FILE_PATTERNS:
            found += [p for p in folder.glob(pattern) if p.is_file()]
    for p in sorted(set(found), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            return p, check_client_config(p.read_text())
        except (OSError, StoreError):
            continue
    return None, None


MISSING_LIBS = ("The Google Drive libraries aren't installed. In the ClipsToVideo folder (with the "
                "virtual environment active) run:  pip install -e \".[whisper]\"  then restart autocut-web.")


def require_google_libs():
    try:
        import google.oauth2.credentials  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401
        import googleapiclient.discovery  # noqa: F401
    except ImportError:
        raise StoreError(MISSING_LIBS) from None


def start_sign_in(client_config, redirect_uri):
    """Returns (flow, url, state). Keep the flow: it holds the PKCE verifier for finish_sign_in."""
    require_google_libs()
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_config(client_config, scopes=SCOPES, redirect_uri=redirect_uri)
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    return flow, url, state


def finish_sign_in(flow, code):
    """Exchange the code; returns the token info to store in settings (includes refresh token)."""
    flow.fetch_token(code=code)
    return json.loads(flow.credentials.to_json())


# ---------------------------------------------------------------------- store

def _escape(s):
    return s.replace("\\", "\\\\").replace("'", "\\'")


class GoogleDriveStore(Store):
    def __init__(self, token_info):
        require_google_libs()
        from google.oauth2.credentials import Credentials
        self.creds = Credentials.from_authorized_user_info(token_info, SCOPES)
        self._root = None

    def _svc(self):
        # A fresh service per call: the underlying HTTP client isn't thread-safe, and the
        # web server handles requests on several threads.
        from googleapiclient.discovery import build
        return build("drive", "v3", credentials=self.creds, cache_discovery=False)

    @staticmethod
    def _item(d):
        return {"id": d["id"], "name": d["name"], "folder": d.get("mimeType") == FOLDER,
                "size": int(d.get("size") or 0), "md5": d.get("md5Checksum"),
                "modified": d.get("modifiedTime")}

    def _call(self, request):
        from googleapiclient.errors import HttpError
        try:
            return request.execute()
        except HttpError as e:
            raise StoreError(f"Google Drive: {e.reason if hasattr(e, 'reason') else e}") from None

    def account(self):
        about = self._call(self._svc().about().get(fields="user(emailAddress,displayName)"))
        return about.get("user", {}).get("emailAddress")

    def app_root(self):
        if self._root is None:
            self._root = self.ensure_folder(APP_FOLDER, "root")
        return self._root

    def _query(self, q):
        svc, out, token = self._svc(), [], None
        while True:
            r = self._call(svc.files().list(q=q, spaces="drive", pageSize=1000, pageToken=token,
                                            fields=f"nextPageToken,files({FIELDS})"))
            out += [self._item(f) for f in r.get("files", [])]
            token = r.get("nextPageToken")
            if not token:
                return out

    def list(self, parent, folders_only=False):
        q = f"'{_escape(parent)}' in parents and trashed=false"
        if folders_only:
            q += f" and mimeType='{FOLDER}'"
        return sorted(self._query(q), key=lambda i: i["name"].lower())

    def find(self, parent, name):
        hits = self._query(f"'{_escape(parent)}' in parents and name='{_escape(name)}' and trashed=false")
        return hits[0] if hits else None

    def ensure_folder(self, name, parent):
        hits = self._query(f"'{_escape(parent)}' in parents and name='{_escape(name)}' "
                           f"and mimeType='{FOLDER}' and trashed=false")
        if hits:
            return hits[0]["id"]
        d = self._call(self._svc().files().create(
            body={"name": name, "mimeType": FOLDER, "parents": [parent]}, fields=FIELDS))
        return d["id"]

    def upload(self, local, parent, name, progress=None, file_id=None):
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
        local = Path(local)
        mime = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
        media = MediaFileUpload(str(local), mimetype=mime, chunksize=CHUNK, resumable=True)
        files = self._svc().files()
        req = (files.update(fileId=file_id, media_body=media, fields=FIELDS) if file_id else
               files.create(body={"name": name, "parents": [parent]}, media_body=media, fields=FIELDS))
        total, resp = local.stat().st_size, None
        try:
            while resp is None:
                status, resp = req.next_chunk(num_retries=5)
                if status and progress:
                    progress(int(status.resumable_progress), total)
        except HttpError as e:
            raise StoreError(f"Upload to Google Drive failed: {e}") from None
        if progress:
            progress(total, total)
        return self._item(resp)

    def download(self, file_id, local, progress=None):
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaIoBaseDownload
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        total = self.meta(file_id)["size"]
        tmp = local.with_name(local.name + ".part")
        try:
            with open(tmp, "wb") as fh:
                dl = MediaIoBaseDownload(fh, self._svc().files().get_media(fileId=file_id), chunksize=CHUNK)
                done = False
                while not done:
                    status, done = dl.next_chunk(num_retries=5)
                    if status and progress:
                        progress(int(status.resumable_progress), total)
        except HttpError as e:
            tmp.unlink(missing_ok=True)
            raise StoreError(f"Download from Google Drive failed: {e}") from None
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(local)

    def read_bytes(self, file_id):
        return self._call(self._svc().files().get_media(fileId=file_id))

    def write_bytes(self, parent, name, data, file_id=None):
        from googleapiclient.http import MediaIoBaseUpload
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
        files = self._svc().files()
        req = (files.update(fileId=file_id, media_body=media, fields=FIELDS) if file_id else
               files.create(body={"name": name, "parents": [parent]}, media_body=media, fields=FIELDS))
        return self._item(self._call(req))

    def meta(self, file_id):
        return self._item(self._call(self._svc().files().get(fileId=file_id, fields=FIELDS)))
