"""Per-computer settings (secrets: fal.ai key, Google sign-in), in the workspace's settings.json.

Never synced to Drive: each computer signs in and keeps its own keys.
"""

import json
import os
from pathlib import Path


class Settings:
    def __init__(self, path):
        self.path = Path(path)

    def _read(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def get(self, k, default=None):
        return self._read().get(k, default)

    def set(self, k, v):
        d = self._read()
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def fal_key(self):
        return os.environ.get("FAL_KEY") or self._read().get("fal_key")
