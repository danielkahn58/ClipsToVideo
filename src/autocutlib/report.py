"""Progress/log reporting. The CLI prints; the web worker emits JSON events."""


class AutocutError(Exception):
    """A user-facing failure (bad input, missing file, nothing aligned...)."""


class Reporter:
    """Default reporter: plain text on stdout, which is what the CLI wants."""

    def log(self, msg=""):
        print(msg, flush=True)

    def warn(self, msg):
        self.log(f"  ! {msg}")

    def stage(self, name):
        """A new phase of the job begins (probe, convert, transcribe, ...)."""
        self.log(f"\n== {name}")

    def progress(self, frac, detail=""):
        """Fractional progress (0..1) within the current stage, when it's knowable."""
