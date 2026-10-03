"""Where projects are stored: Google Drive (signed in), or a plain folder standing in for it.

AUTOCUT_STORE=local:/some/folder makes every computer pointing at that folder share projects
without Google - used by the tests, and handy for trying the feature out.
"""

import os

from .store import LocalStore, Store, StoreError  # noqa: F401


def get_store(settings):
    """The project store for this computer, or None if not signed in."""
    spec = os.environ.get("AUTOCUT_STORE", "")
    if spec.startswith("local:"):
        return LocalStore(spec[len("local:"):])
    token = settings.get("google_token")
    if token:
        from .gdrive import GoogleDriveStore
        return GoogleDriveStore(token)
    return None
