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

from ..pipeline import Options, run
from ..report import AutocutError, Reporter

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


def main():
    # Cancel arrives as SIGTERM; raise so cleanup (e.g. half-written conversions) runs.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    job_dir = Path(sys.argv[1])
    cfg = json.loads((job_dir / "job.json").read_text())
    rep = EventReporter()
    try:
        opts = Options.from_dict({**cfg["options"], "pages": cfg.get("pages")})
        result = run(cfg["script"], [tuple(v) for v in cfg["videos"]], cfg["out"],
                     cfg.get("preview"), opts, rep)
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
