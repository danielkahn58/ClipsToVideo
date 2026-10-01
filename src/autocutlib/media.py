"""ffprobe/ffmpeg helpers: probing, timecode, and converting media Resolve can't read."""

import json
import os
import re
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

from .report import AutocutError, Reporter

STANDARD_RATES = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001),
                  Fraction(30), Fraction(50), Fraction(60000, 1001), Fraction(60)]

# What DaVinci Resolve (free, macOS) reliably reads. Anything else gets converted to ProRes.
# Notably .mpeg/.mpg/.vob/.ts files and MP2/MP3 audio are not on the list: Resolve shows them
# without sound, or refuses them.
RESOLVE_CONTAINERS = {".mov", ".mp4", ".m4v", ".mxf"}
RESOLVE_VIDEO = {"h264", "hevc", "prores", "dnxhd", "mjpeg"}   # dnxhd also covers DNxHR
RESOLVE_AUDIO = {"aac", "alac", "pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be",
                 "pcm_s32le", "pcm_s32be", "pcm_f32le", "pcm_f32be"}

CONVERTED_SUFFIX = ".prores.mov"


def require_ffmpeg():
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise AutocutError(f"{' and '.join(missing)} not found on PATH. Install with: brew install ffmpeg")


def probe(path):
    path = Path(path)
    if not path.exists():
        raise AutocutError(f"File not found: {path}")
    require_ffmpeg()
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json",
                          "-show_streams", "-show_format", str(path)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise AutocutError(f"ffprobe failed on {path}:\n{out.stderr.strip()}")
    d = json.loads(out.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise AutocutError(f"No video stream in {path}")

    raw = Fraction(v.get("avg_frame_rate") or v.get("r_frame_rate") or "24")
    if raw == 0:
        raw = Fraction(v.get("r_frame_rate", "24"))
    rate = min(STANDARD_RATES, key=lambda r: abs(float(r) - float(raw)))

    tc = (v.get("tags", {}).get("timecode") or d["format"].get("tags", {}).get("timecode"))
    if not tc:
        for s in d["streams"]:
            tc = tc or s.get("tags", {}).get("timecode")
    return {
        "width": int(v["width"]), "height": int(v["height"]),
        "rate": rate, "fd": 1 / rate,
        "timecode": tc,
        "duration": float(d["format"]["duration"]),
        "channels": int(a.get("channels", 2)) if a else 0,
        "sample_rate": int(a.get("sample_rate", 48000)) if a else 48000,
        "vcodec": v.get("codec_name", "?"),
        "acodec": a.get("codec_name") if a else None,
        "container": path.suffix.lower(),
    }


def timecode_frames(tc, rate):
    """HH:MM:SS:FF (or ;FF for drop-frame) -> frame count."""
    m = re.match(r"(\d+):(\d+):(\d+)([:;.,])(\d+)", tc or "")
    if not m:
        return 0, False
    h, mi, s, sep, f = int(m[1]), int(m[2]), int(m[3]), m[4], int(m[5])
    nominal = round(float(rate))
    frames = (h * 3600 + mi * 60 + s) * nominal + f
    drop = sep in ";," and nominal in (30, 60)
    if drop:
        per = 2 if nominal == 30 else 4
        total_min = 60 * h + mi
        frames -= per * (total_min - total_min // 10)
    return frames, drop


def resolve_problems(info):
    """Reasons Resolve won't handle this file well; empty list means it's fine as-is."""
    problems = []
    if info["container"] not in RESOLVE_CONTAINERS:
        problems.append(f"{info['container'] or 'no extension'} container")
    if info["vcodec"] not in RESOLVE_VIDEO:
        problems.append(f"{info['vcodec']} video")
    if info["acodec"] and info["acodec"] not in RESOLVE_AUDIO:
        problems.append(f"{info['acodec']} audio")
    return problems


def converted_path(src, convert_dir=None):
    """Where the ProRes copy of `src` lives: next to it, or in convert_dir if given."""
    src = Path(src)
    folder = Path(convert_dir).expanduser() if convert_dir else src.parent
    return folder / (src.stem + CONVERTED_SUFFIX)


def _writable_dir(folder):
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(folder, os.W_OK)


def convert_for_resolve(src, info, convert_dir=None, fallback_dir=None, reporter=None):
    """Transcode to ProRes 422 HQ (.mov, 16-bit PCM). Returns the converted path.

    An existing conversion that's newer than the source is reused. Output is written to a
    temporary name and renamed, so an interrupted conversion is never mistaken for a finished one.
    """
    rep = reporter or Reporter()
    src = Path(src)
    dst = converted_path(src, convert_dir)
    if not _writable_dir(dst.parent):
        if fallback_dir is None:
            raise AutocutError(f"Can't write the converted file next to {src}; pass a convert folder.")
        dst = converted_path(src, fallback_dir)
        _writable_dir(dst.parent)
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime:
        rep.log(f"Reusing converted {dst}")
        return dst

    rep.log(f"Converting {src.name} -> {dst} (ProRes 422 HQ, PCM audio)...")
    tmp = dst.with_name(dst.stem + ".partial.mov")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
           "-progress", "pipe:1", "-i", str(src),
           "-map", "0:v:0", "-map", "0:a?",
           "-c:v", "prores_ks", "-profile:v", "3", "-vendor", "apl0", "-pix_fmt", "yuv422p10le",
           "-c:a", "pcm_s16le", "-map_metadata", "0", str(tmp)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _follow_progress(proc, info["duration"], rep)
        err = proc.stderr.read()
    except BaseException:                   # cancelled / Ctrl-C: don't leave a half file behind
        proc.kill()
        proc.wait()
        tmp.unlink(missing_ok=True)
        raise
    if proc.wait() != 0:
        tmp.unlink(missing_ok=True)
        raise AutocutError(f"ffmpeg failed converting {src}:\n{err.strip()}")
    tmp.replace(dst)
    return dst


def _follow_progress(proc, total_sec, rep):
    """Turn ffmpeg's `-progress pipe:1` key=value stream into reporter.progress() calls."""
    for line in proc.stdout:
        key, _, val = line.strip().partition("=")
        if key == "out_time_us" and val.isdigit() and total_sec:
            done = int(val) / 1e6
            rep.progress(min(done / total_sec, 1.0), f"{done:.0f}s / {total_sec:.0f}s")
