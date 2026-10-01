"""Synthetic inputs: a screenplay PDF, test-pattern takes, and transcript caches.

Transcripts are written as <video>.words.json, so the pipeline never needs WhisperX.
Also runnable to make a demo folder for poking at the web UI:
    python tests/fixtures.py /tmp/autocut-demo
"""

import json
import subprocess
import sys
from pathlib import Path

DIALOGUE = [
    ("MIKEY", "Did you take the last of the coffee?"),
    ("CLAIRE", "I did not touch your coffee."),
    ("MIKEY", "Then who finished it?"),
    ("MIKEY", "It was full this morning."),
    ("CLAIRE", "Maybe the cat drank it."),
    ("MIKEY", "We don't have a cat."),
]


def make_screenplay(path):
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Courier", 12)
    y = 720

    def row(x, text, gap=True):
        nonlocal y
        if gap:
            y -= 12
        c.drawString(x, y, text)
        y -= 12

    row(108, "INT. KITCHEN - NIGHT")
    row(108, "Mikey stares into an empty pot.")
    prev = None
    for n, (who, text) in enumerate(DIALOGUE):
        if who == prev:
            row(108, "He sets the pot down.")
        row(252, who + (" (CONT'D)" if who == prev else ""))
        if n == 1:
            row(223, "(not looking up)", gap=False)
        row(180, text, gap=False)
        prev = who
    row(540, "CUT TO:")
    c.save()


def words_for(timings):
    """timings: start time (s) of each DIALOGUE line as heard in this take."""
    words = []
    for (_, text), t in zip(DIALOGUE, timings):
        for w in text.split():
            words.append({"word": w, "start": round(t, 3), "end": round(t + 0.3, 3)})
            t += 0.35
    return words


def make_video(path, seconds=24, size="320x240", rate="24", vcodec="libx264", acodec="aac",
               tone=440, extra=()):
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size={size}:rate={rate}:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency={tone}:duration={seconds}:sample_rate=48000",
         "-c:v", vcodec, "-c:a", acodec, *extra, "-shortest", str(path)],
        check=True)


# Line start times per take. MIKEY take 2 is missing line 6 ("We don't have a cat").
TAKES = {
    "mikey.mov": dict(timings=[1, 4, 7, 9, 13, 16], vcodec="libx264", acodec="aac", tone=440),
    "claire.mp4": dict(timings=[1.5, 4.5, 7.5, 9.5, 13.5, 16.5], vcodec="libx264", acodec="aac",
                       tone=660),
    "mikey2.mpeg": dict(timings=[2, 5, 8, 10, 14], vcodec="mpeg2video", acodec="mp2", tone=550),
}


def make_project(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    make_screenplay(folder / "scene.pdf")
    for name, t in TAKES.items():
        make_video(folder / name, vcodec=t["vcodec"], acodec=t["acodec"], tone=t["tone"])
        (folder / (name + ".words.json")).write_text(json.dumps(words_for(t["timings"])))
    return folder


if __name__ == "__main__":
    print(make_project(sys.argv[1] if len(sys.argv) > 1 else "demo"))
