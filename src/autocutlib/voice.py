"""Change a character's voice with ElevenLabs' voice changer, via fal.ai.

Only the stretches of each take that the cut actually uses (primary and alternate clips, plus
a little padding) are sent to the API, one request per stretch, since it's billed per minute.
The converted audio is dropped back in at the same position over the take's original audio,
and the result is muxed with the untouched video into `<take>.<voice>.mov`, which then
stands in for the take in the FCPXML and preview. The voice changer keeps the performance's
timing, so the cut computed from the original audio still lines up.

Converted stretches are cached (~/Library/Caches/autocut/voice on macOS), so re-running a cut
doesn't pay for the same audio twice.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

from .media import _writable_dir
from .report import AutocutError, Reporter

FAL_APP = "fal-ai/elevenlabs/voice-changer"
PRICE_PER_MIN = 0.30          # USD, fal's listed price for this model
PAD = 0.4                     # seconds of extra audio either side of each used clip
JOIN_GAP = 1.0                # stretches closer than this are sent as one request

# ElevenLabs' standard voices. Any other voice name or ID that fal accepts can be typed in.
VOICES = ["Rachel", "Aria", "Sarah", "Laura", "Charlotte", "Alice", "Matilda", "Jessica", "Lily",
          "River", "Roger", "Charlie", "George", "Callum", "Liam", "Will", "Eric", "Chris",
          "Brian", "Daniel", "Bill"]


def cache_dir():
    base = (Path.home() / "Library" / "Caches" if sys.platform == "darwin"
            else Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")))
    d = base / "autocut" / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def require_fal():
    try:
        import fal_client  # noqa: F401
    except ImportError:
        raise AutocutError("fal-client isn't installed. Run: pip install -e .") from None
    if not (os.environ.get("FAL_KEY") or (os.environ.get("FAL_KEY_ID") and os.environ.get("FAL_KEY_SECRET"))):
        raise AutocutError("No fal.ai API key. Set it in the web UI (Options > Voice), or "
                           "export FAL_KEY=... before running.")


def call_fal(audio_path, voice, denoise, rep):
    """Send one audio file through the voice changer; returns the URL of the result."""
    import fal_client
    url = fal_client.upload_file(audio_path)

    def on_update(status):
        for entry in getattr(status, "logs", None) or []:
            msg = entry.get("message") if isinstance(entry, dict) else None
            if msg:
                rep.log(f"    fal: {msg}")

    try:
        result = fal_client.subscribe(FAL_APP, {"audio_url": url, "voice": voice,
                                                "remove_background_noise": bool(denoise)},
                                      with_logs=True, on_queue_update=on_update)
    except Exception as e:  # noqa: BLE001 - surface fal's message (bad voice name, no credit...)
        raise AutocutError(f"fal.ai voice changer failed: {e}") from None
    audio = result.get("audio") if isinstance(result, dict) else None
    out = audio.get("url") if isinstance(audio, dict) else (result or {}).get("audio_url")
    if not out:
        raise AutocutError(f"Unexpected reply from fal.ai: {json.dumps(result)[:300]}")
    return out


def merge_ranges(ranges, duration):
    """Pad, clamp, and join (start, end) ranges that overlap or nearly touch."""
    out = []
    for s, e in sorted((max(0.0, s - PAD), min(duration, e + PAD)) for s, e in ranges):
        if out and s - out[-1][1] < JOIN_GAP:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(round(s, 3), round(e, 3)) for s, e in out]


def _ffmpeg(args, what):
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AutocutError(f"ffmpeg failed {what}:\n{r.stderr.strip()}")


def _file_key(path):
    st = Path(path).stat()
    return f"{Path(path).resolve()}|{st.st_size}|{int(st.st_mtime)}"


def convert_range(take_path, start, end, voice, denoise, rep, caller=None):
    """Converted audio (48 kHz WAV, exactly end-start long) for one stretch of a take."""
    key = hashlib.sha1(f"{_file_key(take_path)}|{start}|{end}|{voice}|{denoise}".encode()).hexdigest()[:20]
    done = cache_dir() / f"{key}.wav"
    if done.exists():
        return done, False
    src = cache_dir() / f"{key}.src.wav"
    raw = cache_dir() / f"{key}.raw"
    dur = end - start
    try:
        _ffmpeg(["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(take_path), "-vn", "-ac", "1",
                 "-ar", "44100", "-c:a", "pcm_s16le", str(src)], "extracting audio")
        out_url = (caller or call_fal)(src, voice, denoise, rep)
        urllib.request.urlretrieve(out_url, raw)
        # Force the exact input length so it lands back in sync (pad or trim the tail).
        _ffmpeg(["-i", str(raw), "-af", f"aresample=48000,apad,atrim=duration={dur:.3f}",
                 "-ac", "1", "-c:a", "pcm_s16le", str(done)], "decoding the converted audio")
    finally:
        src.unlink(missing_ok=True)
        raw.unlink(missing_ok=True)
    return done, True


def voiced_path(take_path, voice, out_dir=None):
    safe = re.sub(r"[^\w\-]+", "_", voice).strip("_") or "voice"
    folder = Path(out_dir) if out_dir else Path(take_path).parent
    return folder / f"{Path(take_path).stem}.{safe}.mov"


def build_voiced_take(take, ranges, voice, opts, rep, caller=None):
    """Write the take's video + voice-changed audio to a new .mov and return its path."""
    info = take.info
    ranges = merge_ranges(ranges, info["duration"])
    if not ranges:
        return None
    dst = voiced_path(take.path, voice, opts.convert_dir)
    if not _writable_dir(dst.parent):
        dst = voiced_path(take.path, voice, opts.fallback_dir or cache_dir())
        _writable_dir(dst.parent)
    stamp = Path(str(dst) + ".json")
    sig = {"source": _file_key(take.path), "voice": voice, "denoise": bool(opts.voice_denoise),
           "ranges": ranges}
    if dst.exists() and stamp.exists():
        try:
            if json.loads(stamp.read_text()) == sig:
                rep.log(f"Reusing {dst.name}")
                return dst
        except ValueError:
            pass

    pieces, new = [], 0.0
    for n, (s, e) in enumerate(ranges, 1):
        rep.progress((n - 1) / len(ranges), f"stretch {n}/{len(ranges)}")
        wav, fresh = convert_range(take.path, s, e, voice, opts.voice_denoise, rep, caller)
        new += (e - s) if fresh else 0
        pieces.append((s, e, wav))
    rep.progress(1.0, "muxing")
    if new:
        rep.log(f"  converted {new:.0f}s of new audio (about ${new / 60 * PRICE_PER_MIN:.2f})")

    # Original audio with the converted stretches laid over it (original muted underneath).
    inputs = ["-i", str(take.path)]
    for _, _, wav in pieces:
        inputs += ["-i", str(wav)]
    mute = "+".join(f"between(t,{s},{e})" for s, e, _ in pieces)
    if info["channels"]:
        base = (f"[0:a:0]aresample=48000,aformat=channel_layouts=stereo,"
                f"volume=volume='if({mute},0,1)':eval=frame[base]")
    else:
        base = f"anullsrc=r=48000:cl=stereo,atrim=duration={info['duration']:.3f}[base]"
    parts = [base]
    for i, (s, _, _) in enumerate(pieces, 1):
        ms = int(round(s * 1000))
        parts.append(f"[{i}:a]afade=t=in:d=0.02,areverse,afade=t=in:d=0.02,areverse,"
                     f"aformat=channel_layouts=stereo,adelay={ms}|{ms}[v{i}]")
    mix = "[base]" + "".join(f"[v{i}]" for i in range(1, len(pieces) + 1))
    parts.append(f"{mix}amix=inputs={len(pieces) + 1}:normalize=0:duration=first[a]")

    tmp = dst.with_name(dst.stem + ".partial.mov")
    tc = ["-timecode", info["timecode"]] if info.get("timecode") else []
    try:
        _ffmpeg([*inputs, "-filter_complex", ";".join(parts), "-map", "0:v:0", "-map", "[a]",
                 "-c:v", "copy", "-c:a", "pcm_s16le", *tc, "-map_metadata", "0", str(tmp)],
                f"writing {dst.name}")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dst)
    stamp.write_text(json.dumps(sig))
    return dst


def preview(take_path, voice, start, seconds, denoise, rep=None, caller=None):
    """Convert a short stretch of a take, for auditioning a voice. Returns the WAV path."""
    from .media import probe
    rep = rep or Reporter()
    dur = probe(take_path)["duration"]
    start = max(0.0, min(start, max(dur - seconds, 0.0)))
    wav, _ = convert_range(take_path, round(start, 3), round(min(start + seconds, dur), 3),
                           voice, denoise, rep, caller)
    return wav
