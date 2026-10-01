"""Render a quick MP4 of the cut (V1 only) with ffmpeg."""

import subprocess

from .media import _follow_progress
from .report import AutocutError, Reporter


def render_preview(segs, seq_info, out_path, reporter=None):
    rep = reporter or Reporter()
    W, H, rate = seq_info["width"], seq_info["height"], seq_info["rate"]
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1"]
    filters = []
    for i, s in enumerate(segs):
        cmd += ["-ss", f"{s.start:.3f}", "-to", f"{s.end:.3f}", "-i", str(s.video)]
        has_audio = s.primary.take.info["channels"] > 0
        filters.append(
            f"[{i}:v:0]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={rate.numerator}/{rate.denominator}[v{i}];"
            + (f"[{i}:a:0]aresample=48000,aformat=channel_layouts=stereo,"
               f"afade=t=in:d=0.01,areverse,afade=t=in:d=0.01,areverse[a{i}]"  # tiny fades kill clicks
               if has_audio else
               f"anullsrc=r=48000:cl=stereo,atrim=duration={s.end - s.start:.3f}[a{i}]"))
    concat = "".join(f"[v{i}][a{i}]" for i in range(len(segs))) + f"concat=n={len(segs)}:v=1:a=1[v][a]"
    cmd += ["-filter_complex", ";".join(filters + [concat]), "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out_path)]
    rep.log(f"Rendering preview to {out_path}...")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    _follow_progress(proc, sum(s.end - s.start for s in segs), rep)
    err = proc.stderr.read()
    if proc.wait() != 0:
        raise AutocutError(f"ffmpeg failed rendering the preview:\n{err.strip()}")
